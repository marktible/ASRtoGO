#!/usr/bin/env python3
"""CPU affinity and memory aware build/runtime defaults, without extra packages."""
import json
import os
from pathlib import Path


def detect():
    allowed = os.sched_getaffinity(0) if hasattr(os, 'sched_getaffinity') else set(range(os.cpu_count() or 1))
    physical = set()
    for cpu in allowed:
        topology = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        try:
            physical.add(((topology / 'physical_package_id').read_text().strip(),
                          (topology / 'core_id').read_text().strip()))
        except OSError:
            physical.add(('unknown', str(cpu)))
    memory = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    available = memory.get('MemAvailable', memory.get('MemTotal', 2 * 1024**3))
    # Respect the usual cgroup v2 memory cap when building in a container.
    try:
        cap = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        used = int(Path('/sys/fs/cgroup/memory.current').read_text().strip())
        if cap != 'max':
            available = min(available, max(0, int(cap) - used))
    except (OSError, ValueError):
        pass
    jobs = max(1, min(len(allowed), (available - 1024**3) // (2 * 1024**3)))
    return {'threads': max(1, len(physical)), 'logical_cpus': len(allowed),
            'build_jobs': jobs, 'available_memory_mb': available // 1024**2}


if __name__ == '__main__':
    print(json.dumps(detect()))
