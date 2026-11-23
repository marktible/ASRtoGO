"""Integration regressions for setup, migration and relocation (no downloads/GPU)."""
import importlib.util
import http.client
import hashlib
import json
import os
import pty
from pathlib import Path
import shutil
import select
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[2]

FAKE_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['STUDIO_TOOL_LOG'], 'a') as log:
    log.write(json.dumps([name] + args) + '\n')
if os.environ.get('FAIL_TOOL') == name:
    print('Intentional test failure: ' + name, file=sys.stderr)
    sys.exit(1)
if name == 'cmake':
    if args == ['--version']:
        print('cmake version 3.31.0')
    elif '-B' in args:
        source = pathlib.Path(args[args.index('-S') + 1])
        if os.environ.get('FAIL_SPIRV_HEADERS') and 'find_package(SPIRV-Headers' in (source / 'CMakeLists.txt').read_text():
            sys.exit(1)
        build = pathlib.Path(args[args.index('-B') + 1])
        build.mkdir(parents=True, exist_ok=True)
        (build / 'CMakeCache.txt').write_text('test cache')
    elif '--build' in args:
        if os.environ.get('FAIL_BUILD'):
            print('Intentional backend build failure', file=sys.stderr)
            sys.exit(1)
        build = pathlib.Path(args[args.index('--build') + 1])
        target = args[args.index('--target') + 1]
        binary = build / 'bin' / target
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
elif name == 'git' and args[0] == 'clone':
    (pathlib.Path(args[-1]) / '.git').mkdir(parents=True)
elif name == 'vulkaninfo':
    print('deviceType = ' + os.environ.get('TEST_VULKAN_DEVICE_TYPE', 'PHYSICAL_DEVICE_TYPE_DISCRETE_GPU'))
