#!/usr/bin/env python3
"""Offline mobile translation + ASR on Linux. Stdlib only; local AI backends."""
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import queue
import re
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent
sys.path.insert(0, str(ROOT))
from scripts.hardware import detect

BASE = ROOT / 'data'
if not (BASE / 'config.json').is_file():
    raise SystemExit('Configuration missing. Run bash setup.sh first.')
CFG = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
def local_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def model_id(path):
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


for key in ('llama_bin', 'asr_bin', 'model', 'asr_model'):
    if CFG.get(key):
        CFG[key] = str(local_path(CFG[key]))
ASR_ROOT = local_path(CFG.get('asr_root', 'backends/CrispASR'))
CFG['ffmpeg_bin'] = shutil.which('ffmpeg') or ''
CFG['shutdown_bin'] = shutil.which('shutdown') or ''
if CFG.get('allow_systemctl') is None:
    CFG['allow_systemctl'] = bool(shutil.which('systemctl')) and Path('/run/systemd/system').is_dir()
THREADS = max(1, int(CFG.get('threads') or detect()['threads']))
JOBS = BASE / 'jobs'
JOBS.mkdir(exist_ok=True, mode=0o700)
os.chmod(JOBS, 0o700)
WEB_PORT = int(CFG.get('web_port', 8765))
LLM_PORT = int(CFG.get('backend_port', 8080))
ASR_PORT = int(CFG.get('asr_port', 8178))
CHUNK_TOKENS = 1800
MAX_OUTPUT_TOKENS = 4096
# The web app starts llama-server with this -c. Keep 512 tokens for message
# formatting and other template overhead when checking each request.
CONTEXT_TOKENS = max(8192, min(262144, int(CFG.get('context_tokens', 32768))))
CONTEXT_HEADROOM = 512
# Dynamic batches: never send more than 16 cues or ~1800 prompt tokens.
# This deliberately keeps substantial output space even on a 32K context.
SUBTITLE_BATCH_MAX_CUES = max(1, min(32, int(CFG.get('subtitle_batch_max_cues', 16))))
SUBTITLE_BATCH_INPUT_TOKENS = max(256, min(2400, int(CFG.get('subtitle_batch_input_tokens', 1800))))
MAX_TEXT = 10 * 1024 * 1024
MAX_AUDIO = 100 * 1024 * 1024
MAX_PREVIEW = 350_000  # Characters shown in web result; full output is always downloadable
SESSIONS, FAILURES, JOBS_STATE = {}, {}, {}
LOCK = threading.RLock()
JOB_QUEUE = queue.Queue()
JOB_PARAMS = {}
PROCESSES = {'llm': None, 'asr': None}
PROCESS_MODELS = {'llm': None, 'asr': None}
BUSY_JOB = None


def password_ok(value):
    salt = bytes.fromhex(CFG['password_salt'])
    stored = bytes.fromhex(CFG['password_hash'])
    attempt = hashlib.pbkdf2_hmac('sha256', str(value).encode('utf-8'), salt, 250000)
    return hmac.compare_digest(stored, attempt)


def http_json(url, data=None, timeout=30):
    body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def health(kind):
    port = LLM_PORT if kind == 'llm' else ASR_PORT
    try:
        info = http_json(f'http://127.0.0.1:{port}/health', timeout=1.3)
        if kind == 'asr' and isinstance(info, dict) and info.get('status') not in ('ok', 'ready', None):
            return 'loading'
        return 'ready'
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            return 'loading'
    except Exception:
        pass
    proc = PROCESSES[kind]
    return 'loading' if proc and proc.poll() is None else 'offline'


def running_owned(kind):
    p = PROCESSES[kind]
    return p is not None and p.poll() is None


def scan_models(kind):
    """Present files from explicitly configured, local directories only; no arbitrary paths from clients."""
    if kind == 'llm':
        dirs = [ROOT / 'models' / 'translation']
        suffixes = {'.gguf'}
    else:
        dirs = [ROOT / 'models' / 'asr']
        suffixes = {'.gguf', '.bin'}
    found = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        # Models are separated by task; filenames do not determine the task.
        for file in directory.iterdir():
            if file.is_file() and file.suffix.lower() in suffixes and not file.name.startswith('.'):
                p = model_id(file)
                found[p] = {'id': p, 'name': file.name, 'size_mb': round(file.stat().st_size / 1048576, 1)}
    selected = CFG.get('model' if kind == 'llm' else 'asr_model')
    if selected and Path(selected).is_file():
        p = model_id(Path(selected))
        found[p] = {'id': p, 'name': Path(p).name, 'size_mb': round(Path(selected).stat().st_size / 1048576, 1)}
    return sorted(found.values(), key=lambda x: (x['name'].lower() != Path(selected or '').name.lower(), x['name'].lower()))


def validate_model(kind, candidate):
    options = scan_models(kind)
    if not options:
        folder = 'translation' if kind == 'llm' else 'asr'
        raise ValueError(f'No local model found. Add your model to models/{folder}/ and refresh Settings.')
    if not candidate:
        return options[0]['id']
    if candidate not in {opt['id'] for opt in options}:
        raise ValueError('Selected model is not in the available model list')
    return candidate


