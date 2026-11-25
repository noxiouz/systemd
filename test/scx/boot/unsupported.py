#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Attempt a real child attachment on a guest with root-only sched_ext."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baseline import BTF, SCX, probe, sha256

RUNTIME = Path('/run/systemd/system')
MARK = Path('/run/scxneg-workload-marker')
ROOT = 'scxnegroot@-.scheduler'
CHILD = 'scxnegchild@scxneg.scheduler'
WORKLOAD = 'scxneg-workload.service'


def run(*args, check=True):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)
    return p.returncode, p.stdout.strip()


def test(provider, image_digest):
    run('systemd-detect-virt', '--vm', '--quiet')
    release = platform.release()
    config = Path('/boot') / f'config-{release}'
    result = dict(
        kernel=release,
        image_sha256=image_digest,
        kernel_sha256=sha256(config.with_name(f'vmlinuz-{release}')),
        kernel_config_sha256=sha256(config),
        btf_sha256=sha256(BTF),
        probe=probe(BTF),
        manager=os.readlink('/proc/1/exe'),
        kernel_config=[
            s
            for s in config.read_text().splitlines()
            if 'CONFIG_SCHED_CLASS_EXT' in s
            or 'CONFIG_EXT_SUB_SCHED' in s
            or s.startswith('CONFIG_DEBUG_INFO_BTF=')
        ],
    )
    if result['probe']['hierarchy'] != 'unsupported' or (SCX / 'state').read_text().strip() != 'disabled':
        raise RuntimeError(
            'This test requires an idle stock kernel with readable BTF and root-only sched_ext'
        )

    units = {
        '-.slice.d/70-scxneg.conf': '[Slice]\nScheduler=scxnegroot@.scheduler\n',
        'scxnegroot@.scheduler': f'[Scheduler]\nExecStart={provider}\nTimeoutStartSec=5s\n',
        'scxneg.slice': '[Slice]\nScheduler=scxnegchild@.scheduler\n',
        'scxnegchild@.scheduler': '[Scheduler]\nExecStart=/usr/bin/false\nTimeoutStartSec=5s\n',
        WORKLOAD: f'[Service]\nSlice=scxneg.slice\nType=oneshot\nExecStart=/usr/bin/touch {MARK}\n',
    }
    if MARK.exists() or any((RUNTIME / name).exists() for name in units):
        raise RuntimeError('Test namespace already in use')
    ctl = shutil.which('systemctl', path='/usr/local/bin:/usr/bin')
    try:
        for name, body in units.items():
            path = RUNTIME / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        run(ctl, 'daemon-reload')
        run(ctl, 'start', ROOT)
        result['root_state'] = run(ctl, 'is-active', ROOT)[1]
        result['root_ops'] = (SCX / 'root/ops').read_text().strip()
        assert result['root_ops'] == 'simple' and not (SCX / 'root/sub').exists()
        since = time.time()
        code, diagnostic = run(ctl, 'start', WORKLOAD, check=False)
        invocation = run(ctl, 'show', '-P', 'InvocationID', CHILD)[1]
        run('journalctl', '--sync')
        _, messages = run(
            'journalctl',
            '-b',
            '--no-pager',
            '-o',
            'cat',
            '--since',
            f'@{since:f}',
            '_PID=1',
            f'UNIT={CHILD}',
        )
        result.update(
            start_exit_status=code,
            diagnostic=diagnostic,
            invocation=invocation,
            messages=messages,
            child_state=run(ctl, 'show', '-P', 'ActiveState', CHILD)[1],
            workload_markers=int(MARK.exists()),
            parent_after=run(ctl, 'is-active', ROOT)[1],
        )
        if not (
            code != 0
            and result['child_state'] == 'failed'
            and 'Operation not supported' in messages
            and not MARK.exists()
            and result['parent_after'] == 'active'
        ):
            raise RuntimeError(f'Child did not produce the required unsupported failure: {result}')
    finally:
        run(ctl, 'stop', CHILD, ROOT, 'scxneg.slice', check=False)
        for name in units:
            (RUNTIME / name).unlink(missing_ok=True)
        MARK.unlink(missing_ok=True)
        run(ctl, 'daemon-reload')
    result['teardown'] = (SCX / 'state').read_text().strip()
    assert result['teardown'] == 'disabled'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider', type=Path)
    parser.add_argument('--image-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(test(args.provider.resolve(strict=True), args.image_sha256), indent=2))
