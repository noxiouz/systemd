#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Install and run the disposable root-scheduler cold-boot fixture."""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path('/opt/scxboot')
PAYLOAD = BASE / 'payload'
MOUNT = Path('/scxloader')
SCX = Path('/sys/kernel/sched_ext')
GEN = Path('/etc/systemd/system-generators/scxboot-generator')
ROOT_UNIT = 'scxboot@-.scheduler'


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def marker(kind, **fields):
    msg = dict(event=kind, usec=time.monotonic_ns() // 1000,
               boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), **fields)
    with Path('/dev/console').open('w') as console:
        print('SCXBOOT ' + json.dumps(msg, sort_keys=True), file=console, flush=True)


def metadata(image_sha256):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from baseline import BTF, probe, sha256
    release = platform.release()
    config = Path('/boot') / f'config-{release}'
    result = dict(kernel=release, image_sha256=image_sha256,
                  kernel_sha256=sha256(config.with_name(f'vmlinuz-{release}')), kernel_config_sha256=sha256(config),
                  btf_sha256=sha256(BTF), probe=probe(BTF), manager=os.readlink('/proc/1/exe'),
                  kernel_config=[s for s in config.read_text().splitlines()
                                 if s.startswith(('CONFIG_SCHED_CLASS_EXT=', 'CONFIG_EXT_SUB_SCHED=', 'CONFIG_DEBUG_INFO_BTF='))])
    if result['probe']['hierarchy'] != 'supported':
        raise RuntimeError('The positive guest must have inspectable hierarchy support')
    print(json.dumps(result, indent=2))


def install(provider):
    subprocess.run(['systemd-detect-virt', '--vm', '--quiet'], check=True)
    if BASE.exists() or GEN.exists() or MOUNT.exists():
        raise RuntimeError('The boot fixture already exists on this guest')
    PAYLOAD.mkdir(parents=True)
    MOUNT.mkdir()
    GEN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(provider, PAYLOAD / 'provider')
    for dest in (BASE / 'guest', PAYLOAD / 'wait-loader'):
        shutil.copy2(__file__, dest)
        dest.chmod(0o755)
    GEN.symlink_to(BASE / 'guest')


def generate(out):
    if Path('/etc/initrd-release').exists():
        return
    cases = [a.split('=', 1)[1] for a in Path('/proc/cmdline').read_text().split() if a.startswith('scx.case=')]
    if len(cases) != 1 or cases[0] not in ('delay', 'missing', 'control'):
        return
    case = cases[0]
    if case != 'control':
        put(out / '-.slice.d/50-scxboot.conf', '[Slice]\nScheduler=scxboot@.scheduler\n')
        loader = 'missing-provider' if case == 'missing' else 'wait-loader'
        put(out / 'scxboot@.scheduler', f'[Unit]\nRequiresMountsFor={MOUNT}\n'
            f'[Scheduler]\nExecStart={MOUNT}/{loader}\nTimeoutStartSec=8s\n')
        put(out / 'scxloader.mount', f'[Unit]\nDefaultDependencies=no\n'
            f'[Mount]\nWhat={PAYLOAD}\nWhere={MOUNT}\nType=none\nOptions=bind\n')
    put(out / 'scxboot-workload.service', '[Unit]\nDescription=SCX cold boot workload\nDefaultDependencies=no\n'
        f'[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={BASE}/guest workload\n')
    put(out / 'scxboot.target', '[Unit]\nDescription=SCX cold boot target\nDefaultDependencies=no\n'
        'Requires=scxboot-workload.service\nAfter=scxboot-workload.service\n')


def wait_loader():
    mounted = os.path.ismount(MOUNT) or any(l.split()[4] == str(MOUNT) for l in Path('/proc/self/mountinfo').read_text().splitlines())
    if not mounted:
        raise RuntimeError('The loader prerequisite is missing')
    marker('loader-delayed', mount=str(MOUNT), generation=sys.argv[1])
    time.sleep(1.5)
    marker('loader-released')
    os.execv(MOUNT / 'provider', [str(MOUNT / 'provider'), *sys.argv[1:]])


def workload():
    start = time.monotonic_ns() // 1000
    marker('workload-started', first_usec=start)
    ops = SCX / 'root/ops'
    state = (SCX / 'state').read_text().strip()
    ctl = shutil.which('systemctl', path='/usr/local/bin:/usr/bin')
    enter = '0'
    if ops.exists():
        enter = subprocess.check_output([ctl, 'show', '-P', 'ActiveEnterTimestampMonotonic', ROOT_UNIT], text=True).strip()
    marker('workload-finished', first_usec=start, kernel_state=state, ops=ops.read_text().strip() if ops.exists() else '',
           active_enter_usec=int(enter), manager=os.readlink('/proc/1/exe'))


if __name__ == '__main__':
    if Path(sys.argv[0]) == GEN:
        generate(Path(sys.argv[1]))
    elif Path(sys.argv[0]).name == 'wait-loader':
        wait_loader()
    elif sys.argv[1:] == ['workload']:
        workload()
    elif len(sys.argv) == 3 and sys.argv[1] == 'metadata':
        metadata(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'install':
        install(sys.argv[2])
    else:
        sys.exit(f'Usage: {sys.argv[0]} install /path/to/provider | metadata IMAGE_SHA256')