'''


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='studio portable ')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'original project'
        self.root.mkdir()
        for folder in ('internal/app', 'internal/scripts', 'internal/docs', 'internal/tools', 'models'):
            shutil.copytree(SOURCE / folder, self.root / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.gguf', '*.bin'))
        (self.root / 'internal/backends').mkdir()
        shutil.copy2(SOURCE / 'internal/backends/README.md', self.root / 'internal/backends/README.md')
        for name in ('setup.sh', 'start.sh', 'stop.sh', 'settings.sh', 'README.md', 'LICENSE', '.gitignore'):
            if name == 'setup.sh' and not (SOURCE / name).exists():
                content = subprocess.check_output(['git', '-C', str(SOURCE), 'show', 'HEAD:setup.sh'])
                (self.root / name).write_bytes(content)
            else:
                shutil.copy2(SOURCE / name, self.root / name)
        (self.root / 'internal/data/jobs').mkdir(parents=True)
        self.config = {'password_hash': 'existing hash', 'password_salt': 'existing salt',
                       'web_port': 9876, 'context_tokens': 16384, 'custom_setting': 'preserve me'}
        self.write_config(self.config)
        self.bin = self.base / 'fake tools'
        self.bin.mkdir()
        for name in ('git', 'cmake', 'pkg-config', 'glslc', 'vulkaninfo'):
            path = self.bin / name
            path.write_text(FAKE_TOOL)
            path.chmod(0o755)
        self.log = self.base / 'tools.jsonl'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'], STUDIO_TOOL_LOG=str(self.log))

    def write_config(self, config):
        (self.root / 'internal/data/config.json').write_text(json.dumps(config))

    def run_script(self, name, *args, success=True, **environment):
        process = subprocess.run(['bash', str(self.root / name), *args], cwd=self.base,
                                 env=dict(self.env, **environment), capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        else:
            self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        return process

    def tool_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def configured_app(self):
        process = subprocess.run([sys.executable, str(self.root / 'internal/scripts/configure.py')], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        return load_module('studio_test_app', self.root / 'internal/app/app.py')

    def settings_menu(self, replies):
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(self.base)
            os.execvp('bash', ['bash', str(self.root / 'settings.sh')])
        output = b''
        cursor = 0
        pending = iter(replies)
        reply = next(pending, None)
        deadline = time.monotonic() + 30
        status = None
        try:
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        data = os.read(terminal, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    output += data
                if reply is not None:
                    prompt, value = reply
                    position = output.find(prompt.encode(), cursor)
                    if position >= 0:
                        cursor = position + len(prompt)
                        os.write(terminal, (value + '\n').encode())
                        reply = next(pending, None)
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    break
                status = None
            if status is None:
                finished, result = os.waitpid(pid, os.WNOHANG)
                if finished:
                    status = result
            self.assertIsNotNone(status, output.decode(errors='replace'))
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors='replace'))
            self.assertIsNone(reply, output.decode(errors='replace'))
            return output.decode(errors='replace')
        finally:
            os.close(terminal)
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)

    def test_settings_exit_and_invalid_option_preserve_config(self):
        before = (self.root / 'internal/data/config.json').read_bytes()
        output = self.settings_menu([('Select an option: ', 'invalid'), ('Select an option: ', '0')])
        self.assertIn('Choose one of the listed options.', output)
        self.assertEqual((self.root / 'internal/data/config.json').read_bytes(), before)

    def test_settings_changes_password_privately_and_restarts_owned_web(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        old_password = 'Old-test-passphrase-42!'
        new_password = 'New-test-passphrase-73!'
        salt = bytes.fromhex('12345678901234567890123456789012')
        self.config.update(web_port=port, password_salt=salt.hex(),
                           password_hash=hashlib.pbkdf2_hmac('sha256', old_password.encode(), salt, 250000).hex())
        self.write_config(self.config)
        self.configured_app()

        def login(password):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                body = json.dumps({'password': password})
                connection.request('POST', '/api/login', body, {'Content-Type': 'application/json'})
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()

        try:
            self.run_script('start.sh')
            original_pid = (self.root / 'internal/data/web.pid').read_text()
            self.assertEqual(login(old_password), 200)
            output = self.settings_menu([('Select an option: ', '1'), ('New password: ', 'short'),
                ('New password: ', new_password), ('Repeat password: ', 'mismatch'),
                ('New password: ', new_password), ('Repeat password: ', new_password),
                ('Select an option: ', '0')])
            self.assertNotIn(new_password, output)
            self.assertIn('Passwords did not match.', output)
            self.assertIn('Password updated.', output)
            self.assertNotEqual((self.root / 'internal/data/web.pid').read_text(), original_pid)
            self.assertEqual(login(new_password), 200)
            self.assertEqual(login(old_password), 401)
            config_path = self.root / 'internal/data/config.json'
            cfg = json.loads(config_path.read_text())
            self.assertEqual(cfg['custom_setting'], self.config['custom_setting'])
            self.assertEqual(cfg['web_port'], port)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        finally:
            self.run_script('stop.sh')

    def test_setup_empty_models_builds_vulkan_preserves_settings_and_deletes_itself(self):
        self.run_script('setup.sh', '--no-start', BUILD_JOBS='2')
        self.assertFalse((self.root / 'setup.sh').exists())
        cfg = json.loads((self.root / 'internal/data/config.json').read_text())
        for key, value in self.config.items():
            self.assertEqual(cfg[key], value)
        self.assertEqual(cfg['model'], '')
        self.assertEqual(cfg['asr_model'], '')
        self.assertFalse(Path(cfg['llama_bin']).is_absolute())
        self.assertEqual((self.root / 'internal/data/config.json').stat().st_mode & 0o777, 0o600)
        calls = self.tool_calls()
        clones = [call for call in calls if call[:2] == ['git', 'clone']]
        self.assertEqual(len(clones), 2)
        self.assertTrue(all('--recurse-submodules' in call for call in clones))
        configurations = [call for call in calls if '-DGGML_VULKAN=ON' in call]
        self.assertEqual(len(configurations), 2)
        for call in configurations:
            for flag in ('-DCMAKE_BUILD_TYPE=Release', '-DGGML_NATIVE=ON', '-DGGML_VULKAN=ON', '-DGGML_LTO=ON', '-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON'):
                self.assertIn(flag, call)
        builds = [call for call in calls if '--build' in call]
        self.assertEqual({call[-1] for call in builds}, {'llama-server', 'crispasr'})
        self.assertTrue(all(call[call.index('--parallel') + 1] == '2' for call in builds))
        visible = {path.name for path in self.root.iterdir() if not path.name.startswith('.')}
        self.assertEqual(visible, {'README.md', 'LICENSE', 'start.sh', 'stop.sh', 'settings.sh', 'models', 'internal'})

    def test_build_failure_retains_setup_and_credentials(self):
        result = self.run_script('setup.sh', '--no-start', success=False, FAIL_BUILD='1')
        self.assertTrue((self.root / 'setup.sh').exists())
        self.assertIn('retained', result.stderr)
        self.assertEqual(json.loads((self.root / 'internal/data/config.json').read_text()), self.config)

    def test_missing_dependency_report_precedes_clone(self):
        result = self.run_script('setup.sh', '--no-start', success=False, FAIL_TOOL='pkg-config')
        self.assertIn('Vulkan development', result.stderr)
        self.assertTrue((self.root / 'setup.sh').exists())
        self.assertFalse(any(call[0] == 'git' for call in self.tool_calls()))

    def test_missing_spirv_headers_precedes_clone_and_retains_setup(self):
        result = self.run_script('setup.sh', '--no-start', success=False, FAIL_SPIRV_HEADERS='1')
        self.assertIn('SPIRV-HeadersConfig.cmake', result.stderr)
        self.assertIn('spirv-headers', result.stderr)
        self.assertTrue((self.root / 'setup.sh').exists())
        self.assertFalse(any(call[0] == 'git' for call in self.tool_calls()))
        self.assertFalse(any('--build' in call for call in self.tool_calls()))

    def test_spirv_headers_probe_finds_custom_cmake_and_sdk_prefixes(self):
        cmake = shutil.which('cmake')
        if cmake is None:
            self.skipTest('CMake is unavailable')
        (self.bin / 'cmake').unlink()
        (self.bin / 'cmake').symlink_to(cmake)
        prefix = self.base / 'custom SDK with spaces'
        config = prefix / 'share/cmake/SPIRV-Headers/SPIRV-HeadersConfig.cmake'
        config.parent.mkdir(parents=True)
        command = ['bash', '-c', 'source "$1"; check_spirv_headers',
                   'test', str(self.root / 'internal/scripts/dependencies.sh')]
        for variable in ('CMAKE_PREFIX_PATH', 'VULKAN_SDK'):
            with self.subTest(variable=variable):
                probe_dir = self.base / variable
                probe_dir.mkdir()
                env = dict(self.env, CMAKE_PREFIX_PATH='', VULKAN_SDK='', TMPDIR=str(probe_dir))
                env[variable] = str(prefix)
                # Prove this fixture is used even on hosts with system headers.
                config.write_text('message(FATAL_ERROR "Found fixture")\n')
                failed = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                config.write_text('add_library(SPIRV-Headers::SPIRV-Headers INTERFACE IMPORTED)\n')
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(list(probe_dir.iterdir()), [])

    def test_gpu_failure_retains_setup_and_precedes_clone(self):
        self.run_script('setup.sh', '--no-start', success=False, FAIL_TOOL='vulkaninfo')
        self.assertTrue((self.root / 'setup.sh').exists())
        self.assertFalse(any(call[0] == 'git' for call in self.tool_calls()))

    def test_software_only_vulkan_retains_setup_and_precedes_clone(self):
        result = self.run_script('setup.sh', '--no-start', success=False, TEST_VULKAN_DEVICE_TYPE='PHYSICAL_DEVICE_TYPE_CPU')
        self.assertIn('Only software/unsupported', result.stderr)
        self.assertTrue((self.root / 'setup.sh').exists())
        self.assertFalse(any(call[0] == 'git' for call in self.tool_calls()))

    def test_migrate_models_credentials_and_queued_job_then_relocate(self):
        old_model = self.root / 'plamo-test.gguf'
        old_model.write_bytes(b'test model')
        old_asr = self.root / 'whisper-test.gguf'
        old_asr.write_bytes(b'test asr')
        cfg = dict(self.config, root=str(self.root), model=str(old_model), asr_model=str(old_asr))
        self.write_config(cfg)
        jid = 'a' * 32
        record = {'state': {'id': jid, 'status': 'queued'},
                  'params': {'mode': 'translate', 'llm_model': str(old_model), 'source_file': jid + '.input.txt'}}
        (self.root / 'internal/data/jobs' / (jid + '.json')).write_text(json.dumps(record))
        (self.root / 'internal/data/jobs' / (jid + '.input.txt')).write_text('hello')
        (self.root / 'internal/data').rename(self.root / '.plamo-web')
        (self.root / 'llama.cpp/.git').mkdir(parents=True)
        self.run_script('setup.sh', '--no-start')
        self.assertFalse((self.root / '.plamo-web').exists())
        self.assertFalse((self.root / 'llama.cpp').exists())
        self.assertEqual((self.root / 'models/translation/plamo-test.gguf').read_bytes(), b'test model')
        saved = json.loads((self.root / 'internal/data/jobs' / (jid + '.json')).read_text())
        self.assertEqual(saved['params']['llm_model'], 'models/translation/plamo-test.gguf')
        moved = self.base / 'moved project with spaces'
        self.root.rename(moved)
        self.root = moved
        app = self.configured_app()
        self.assertEqual(app.scan_models('llm')[0]['id'], 'models/translation/plamo-test.gguf')
        self.assertEqual(app.scan_models('asr')[0]['id'], 'models/asr/whisper-test.gguf')
        self.assertTrue(Path(app.CFG['llama_bin']).is_file())
        app.recover_jobs()
        self.assertEqual(app.JOBS_STATE[jid]['status'], 'queued')
        self.assertEqual(app.JOB_QUEUE.get_nowait(), jid)
        cache = self.root / 'internal/backends/llama.cpp/build-studio/old-cache-marker'
        cache.write_text('obsolete')
        self.run_script('internal/scripts/build-backends.sh')
        self.assertFalse(cache.exists())
        self.assertTrue((self.root / 'internal/data/jobs' / (jid + '.input.txt')).is_file())

    def test_app_models_static_assets_and_portable_launch_arguments(self):
        app = self.configured_app()
        with self.assertRaisesRegex(ValueError, 'models/translation/'):
            app.validate_model('llm', '')
        (self.root / 'models/translation/model.gguf').write_bytes(b'model')
        (self.root / 'models/asr/speech.bin').write_bytes(b'asr')
        self.assertEqual([m['name'] for m in app.scan_models('llm')], ['model.gguf'])
        self.assertEqual([m['name'] for m in app.scan_models('asr')], ['speech.bin'])
        with self.assertRaises(ValueError):
            app.validate_model('llm', '/etc/passwd')
        binary = self.root / app.CFG['llama_bin']
        # CFG paths have already been resolved at import.
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        with patch.object(app, 'health', return_value='offline'), patch.object(app.subprocess, 'Popen') as popen:
            app.start_backend('llm', 'models/translation/model.gguf')
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('-m') + 1], str(self.root / 'models/translation/model.gguf'))
            self.assertEqual(command[command.index('-t') + 1], str(app.THREADS))
            self.assertIn('-ngl', command)
        handler = object.__new__(app.Handler)
        handler.path = '/script.js'
        handler.trusted = lambda: True
        handler.send_bytes = Mock()
        handler.do_GET()
        self.assertEqual(handler.send_bytes.call_args.args[0], (self.root / 'internal/app/script.js').read_bytes())
        app.CFG.update(allow_loginctl=False, allow_systemctl=True, allow_sudo_shutdown=False)
        with patch.object(app.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            app.poweroff_pc()
            self.assertEqual(run.call_args.args[0], ['systemctl', '--no-ask-password', 'poweroff'])

    def test_package_mappings_for_distro_families(self):
        for distro, family, manager, headers in (
                ('void', '', 'xbps-install', 'SPIRV-Headers'),
                ('ubuntu', 'debian', 'apt-get', 'spirv-headers'),
                ('linuxmint', 'ubuntu debian', 'apt-get', 'spirv-headers'),
                ('fedora', '', 'dnf', 'spirv-headers-devel'),
                ('arch', '', 'pacman', 'spirv-headers'),
                ('cachyos', 'arch', 'pacman', 'spirv-headers'),
                ('opensuse-tumbleweed', 'suse', 'zypper', 'spirv-headers'),
                ('alpine', '', 'apk', 'spirv-headers')):
            command = ['bash', '-c', 'source "$1"; dependency_command "$2" "$3"; printf "%s\\n" "${INSTALL[@]}"',
                       'test', str(self.root / 'internal/scripts/dependencies.sh'), distro, family]
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            packages = result.stdout.splitlines()
            self.assertEqual(packages[0], manager)
            self.assertIn(headers, packages)

    def test_auto_unload_switches_only_for_enabled_combined_jobs(self):
        app = self.configured_app()
        jid = 'a' * 32
        app.JOBS_STATE[jid] = {}
        loaded = {'llm'}
        events = []

        def stop(kind):
            events.append(('stop', kind))
            loaded.remove(kind)

        def start(kind, model):
            events.append(('start', kind))
            loaded.add(kind)

        with patch.object(app, 'start_backend', start), patch.object(app, 'stop_backend', stop), \
             patch.object(app, 'running_owned', loaded.__contains__), \
             patch.object(app, 'health', lambda kind: 'ready' if kind in loaded else 'offline'):
            app.JOB_PARAMS[jid] = {'mode': 'asr_translate', 'auto_unload': True}
            for kind in ('asr', 'llm', 'asr'):
                app.wait_ready(kind, 'model', jid)
                self.assertEqual(loaded, {kind})
            self.assertEqual(events, [('stop', 'llm'), ('start', 'asr'), ('stop', 'asr'),
                                      ('start', 'llm'), ('stop', 'llm'), ('start', 'asr')])
            for params in ({'mode': 'asr_translate'}, {'mode': 'asr_translate', 'auto_unload': False},
                           {'mode': 'asr'}, {'mode': 'translate'}):
                app.JOB_PARAMS[jid] = params
                events.clear()
                app.wait_ready('llm', 'model', jid)
                self.assertEqual(events, [('start', 'llm')])

    def test_update_publishes_only_a_working_pair(self):
        build = 'internal/scripts/build-backends.sh'
        self.run_script(build)
        self.run_script(build, '--update')
        maintenance = load_module('test_maintenance', self.root / 'internal/scripts/maintenance.py')
        current = self.root / 'internal/data/backend-builds/current'
        first = current.resolve()
        binary = self.root / 'internal/backends/llama.cpp/build-studio/bin/llama-server'
        self.assertEqual(maintenance.backend_binary('llm', binary), first / 'llama.cpp/bin/llama-server')
        self.assertTrue(maintenance.backend_binary('llm', binary).is_file())
        self.run_script(build, '--update', FAIL_BUILD='1', success=False)
        self.assertEqual(current.resolve(), first)
        self.run_script(build, '--update')
        self.assertNotEqual(current.resolve(), first)
        self.assertIn('pull', [arg for args in self.tool_calls() for arg in args])

    def test_desktop_control_targets_display_manager(self):
        maintenance = load_module('test_maintenance', self.root / 'internal/scripts/maintenance.py')
        with patch.object(maintenance, 'desktop_state', return_value='active'), \
             patch.object(maintenance.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            for action in ('stop', 'start'):
                maintenance.set_desktop(action)
                self.assertEqual(run.call_args.args[0],
                                 ['systemctl', '--no-ask-password', action, 'display-manager.service'])
            with self.assertRaises(ValueError):
                maintenance.set_desktop('disable')

    def test_service_rejects_occupied_port_then_starts_and_stops(self):
        try:
            occupied = socket.socket()
            occupied.bind(('0.0.0.0', 0))
            occupied.listen()
        except PermissionError:
            self.skipTest('Local TCP sockets are restricted in this environment')
        with occupied:
            port = occupied.getsockname()[1]
            self.config['web_port'] = port
            self.write_config(self.config)
            self.configured_app()
            result = self.run_script('start.sh', success=False)
            self.assertIn('Cannot listen on web port', result.stderr)
        try:
            self.run_script('start.sh')
            pid = (self.root / 'internal/data/web.pid').read_text()
            self.run_script('start.sh')
            self.assertEqual((self.root / 'internal/data/web.pid').read_text(), pid)
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                connection.request('GET', '/')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b'Local AI Studio', response.read())
            finally:
                connection.close()
        finally:
            self.run_script('stop.sh')
        self.assertFalse((self.root / 'internal/data/web.pid').exists())

    def test_package_excludes_private_data_models_and_backends(self):
        (self.root / 'models/asr/private.gguf').write_bytes(b'private model')
        (self.root / 'internal/data/private-transcript.txt').write_text('private')
        (self.root / 'internal/tests').mkdir()
        result = subprocess.run([sys.executable, str(self.root / 'internal/tools/package.py')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.root / 'internal/dist/ASRtoGO.tar.gz') as archive:
            names = archive.getnames()
        self.assertIn('ASRtoGO/setup.sh', names)
        self.assertIn('ASRtoGO/LICENSE', names)
        self.assertIn('ASRtoGO/start.sh', names)
        self.assertIn('ASRtoGO/settings.sh', names)
        self.assertIn('ASRtoGO/internal/scripts/service.py', names)
        self.assertIn('ASRtoGO/models/asr/README.md', names)
        visible = {Path(name).parts[1] for name in names if not Path(name).parts[1].startswith('.')}
        self.assertEqual(visible, {'README.md', 'LICENSE', 'setup.sh', 'start.sh', 'stop.sh', 'settings.sh', 'models', 'internal'})
        self.assertFalse(any('/data/' in name or name.endswith('.gguf') for name in names))

    def test_start_upgrades_previous_layout_without_reinstalling(self):
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
        except PermissionError:
            self.skipTest('Local TCP sockets are restricted in this environment')
        self.config.update(web_port=port, llama_bin='backends/llama.cpp/build-studio/bin/llama-server',
                           asr_bin='backends/CrispASR/build-studio/bin/crispasr', asr_root='backends/CrispASR')
        self.write_config(self.config)
        (self.root / 'internal/data/jobs/saved.result').write_text('keep this transcript')
        (self.root / 'internal/data').rename(self.root / 'data')
        for backend, name in (('llama.cpp', 'llama-server'), ('CrispASR', 'crispasr')):
            checkout = self.root / 'backends' / backend
            (checkout / '.git').mkdir(parents=True)
            binary = checkout / 'build-studio/bin' / name
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
        speech_model = self.root / 'backends/CrispASR/models/speech.gguf'
        speech_model.parent.mkdir()
        speech_model.write_bytes(b'ASR model')
        jid = 'b' * 32
        metadata = {'state': {'id': jid, 'status': 'done'},
                    'params': {'mode': 'asr', 'source_file': jid + '.wav',
                               'asr_model': 'backends/CrispASR/models/speech.gguf'}}
        (self.root / 'data/jobs' / (jid + '.json')).write_text(json.dumps(metadata))
        (self.root / 'backends/README.md').write_text('outdated backend instructions')
        try:
            self.run_script('start.sh')
            self.assertFalse((self.root / 'data').exists())
            self.assertFalse((self.root / 'backends').exists())
            self.assertEqual((self.root / 'internal/data/jobs/saved.result').read_text(), 'keep this transcript')
            cfg = json.loads((self.root / 'internal/data/config.json').read_text())
            self.assertEqual(cfg['password_hash'], self.config['password_hash'])
            self.assertEqual(cfg['web_port'], port)
            self.assertEqual(cfg['llama_bin'], 'internal/backends/llama.cpp/build-studio/bin/llama-server')
            self.assertTrue((self.root / cfg['llama_bin']).is_file())
            saved = json.loads((self.root / 'internal/data/jobs' / (jid + '.json')).read_text())
            self.assertEqual(saved['params']['asr_model'], 'internal/backends/CrispASR/models/speech.gguf')
            self.assertTrue((self.root / saved['params']['asr_model']).is_file())
            self.assertFalse(self.log.exists(), 'Upgrading layout must not clone or compile backends')
        finally:
            self.run_script('stop.sh')


if __name__ == '__main__':
    unittest.main()