def stop_backend(kind):
    with LOCK:
        proc = PROCESSES[kind]
        PROCESSES[kind] = None
        PROCESS_MODELS[kind] = None
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def start_backend(kind, model_id):
    model = validate_model(kind, model_id)
    with LOCK:
        if running_owned(kind) and PROCESS_MODELS[kind] == model:
            return model
        if running_owned(kind):
            stop_backend(kind)
        elif health(kind) in ('ready', 'loading'):
            raise RuntimeError(f'Port {LLM_PORT if kind == "llm" else ASR_PORT} is occupied by an externally started server. Stop that server first so model selection works.')
        if kind == 'llm':
            binary = Path(CFG['llama_bin'])
            command = [str(binary), '-m', str(local_path(model)), '--alias', 'local-translation',
                       '--host', '127.0.0.1', '--port', str(LLM_PORT), '-ngl', '99',
                       '-fa', 'on', '-c', str(CONTEXT_TOKENS), '-b', '512', '-ub', '512', '-t', str(THREADS),
                       '-np', '1', '--temp', '0', '--jinja', '--no-prefill-assistant']
            cwd = ROOT
        else:
            binary = Path(CFG.get('asr_bin', ''))
            command = [str(binary), '--server', '--gpu-backend', 'vulkan',
                       '-m', str(local_path(model)), '-l', 'auto', '-t', str(THREADS),
                       '--host', '127.0.0.1', '--port', str(ASR_PORT)]
            # Original start-asr.sh uses parakeet backend explicitly; leave auto-detection for others.
            if 'parakeet' in Path(model).name.lower():
                command += ['--backend', 'parakeet']
            cwd = ASR_ROOT
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f'{kind.upper()} executable not found: {binary}. Run bash scripts/build-backends.sh.')
        logpath = BASE / f'{kind}.log'
        environment = dict(os.environ)
        with logpath.open('ab', buffering=0) as handle:
            proc = subprocess.Popen(command, cwd=cwd, env=environment, stdout=handle,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        PROCESSES[kind] = proc
        PROCESS_MODELS[kind] = model
        return model


def wait_ready(kind, model, jid):
    start_backend(kind, model)
    for _ in range(180):
        if JOBS_STATE[jid].get('cancel'):
            raise RuntimeError('Job cancelled')
        if health(kind) == 'ready':
            return
        proc = PROCESSES[kind]
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f'{kind.upper()} server exited. Check data/{kind}.log')
        time.sleep(1)
    raise RuntimeError(f'{kind.upper()} server not ready. Check data/{kind}.log')


def token_count(text):
    try:
        result = http_json(f'http://127.0.0.1:{LLM_PORT}/tokenize',
                           {'content': text, 'add_special': False}, timeout=50)
        return len(result['tokens'])
    except Exception:
        return len(text.encode('utf-8')) // 2 + 1


def exact_token_count(text):
    """Count the *real* model tokens. Fail closed if llama.cpp cannot tokenize.

    Approximate byte/character estimates are okay for chunk discovery, but
    must never be used as a safety gate immediately before inference.
    """
    try:
        result = http_json(f'http://127.0.0.1:{LLM_PORT}/tokenize',
                           {'content': text, 'add_special': False}, timeout=60)
        tokens = result.get('tokens') if isinstance(result, dict) else None
        if not isinstance(tokens, list):
            raise ValueError('missing tokens array')
        return len(tokens)
    except Exception as exc:
        raise RuntimeError('Cannot verify context length using llama.cpp /tokenize; '
                           'refusing to send unmeasured text (nothing was truncated).') from exc


def ensure_prompt_fits(messages, output_tokens=MAX_OUTPUT_TOKENS):
    """Conservatively preflight full message content plus output reservation.

    Chat-template wrappers are not part of simple /tokenize requests. A fixed
    headroom covers formatting overhead for the short request templates used
    here; the small batch-input cap provides another large buffer.
    """
    measured = sum(exact_token_count(m.get('content', '')) for m in messages)
    if measured + output_tokens + CONTEXT_HEADROOM > CONTEXT_TOKENS:
        raise InputLimitError('Prompt + reserved output exceed the configured '
                              'context limit; splitting before inference.')
    return measured


