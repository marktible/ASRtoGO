#!/usr/bin/env python3
"""Reject missing Vulkan devices and software-only Vulkan installations."""
import re
import subprocess
import sys

try:
    result = subprocess.run(['vulkaninfo', '--summary'], capture_output=True, text=True, timeout=30)
except (OSError, subprocess.TimeoutExpired) as error:
    raise SystemExit(f'Cannot check Vulkan: {error}')
if result.returncode:
    print(result.stderr.strip(), file=sys.stderr)
    raise SystemExit('No usable Vulkan device. Check your GPU driver and device access, then rerun setup.')
types = re.findall(r'deviceType\s*=\s*(\S+)', result.stdout)
if not types:
    raise SystemExit('vulkaninfo did not report any Vulkan device types. Check the Vulkan installation.')
if not any(kind in ('PHYSICAL_DEVICE_TYPE_DISCRETE_GPU', 'PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU',
                    'PHYSICAL_DEVICE_TYPE_VIRTUAL_GPU') for kind in types):
    raise SystemExit('Only software/unsupported Vulkan devices were found. Install the Vulkan driver for your GPU.')
print('Vulkan GPU available.')