def chunk_text(text, limit=CHUNK_TOKENS):
    pos = 0
    while pos < len(text):
        rest = text[pos:]
        bound = min(len(rest), 6500)
        if len(rest) <= bound and token_count(rest) <= limit:
            yield rest
            return
        lo, hi = 1, bound
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if token_count(rest[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        end = max(1, lo)
        if end < len(rest):
            window = rest[:end]
            candidates = [window.rfind(x, end // 2) + len(x) for x in
                          ('\n\n', '\n', '。', '！', '？', '. ', '! ', '? ', ' ')
                          if window.rfind(x, end // 2) != -1]
            if candidates:
                end = max(candidates)
        yield rest[:end]
        pos += end


class OutputLimitError(RuntimeError):
    pass


class InputLimitError(RuntimeError):
    pass


class SubtitleAlignmentError(RuntimeError):
    pass


def translate_piece(piece, source, target, model, history):
    if not piece.strip():
        return piece
    is_plamo = 'plamo' in Path(model).name.lower()
    if is_plamo:
        messages = []
        for original, translated in history[-1:]:
            messages += [{'role': 'user', 'content': original},
                         {'role': 'assistant', 'content': translated}]
        messages.append({'role': 'user', 'content': piece})
        extra = {'chat_template_kwargs': {'source_lang': source, 'target_lang': target},
                 'stop': ['<|plamo:tag|>', '<|plamo:bos|>']}
    else:
        from_name = {'ja': 'Japanese', 'en': 'English'}[source]
        to_name = {'ja': 'Japanese', 'en': 'English'}[target]
        messages = [{'role': 'system', 'content':
                     f'Translate from {from_name} to {to_name}. Return ONLY the translation. '
                     'Preserve names, line breaks, and meaning. No explanations.'}]
        messages += [{'role': 'user', 'content': piece}]
        extra = {}
    ensure_prompt_fits(messages)
    payload = {'model': 'local-translation', 'messages': messages, 'temperature': 0,
               'top_k': 1, 'max_tokens': MAX_OUTPUT_TOKENS, 'stream': False, **extra}
    try:
        response = http_json(f'http://127.0.0.1:{LLM_PORT}/v1/chat/completions', payload, timeout=900)
    except urllib.error.HTTPError as exc:
        # Older/newer llama.cpp builds sometimes report an over-context request
        # as HTTP 400/413 instead of a structured finish_reason. Retry smaller.
        reason = exc.read(2048).decode('utf-8', 'replace')
        if exc.code in (400, 413, 422) and any(term in reason.lower() for term in
                ('context', 'too many tokens', 'prompt is too long', 'exceed')):
            raise InputLimitError('llama-server rejected the prompt as too long; splitting') from exc
        raise RuntimeError(f'llama-server HTTP {exc.code}: {reason[:500]}') from exc
    choice = response['choices'][0]
    value = choice['message']['content']
    if choice.get('finish_reason') == 'length':
        raise OutputLimitError('Translation reached its output token limit')
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError('Translation returned empty text')
    return value.strip()


def split_for_retry(piece):
    """Split at a natural boundary near the middle, or at the midpoint as a last resort."""
    middle = len(piece) // 2
    lo, hi = max(1, middle // 2), min(len(piece) - 1, middle + middle // 2)
    candidates = [m.end() for m in re.finditer(r'\n|[。！？!?]|[.!?]\s+|\s+', piece)
                  if lo <= m.end() <= hi]
    boundary = min(candidates, key=lambda p: abs(p - middle)) if candidates else middle
    return piece[:boundary], piece[boundary:]


def translate_safely(piece, source, target, model, history=(), *, depth=0):
    """Retry runaway generations using progressively smaller inputs.

    PLaMo may receive a single huge ASR segment, even when ASR chunk_seconds is set.
    Input and generated output limits are independent, so an input can be short
    enough to fit the model's context yet still reach max_tokens.
    """
    if not piece.strip():
        return piece
    if depth >= 16:
        raise RuntimeError('A very long subtitle could not be split safely after 16 attempts. '
                           'The original ASR transcript is preserved.')
    if exact_token_count(piece) > CHUNK_TOKENS:
        left, right = split_for_retry(piece)
    else:
        try:
            return translate_piece(piece, source, target, model, history)
        except (OutputLimitError, InputLimitError):
            # Retry once without history before splitting; do not discard text.
            if history:
                try:
                    return translate_piece(piece, source, target, model, [])
                except (OutputLimitError, InputLimitError):
                    pass
            if len(piece.strip()) < 24:
                raise RuntimeError('Translation repeatedly exceeded its context/output limit '
                                   'even on a short segment. Original transcript is preserved; '
                                   'check llama.cpp logs and chat template.')
            left, right = split_for_retry(piece)
    first = translate_safely(left, source, target, model, history, depth=depth+1)
    # Avoid propagating long history into the split continuation.
    second = translate_safely(right, source, target, model, [(left, first)], depth=depth+1)
    # Do not drop newlines or whitespace at a split boundary.
    if left.endswith('\n'):
        joiner = '\n'
    elif left.endswith((' ', '\t')):
        joiner = ' '
    else:
        joiner = ' ' if target == 'en' else ''
    return first.rstrip() + joiner + second.lstrip()


# Subtitle markers are intentionally kept OUTSIDE the timestamp format.
# Only dialogue reaches the translation backend; cue times remain in Python.
CUE_MARKER = re.compile(r'(?m)^\s*\[\[CUE([0-9]{4})\]\][ \t]*')


def marked_subtitle_text(cues):
    """Encode cue indices for one request, without ever including timestamps."""
    return '\n'.join(f'[[CUE{i:04d}]] ' + ' '.join(content.split())
                     for i, (_stamp, content) in enumerate(cues, 1))


def parse_marked_translation(output, expected):
    """Reject missing/duplicated/reordered markers rather than guessing alignment."""
    matches = list(CUE_MARKER.finditer(output))
    if len(matches) != expected or not matches:
        raise SubtitleAlignmentError('Batch translation lost subtitle ID markers')
    if output[:matches[0].start()].strip():
        raise SubtitleAlignmentError('Batch translation added unmarked content')
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, expected + 1)):
        raise SubtitleAlignmentError('Batch translation changed subtitle IDs/order')
    translated = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        content = output[match.end():end].strip()
        if not content:
            raise SubtitleAlignmentError('Batch translation omitted dialogue')
        translated.append(content)
    return translated


def batch_subtitles(cues, start):
    """Build the largest next batch that fits the user-selected count/token cap.

    Each candidate is measured through /tokenize BEFORE being sent for inference.
    A pathological single cue goes through translate_safely, not truncation.
    """
    selected = []
    while start + len(selected) < len(cues) and len(selected) < SUBTITLE_BATCH_MAX_CUES:
        candidate = selected + [cues[start + len(selected)]]
        measured = exact_token_count(marked_subtitle_text(candidate))
        if measured > SUBTITLE_BATCH_INPUT_TOKENS or measured + MAX_OUTPUT_TOKENS + CONTEXT_HEADROOM > CONTEXT_TOKENS:
            break
        selected = candidate
    return selected or [cues[start]]


def translate_subtitle_batch(cues, source, target, model):
    """Translate a batch with markers; halve and retry on length/alignment errors.

    Yield completed (stamp, translated_text) pairs incrementally so the caller
    can keep partial results even when a later half ultimately fails.
    """
    if len(cues) == 1:
        stamp, speech = cues[0]
        translated = translate_safely(speech, source, target, model)
        yield stamp, translated
        return
    try:
        prompt = marked_subtitle_text(cues)
        # The LLM gets cue IDs, but never timecodes.
        response = translate_piece(prompt, source, target, model, [])
        aligned = parse_marked_translation(response, len(cues))
    except (InputLimitError, OutputLimitError, SubtitleAlignmentError):
        half = len(cues) // 2
        yield from translate_subtitle_batch(cues[:half], source, target, model)
        yield from translate_subtitle_batch(cues[half:], source, target, model)
    else:
        yield from zip((stamp for stamp, _ in cues), aligned)


def translate_subtitle_cues(jid, cues, fmt, source, target, model, save_partial=True):
    """Dynamic-batch subtitles and preserve original timing and output order."""
    translated = []
    index = 0
    while index < len(cues):
        ensure_not_cancelled(jid)
        batch = batch_subtitles(cues, index)
        for stamp, english in translate_subtitle_batch(batch, source, target, model):
            ensure_not_cancelled(jid)
            translated.append((stamp, english))
            # Store a partial result as soon as a batch (or retry subgroup) finishes.
            if save_partial:
                partial = ('\n'.join(t for _, t in translated) + '\n' if fmt == 'txt'
                           else subtitle_output(translated, fmt))
                (JOBS / f'{jid}.partial').write_text(partial, encoding='utf-8')
            set_progress(jid, done=len(translated),
                         message=f'Translated {len(translated)}/{len(cues)} subtitles')
        index += len(batch)
    return translated


def parse_subtitles(text, kind):
    text = text.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff').strip()
    if kind == 'vtt':
        text = re.sub(r'^WEBVTT[^\n]*\n', '', text, count=1).strip()
    items = []
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        ts = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if ts is None:
            continue
        content = '\n'.join(lines[ts + 1:]).strip()
        if content:
            items.append((lines[ts].strip(), content))
    if not items:
        raise ValueError('No subtitle timestamps found in this SRT/VTT file')
    return items


def fmt_timestamp(seconds, kind):
    ms = max(0, int(round(float(seconds or 0) * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}{"," if kind == "srt" else "."}{ms:03d}'


def subtitle_output(items, kind):
    lines = ['WEBVTT', ''] if kind == 'vtt' else []
    for i, (timestamp, content) in enumerate(items, 1):
        if kind == 'srt':
            lines.append(str(i))
        # Normalize separators while preserving exact cue times.
        if kind == 'srt':
            stamp = timestamp.replace('.', ',')
        else:
            stamp = timestamp.replace(',', '.')
        lines.extend([stamp, content, ''])
    return '\n'.join(lines) + '\n'


def asr_request(audio_path, language):
    """Stream a multipart upload to CrispASR's OpenAI-compatible local endpoint."""
    boundary = '----local-asr-' + secrets.token_hex(14)
    fields = {'response_format': 'verbose_json', 'language': language,
              'split_on_punct': 'true', 'chunk_seconds': '30'}
    field_bytes = b''.join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items())
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
              'Content-Type: application/octet-stream\r\n\r\n').encode()
    suffix = f'\r\n--{boundary}--\r\n'.encode()
    length = len(field_bytes) + len(prefix) + audio_path.stat().st_size + len(suffix)
    conn = http.client.HTTPConnection('127.0.0.1', ASR_PORT, timeout=3600)
    try:
        conn.putrequest('POST', '/v1/audio/transcriptions')
        conn.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
        conn.putheader('Content-Length', str(length))
        conn.endheaders()
        conn.send(field_bytes)
        conn.send(prefix)
        with audio_path.open('rb') as infile:
            while True:
                block = infile.read(1024 * 1024)
                if not block:
                    break
                conn.send(block)
        conn.send(suffix)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f'CrispASR error {response.status}: ' + body[:1000].decode('utf-8', 'replace'))
        return json.loads(body)
    finally:
        conn.close()


def convert_audio(path):
    """Normalize audio/video through ffmpeg for reliable backend ingestion."""
    ffmpeg = CFG.get('ffmpeg_bin')
    if not ffmpeg:
        if path.suffix.lower() in {'.mp4', '.mov', '.webm', '.mkv'}:
            raise RuntimeError('FFmpeg is required to extract audio from videos. Run bash scripts/dependencies.sh --install.')
        return path
    converted = path.with_name(path.stem + '-16k.wav')
    cmd = [ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-i', str(path),
           '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(converted)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1500)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Audio conversion timed out') from exc
    if p.returncode != 0 or not converted.exists():
        raise RuntimeError('Cannot decode audio: ' + p.stderr[-400:])
    return converted


def create_audio_preview(audio_path):
    """Prepare a browser-friendly MP3 when ffmpeg is available.

    The user-uploaded original remains available as a fallback. A failed MP3
    transcode never fails ASR or translation.
    """
    ffmpeg = CFG.get('ffmpeg_bin')
    if not ffmpeg:
        return
    jid = audio_path.name.split('.', 1)[0]
    if not re.fullmatch(r'[a-f0-9]{32}', jid):
        return
    preview = JOBS / (jid + '.audio.mp3')
    try:
        result = subprocess.run(
            [ffmpeg, '-nostdin', '-loglevel', 'error', '-y', '-i', str(audio_path),
             '-vn', '-ar', '24000', '-ac', '1', '-codec:a', 'libmp3lame',
             '-q:a', '6', str(preview)],
            timeout=1800, capture_output=True, text=True)
        if result.returncode != 0 or not preview.is_file() or preview.stat().st_size == 0:
            preview.unlink(missing_ok=True)
    except Exception as exc:
        preview.unlink(missing_ok=True)
        print('Audio player conversion skipped:', exc, flush=True)


def job_audio_file(jid):
    """Resolve only media from a registered ASR job, never an arbitrary path."""
    params = JOB_PARAMS.get(jid)
    if not params or params.get('mode') not in ('asr', 'asr_translate'):
        return None
    preview = JOBS / (jid + '.audio.mp3')
    if preview.is_file():
        return preview
    source = str(params.get('source_file', ''))
    if not re.fullmatch(r'[a-f0-9]{32}\.[a-zA-Z0-9.]+', source):
        return None
    original = JOBS / source
    return original if original.is_file() else None


def persist_job(jid):
    # Write metadata atomically; completed files remain on the PC after browser closes.
    # Inputs are stored separately. Never put source text or audio in JSON metadata.
    record = {'state': JOBS_STATE[jid], 'params': JOB_PARAMS[jid]}
    target = JOBS / (jid + '.json')
    tmp = JOBS / (jid + '.json.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(record, out, ensure_ascii=False)
    tmp.replace(target)


def set_progress(jid, **values):
    with LOCK:
        JOBS_STATE[jid].update(values)
        if values.get('status') in ('done', 'error', 'cancelled'):
            JOBS_STATE[jid]['finished_at'] = time.time()
        persist_job(jid)


def create_job(jid, name, filename, mode, params, source_file, status='queued'):
    with LOCK:
        JOB_PARAMS[jid] = dict(params, source_file=source_file)
        JOBS_STATE[jid] = {'id': jid, 'status': status,
                           'message': 'Uploading...' if status == 'uploading' else 'Waiting in queue...',
                           'done': 0, 'total': 0, 'preview': '', 'filename': filename,
                           'source_name': name[:150], 'mode': mode, 'created_at': time.time(),
                           'cancel': False}
        persist_job(jid)


def start_queued_worker():
    threading.Thread(target=background_worker, name='local-ai-job-queue', daemon=True).start()


def background_worker():
    global BUSY_JOB
    while True:
        jid = JOB_QUEUE.get()
        try:
            with LOCK:
                state = JOBS_STATE.get(jid)
                if state is None or state.get('status') == 'cancelled':
                    continue
                BUSY_JOB = jid
                params = JOB_PARAMS[jid].copy()
            textpath = JOBS / params['source_file']
            if not textpath.is_file():
                raise RuntimeError('Input file was removed before processing')
            if params['mode'] == 'translate':
                params['text'] = textpath.read_text(encoding='utf-8')
                worker(jid, params)
            else:
                worker(jid, params, textpath)
        except Exception as exc:
            set_progress(jid, status='error', message=str(exc))
        finally:
            with LOCK:
                BUSY_JOB = None
            JOB_QUEUE.task_done()


def recover_jobs():
    # Requeue unfinished jobs when web-start.sh restarts the app.
    for metadata in sorted(JOBS.glob('*.json')):
        jid = metadata.stem
        if not re.fullmatch(r'[a-f0-9]{32}', jid):
            continue
        try:
            record = json.loads(metadata.read_text(encoding='utf-8'))
            state, params = record['state'], record['params']
            if state['id'] != jid or not re.fullmatch(r'[a-f0-9]{32}\.[a-zA-Z0-9.]+', params['source_file']):
                continue
            if params.get('mode') not in ('translate', 'asr', 'asr_translate', 'archive'):
                continue
            JOBS_STATE[jid] = state
            JOB_PARAMS[jid] = params
            if state['status'] == 'uploading':
                set_progress(jid, status='error', message='Upload interrupted. Please submit the file again.')
            elif state['status'] in ('queued', 'loading', 'running'):
                if not (JOBS / params['source_file']).is_file():
                    set_progress(jid, status='error', message='Input missing after restart')
                else:
                    set_progress(jid, status='queued', done=0, total=0,
                                 message='Restored after server restart. Waiting in queue...')
                    JOB_QUEUE.put(jid)
        except Exception as exc:
            print('Skipping invalid saved job', metadata.name, exc, flush=True)
    # Old versions stored only <id>.result files. Keep them available for download
    # and explicit deletion rather than silently deleting a user's earlier work.
    for result_file in JOBS.glob('*.result'):
        jid = result_file.stem
        if not re.fullmatch(r'[a-f0-9]{32}', jid) or jid in JOBS_STATE:
            continue
        try:
            with result_file.open('r', encoding='utf-8', errors='replace') as reader:
                preview = reader.read(9000)
            ext = 'vtt' if preview.startswith('WEBVTT') else ('srt' if re.search(r'\d\d:\d\d:\d\d,\d{3}\s+-->', preview) else 'txt')
            JOB_PARAMS[jid] = {'mode': 'archive', 'source_file': jid + '.archive'}
            JOBS_STATE[jid] = {'id': jid, 'status': 'done', 'message': 'Saved by a previous version',
                               'done': 1, 'total': 1, 'preview': preview, 'filename': f'previous-output-{jid[:8]}.{ext}',
                               'source_name': f'Previous result {jid[:8]}', 'mode': 'archive',
                               'created_at': result_file.stat().st_mtime, 'cancel': False}
            persist_job(jid)
        except Exception as exc:
            print('Could not import previous result', result_file.name, exc, flush=True)


def ensure_not_cancelled(jid):
    if JOBS_STATE[jid].get('cancel'):
        raise RuntimeError('Job cancelled')


def task_translate(jid, content, kind, source, target, model):
    set_progress(jid, status='loading', message='Loading translation model...')
    wait_ready('llm', model, jid)
    if kind in ('srt', 'vtt'):
        chunks = parse_subtitles(content, kind)
    else:
        set_progress(jid, message='Splitting text into chunks...')
        chunks = list(chunk_text(content))
    if not chunks:
        raise ValueError('Nothing to translate')
    set_progress(jid, total=len(chunks), status='running')
    if kind in ('srt', 'vtt'):
        return subtitle_output(translate_subtitle_cues(jid, chunks, kind, source, target, model), kind)
    history, output = [], []
    for i, chunk in enumerate(chunks, 1):
        ensure_not_cancelled(jid)
        text = chunk[1] if kind in ('srt', 'vtt') else chunk
        translated = translate_safely(text, source, target, model, history)
        history.append((text, translated))
        history = history[-2:]
        if kind in ('srt', 'vtt'):
            output.append((chunk[0], translated))
        else:
            lead = text[:len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            output.append(lead + translated + trail)
        set_progress(jid, done=i, message=f'Translated {i}/{len(chunks)} chunks')
    return subtitle_output(output, kind) if kind in ('srt', 'vtt') else ''.join(output)


def task_asr(jid, audio_path, mode, fmt, asr_model, llm_model, source, target, language):
    set_progress(jid, status='loading', message='Starting CrispASR...')
    wait_ready('asr', asr_model, jid)
    ensure_not_cancelled(jid)
    set_progress(jid, status='running', message='Preparing audio...')
    audio = convert_audio(audio_path)
    create_audio_preview(audio_path)
    ensure_not_cancelled(jid)
    set_progress(jid, message='Transcribing audio with CrispASR...')
    result = asr_request(audio, language)
    if not isinstance(result, dict):
        raise RuntimeError('Unexpected CrispASR response')
    full_text = result.get('text', '')
    segs = []
    for row in result.get('segments', []):
        if not isinstance(row, dict) or not row.get('text', '').strip():
            continue
        segs.append((fmt_timestamp(row.get('start', 0), fmt) + ' --> ' +
                     fmt_timestamp(row.get('end', 0), fmt), str(row['text']).strip()))
    if not segs and full_text.strip():
        duration = result.get('duration', 0)
        segs = [(fmt_timestamp(0, fmt) + ' --> ' + fmt_timestamp(duration, fmt), full_text.strip())]
    if not segs:
        raise RuntimeError('CrispASR returned no transcript')
    if mode == 'asr_translate':
        raw_output = ('\n'.join(text for _, text in segs) + '\n' if fmt == 'txt'
                      else subtitle_output(segs, fmt))
        (JOBS / f'{jid}.transcript').write_text(raw_output, encoding='utf-8')
        set_progress(jid, status='loading', message='Loading translation model...')
        wait_ready('llm', llm_model, jid)
        set_progress(jid, status='running', total=len(segs), message='Translating transcript...')
        segs = translate_subtitle_cues(jid, segs, fmt, source, target, llm_model)
    if fmt == 'txt':
        return '\n'.join(text for _, text in segs) + '\n'
    return subtitle_output(segs, fmt)


def worker(jid, params, audio_path=None):
    global BUSY_JOB
    try:
        if audio_path is None:
            result = task_translate(jid, params['text'], params['kind'], params['source'],
                                    params['target'], params['llm_model'])
        else:
            result = task_asr(jid, audio_path, params['mode'], params['format'],
                              params['asr_model'], params.get('llm_model'), params['source'],
                              params['target'], params['language'])
        path = JOBS / f'{jid}.result'
        path.write_text(result, encoding='utf-8')
        (JOBS / f'{jid}.partial').unlink(missing_ok=True)
        set_progress(jid, status='done', message='Finished. Saved on this PC.', preview=result[:9000],
                     done=max(1, JOBS_STATE[jid].get('total', 1)))
    except Exception as exc:
        status = 'cancelled' if JOBS_STATE[jid].get('cancel') else 'error'
        set_progress(jid, status=status, message=str(exc))
    finally:
        # Keep ASR originals for authenticated playback until the user deletes
        # the job. Translated text sources are not needed once processed.
        source = JOBS / JOB_PARAMS[jid]['source_file']
        if audio_path:
            audio_path.with_name(audio_path.stem + '-16k.wav').unlink(missing_ok=True)
        else:
            source.unlink(missing_ok=True)


def poweroff_pc():
    issues = []
    if CFG.get('allow_loginctl'):
        try:
            proc = subprocess.run(['loginctl', 'poweroff'], capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                return
            issues.append('loginctl: ' + (proc.stderr.strip() or str(proc.returncode)))
        except Exception as exc:
            issues.append(str(exc))
    if CFG.get('allow_systemctl'):
        try:
            proc = subprocess.run(['systemctl', '--no-ask-password', 'poweroff'], capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                return
            issues.append('systemctl: ' + (proc.stderr.strip() or str(proc.returncode)))
        except Exception as exc:
            issues.append(str(exc))
    if CFG.get('allow_sudo_shutdown') and CFG.get('shutdown_bin'):
        try:
            proc = subprocess.run(['sudo', '-n', CFG['shutdown_bin'], '-P', 'now'],
                                  capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                return
            issues.append('sudo: ' + (proc.stderr.strip() or str(proc.returncode)))
        except Exception as exc:
            issues.append(str(exc))
    raise RuntimeError('Shutdown permission unavailable. ' + '; '.join(issues))


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalAI/6.0'

    def log_message(self, fmt, *args):
        print('[{}] {}'.format(self.log_date_time_string(), fmt % args), flush=True)

    def send_json(self, obj, status=200, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_bytes(data, 'application/json; charset=utf-8', status, cookie)

    def send_bytes(self, data, mime, status=200, cookie=None, disposition=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(data)

    def send_audio(self, file):
        """Stream authenticated media and support HTTP byte ranges for seeking."""
        size = file.stat().st_size
        mime = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
        hdr = self.headers.get('Range', '').strip()
        begin, end = 0, size - 1
        if hdr:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', hdr)
            if not match or (not match.group(1) and not match.group(2)):
                return self.send_audio_range_error(size)
            if not match.group(1):
                length = int(match.group(2))
                if length <= 0:
                    return self.send_audio_range_error(size)
                begin = max(0, size - length)
            else:
                begin = int(match.group(1))
                if match.group(2):
                    end = min(size - 1, int(match.group(2)))
            if begin > end or begin >= size:
                return self.send_audio_range_error(size)
        self.send_response(206 if hdr else 200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Disposition', 'inline')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - begin + 1))
        if hdr:
            self.send_header('Content-Range', f'bytes {begin}-{end}/{size}')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        try:
            with file.open('rb') as reader:
                reader.seek(begin)
                remaining = end - begin + 1
                while remaining:
                    block = reader.read(min(128 * 1024, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Browser paused/seeked to another position.

    def send_audio_range_error(self, size):
        self.send_response(416)
        self.send_header('Content-Range', f'bytes */{size}')
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def session(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get('Cookie', ''))
            sid = c.get('plamo_session')
            if sid is None:
                return None
            session = SESSIONS.get(sid.value)
            if session and session['expiry'] > time.time():
                return session
        except Exception:
            return None
        return None

    def csrf_ok(self, session):
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if origin and urlsplit(origin).netloc != host:
            return False
        return hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf'])

    def trusted(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')
        except ValueError:
            return False

    def body_json(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size <= 0 or size > MAX_TEXT + 10000:
            raise ValueError('Request too large or empty (10 MB text maximum)')
        obj = json.loads(self.rfile.read(size))
        if not isinstance(obj, dict):
            raise ValueError('Expected JSON object')
        return obj

    @staticmethod
    def public_job(item):
        jid = item['id']
        data = {k: v for k, v in item.items() if k != 'cancel'}
        data['has_transcript'] = (JOBS / f'{jid}.transcript').is_file()
        data['has_partial'] = (JOBS / f'{jid}.partial').is_file()
        data['has_audio'] = job_audio_file(jid) is not None
        return data

    def do_GET(self):
        if not self.trusted():
            return self.send_json({'error': 'Only LAN/Tailscale access is allowed'}, 403)
        path = urlsplit(self.path).path
        if path in ('/', '/index.html', '/style.css', '/script.js'):
            name = 'index.html' if path in ('/', '/index.html') else path[1:]
            mimes = {'index.html': 'text/html; charset=utf-8',
                     'style.css': 'text/css; charset=utf-8',
                     'script.js': 'application/javascript; charset=utf-8'}
            return self.send_bytes((ASSETS / name).read_bytes(), mimes[name])
        if path == '/api/session':
            s = self.session()
            return self.send_json({'authenticated': bool(s), 'csrf': s['csrf'] if s else ''})
        if not self.session():
            return self.send_json({'error': 'Login required'}, 401)
        if path == '/api/models':
            return self.send_json({'llm': scan_models('llm'), 'asr': scan_models('asr'),
                                   'asr_available': Path(CFG.get('asr_bin', '')).is_file()})
        if path == '/api/jobs':
            with LOCK:
                records = [self.public_job(j) for j in JOBS_STATE.values()]
            return self.send_json({'jobs': sorted(records, key=lambda j: j.get('created_at', 0), reverse=True)[:150]})
        if path == '/api/status':
            return self.send_json({'llm': health('llm'), 'asr': health('asr'),
                                   'llm_model': PROCESS_MODELS['llm'], 'asr_model': PROCESS_MODELS['asr'],
                                   'llm_owned': running_owned('llm'), 'asr_owned': running_owned('asr'),
                                   'poweroff': CFG.get('allow_loginctl') or CFG.get('allow_systemctl') or CFG.get('allow_sudo_shutdown'),
                                   'active_job': BUSY_JOB, 'queued': JOB_QUEUE.qsize()})
        m = re.fullmatch(r'/api/job/([a-f0-9]{32})', path)
        if m:
            with LOCK:
                item = JOBS_STATE.get(m.group(1))
                return self.send_json(self.public_job(item) if item else {'error': 'Job not found'},
                                      200 if item else 404)
        m = re.fullmatch(r'/api/audio/([a-f0-9]{32})', path)
        if m:
            media = job_audio_file(m.group(1))
            if not media:
                return self.send_json({'error': 'Audio unavailable for this job'}, 404)
            return self.send_audio(media)
        m = re.fullmatch(r'/api/preview/([a-f0-9]{32})', path)
        if m:
            jid = m.group(1)
            kind = parse_qs(urlsplit(self.path).query).get('kind', ['result'])[0]
            if kind not in ('result', 'transcript', 'partial'):
                return self.send_json({'error': 'Invalid preview type'}, 400)
            stored = JOBS / f'{jid}.{kind}'
            if jid not in JOBS_STATE or not stored.is_file():
                return self.send_json({'error': 'Preview unavailable'}, 404)
            with stored.open('r', encoding='utf-8', errors='replace') as stream:
                content = stream.read(MAX_PREVIEW + 1)
            return self.send_json({'text': content[:MAX_PREVIEW],
                                   'truncated': len(content) > MAX_PREVIEW, 'kind': kind})
        m = re.fullmatch(r'/api/download/([a-f0-9]{32})', path)
        if m:
            jid = m.group(1)
            item = JOBS_STATE.get(jid)
            kind = parse_qs(urlsplit(self.path).query).get('kind', ['result'])[0]
            if kind not in ('result', 'transcript', 'partial'):
                return self.send_json({'error': 'Invalid download type'}, 400)
            pathfile = JOBS / f'{jid}.{kind}'
            if not item or (kind == 'result' and item['status'] != 'done') or not pathfile.is_file():
                return self.send_json({'error': 'Download not available'}, 404)
            name = re.sub(r'[^a-zA-Z0-9._-]', '_', item['filename'])
            if kind == 'transcript':
                name = 'original-asr-' + name
            elif kind == 'partial':
                name = 'partial-' + name
            return self.send_bytes(pathfile.read_bytes(), 'application/octet-stream',
                                   disposition=f'attachment; filename="{name}"')
        return self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        global BUSY_JOB
        if not self.trusted():
            return self.send_json({'error': 'Only LAN/Tailscale access is allowed'}, 403)
        request = urlsplit(self.path)
        path = request.path
        try:
            if path == '/api/login':
                body = self.body_json()
                ip = self.client_address[0]
                with LOCK:
                    attempts = [t for t in FAILURES.get(ip, []) if time.time() - t < 300]
                    FAILURES[ip] = attempts
                    if len(attempts) >= 8:
                        return self.send_json({'error': 'Too many attempts. Try again in 5 minutes.'}, 429)
                    if not password_ok(body.get('password', '')):
                        attempts.append(time.time())
                        return self.send_json({'error': 'Incorrect password'}, 401)
                    FAILURES[ip] = []
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(32)
                    SESSIONS[token] = {'expiry': time.time() + 43200, 'csrf': csrf}
                    cookie = f'plamo_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'
                    return self.send_json({'ok': True, 'csrf': csrf}, cookie=cookie)
            session = self.session()
            if not session:
                return self.send_json({'error': 'Login required'}, 401)
            if not self.csrf_ok(session):
                return self.send_json({'error': 'Security token mismatch'}, 403)
            if path == '/api/upload':
                opts = parse_qs(request.query)
                mode = opts.get('mode', ['asr'])[0]
                fmt = opts.get('format', ['srt'])[0]
                source = opts.get('source', ['ja'])[0]
                target = opts.get('target', ['en'])[0]
                lang = opts.get('language', ['ja'])[0]
                asr_model = validate_model('asr', opts.get('asr_model', [''])[0])
                llm_model = validate_model('llm', opts.get('llm_model', [''])[0]) if mode == 'asr_translate' else None
                if mode not in ('asr', 'asr_translate') or fmt not in ('txt', 'srt', 'vtt'):
                    raise ValueError('Invalid ASR workflow or output format')
                if source not in ('ja', 'en') or target not in ('ja', 'en') or (mode == 'asr_translate' and source == target):
                    raise ValueError('Choose different translation languages')
                if lang not in ('ja', 'en', 'auto') or (mode == 'asr_translate' and lang == 'auto'):
                    raise ValueError('Choose a specific Japanese or English speech language for translation')
                name = unquote(self.headers.get('X-Upload-Name', 'audio.wav'))
                name = Path(name).name[:150]
                if not name.lower().endswith(('.wav', '.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.webm', '.mp4', '.mkv', '.mov')):
                    raise ValueError('Unsupported audio/video file extension')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_AUDIO:
                    raise ValueError('Audio must be between 1 byte and 100 MB')
                jid = uuid.uuid4().hex
                source_name = jid + Path(name).suffix.lower()
                pathfile = JOBS / source_name
                output_name = re.sub(r'[^a-zA-Z0-9._-]', '_', Path(name).stem[:80]) + (
                    '.translated.' if mode == 'asr_translate' else '.transcript.') + fmt
                params = {'mode': mode, 'format': fmt, 'asr_model': asr_model, 'llm_model': llm_model,
                          'source': source, 'target': target, 'language': lang}
                create_job(jid, name, output_name, mode, params, source_name, 'uploading')
                try:
                    with pathfile.open('wb') as output:
                        remains = size
                        while remains:
                            data = self.rfile.read(min(remains, 1024 * 1024))
                            if not data:
                                raise RuntimeError('Upload interrupted')
                            output.write(data)
                            remains -= len(data)
                except Exception:
                    pathfile.unlink(missing_ok=True)
                    set_progress(jid, status='error', message='Upload interrupted')
                    raise
                set_progress(jid, status='queued', message='Saved. Waiting in queue...')
                JOB_QUEUE.put(jid)
                return self.send_json({'id': jid, 'saved': True})
            body = self.body_json()
            if path in ('/api/start', '/api/stop'):
                which = body.get('which', 'llm')
                if which not in ('llm', 'asr'):
                    raise ValueError('Unknown server')
                if BUSY_JOB or JOB_QUEUE.qsize():
                    raise ValueError('Jobs are processing or queued. Finish them before changing servers.')
                if path.endswith('start'):
                    model = validate_model(which, body.get('model'))
                    start_backend(which, model)
                else:
                    if not running_owned(which):
                        raise ValueError('Server not started by this app; cannot stop it')
                    stop_backend(which)
                return self.send_json({'ok': True})
            if path == '/api/translate':
                text = body.get('text', '')
                if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_TEXT:
                    raise ValueError('Text must not be empty or larger than 10 MB')
                source, target = body.get('source', 'ja'), body.get('target', 'en')
                if source not in ('ja', 'en') or target not in ('ja', 'en') or source == target:
                    raise ValueError('Choose different translation languages')
                kind = body.get('kind', 'txt')
                if kind not in ('txt', 'srt', 'vtt'):
                    raise ValueError('Only TXT/SRT/VTT text files are supported')
                llm_model = validate_model('llm', body.get('llm_model'))
                filename = re.sub(r'[^a-zA-Z0-9._-]', '_', str(body.get('filename') or 'text')[-90:])
                output_name = re.sub(r'\.(txt|srt|vtt)$', '', filename, flags=re.I) + '.' + target + '.' + kind
                jid = uuid.uuid4().hex
                source_name = jid + '.input.txt'
                (JOBS / source_name).write_text(text, encoding='utf-8')
                params = {'mode': 'translate', 'kind': kind, 'source': source,
                          'target': target, 'llm_model': llm_model}
                create_job(jid, filename, output_name, 'translate', params, source_name)
                JOB_QUEUE.put(jid)
                return self.send_json({'id': jid, 'saved': True})
            if path == '/api/cancel':
                jid = str(body.get('id') or BUSY_JOB or '')
                with LOCK:
                    state = JOBS_STATE.get(jid)
                    if not state or state['status'] in ('done', 'error', 'cancelled'):
                        raise ValueError('No cancellable job selected')
                    state['cancel'] = True
                    if state['status'] == 'queued':
                        set_progress(jid, status='cancelled', message='Cancelled before processing')
                        (JOBS / JOB_PARAMS[jid]['source_file']).unlink(missing_ok=True)
                    else:
                        persist_job(jid)
                return self.send_json({'ok': True})
            if path == '/api/delete':
                jid = str(body.get('id', ''))
                if not re.fullmatch(r'[a-f0-9]{32}', jid):
                    raise ValueError('Invalid job')
                with LOCK:
                    state = JOBS_STATE.get(jid)
                    if state is None:
                        return self.send_json({'error': 'Job not found'}, 404)
                    if state['status'] not in ('done', 'error', 'cancelled'):
                        raise ValueError('Cancel or finish the job before deleting it')
                    params = JOB_PARAMS.pop(jid)
                    JOBS_STATE.pop(jid)
                    for suffix in ('.json', '.result', '.transcript', '.partial', '.audio.mp3'):
                        (JOBS / (jid + suffix)).unlink(missing_ok=True)
                    (JOBS / params['source_file']).unlink(missing_ok=True)
                return self.send_json({'ok': True})
            if path == '/api/stopweb':
                if BUSY_JOB or JOB_QUEUE.qsize():
                    raise ValueError('Jobs are still running/queued. Close the browser instead; processing will continue.')
                self.send_json({'ok': True})
                threading.Thread(target=lambda: (time.sleep(.4), os.kill(os.getpid(), signal.SIGTERM)), daemon=True).start()
                return
            if path == '/api/poweroff':
                if BUSY_JOB or JOB_QUEUE.qsize():
                    raise ValueError('Jobs are still processing. Shut down after they finish.')
                if body.get('confirmation') != 'SHUTDOWN' or not password_ok(body.get('password', '')):
                    return self.send_json({'error': 'Re-enter password and type SHUTDOWN'}, 403)
                poweroff_pc()
                return self.send_json({'ok': True, 'message': 'Shutdown requested'})
            if path == '/api/logout':
                cookie = SimpleCookie()
                cookie.load(self.headers.get('Cookie', ''))
                token = cookie.get('plamo_session')
                if token:
                    SESSIONS.pop(token.value, None)
                return self.send_json({'ok': True}, cookie='plamo_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            return self.send_json({'error': 'Not found'}, 404)
        except Exception as exc:
            return self.send_json({'error': str(exc)}, 400)


def exit_handler(*_):
    for kind in ('asr', 'llm'):
        stop_backend(kind)
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, exit_handler)
    signal.signal(signal.SIGINT, exit_handler)
    recover_jobs()
    start_queued_worker()
    server = ThreadingHTTPServer(('0.0.0.0', WEB_PORT), Handler)
    server.daemon_threads = True
    print(f'Local AI hub listening on port {WEB_PORT}', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        for kind in ('asr', 'llm'):
            stop_backend(kind)
