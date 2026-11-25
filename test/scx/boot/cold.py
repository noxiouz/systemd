#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Cold-boot the installed fixture on independent QEMU disk overlays and audit its serial console."""

import argparse
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

CSI = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
RECOVERY = (
    "test -f /etc/initrd-release && test -f /sysroot/etc/os-release && "
    "printf 'SCXBOOT_%s\\n' INITRD_CONSOLE; "
    "mkdir -p /sysroot/etc/systemd/system/-.slice.d && "
    "printf '[Slice]\\nScheduler=\\n' > /sysroot/etc/systemd/system/-.slice.d/99-recovery.conf && "
    "printf 'SCXBOOT_%s\\n' OVERRIDE_WRITTEN; exit\n"
)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def recovery_initrd(stock, bash, directory):
    # Append a userspace-only newc archive to the distribution initrd. Its stock
    # loader, libc and libtinfo already support the shell from this same guest.
    archive = directory / 'initrd-with-bash'
    member = directory / 'usr/bin/bash'
    member.parent.mkdir(parents=True)
    shutil.copyfile(bash, member)
    member.chmod(0o755)
    shutil.copyfile(stock, archive)
    with archive.open('ab') as out:
        out.write(bytes((-out.tell()) % 4))
        subprocess.run(
            ['cpio', '-o', '-H', 'newc', '--owner', '0:0'],
            cwd=directory,
            input=b'usr/bin/bash\n',
            stdout=out,
            check=True,
        )
    return archive


def boot(args, case, overlay, console_path, initrd):
    spec = 'missing' if case == 'recovery' else case
    cmdline = (
        f'root={args.root} rw console=ttyAMA0 systemd.log_target=console systemd.show_status=yes '
        f'systemd.status_unit_format=name systemd.unit=scxboot.target scx.case={spec}'
    )
    if case == 'recovery':
        cmdline += ' rd.systemd.break=pre-switch-root'
    accel = args.accel
    cmd = [
        'qemu-system-aarch64',
        '-machine',
        f'virt,accel={accel}',
        '-cpu',
        'max' if accel == 'tcg' else 'host',
        '-smp',
        str(args.cpus),
        '-m',
        '4G',
        '-display',
        'none',
        '-nodefaults',
        '-no-reboot',
        '-serial',
        'stdio',
        '-monitor',
        'none',
        '-kernel',
        args.kernel,
        '-initrd',
        initrd,
        '-append',
        cmdline,
        '-drive',
        f'file={overlay},format=qcow2,if=none,id=osdisk',
        '-device',
        'virtio-blk-pci,drive=osdisk',
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    raw = bytearray()
    shell_command = False
    failure_began = None
    deadline = time.monotonic() + args.boot_timeout
    try:
        with selectors.DefaultSelector() as selector, console_path.open('wb') as f:
            selector.register(p.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(timeout=0.5):
                    continue
                block = os.read(p.stdout.fileno(), 65536)
                if not block:
                    break
                f.write(block)
                f.flush()
                raw.extend(block)
                clean = CSI.sub(b'', raw).decode(errors='replace')
                if (
                    case == 'recovery'
                    and not shell_command
                    and ('bash-' in clean or 'sh-' in clean or ':/#' in clean)
                ):
                    p.stdin.write(RECOVERY.encode())
                    p.stdin.flush()
                    shell_command = True
                if case == 'missing':
                    if 'Starting scxboot@-.scheduler' in clean and failure_began is None:
                        failure_began = time.monotonic()
                    if 'Dependency failed for scxboot.target' in clean and re.search(
                        r'missing-provider[^\r\n]*No such file or directory', clean
                    ):
                        break
                elif 'Reached target scxboot.target' in clean:
                    break
            else:
                raise RuntimeError(f'{case}: boot timeout; inspect {console_path}')
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    clean = CSI.sub(b'', raw).decode(errors='replace')
    events = [json.loads(s) for s in re.findall(r'SCXBOOT (\{[^\r\n]*\})', clean)]
    journal = [s.strip() for s in clean.splitlines() if 'scxboot' in s or 'SCXBOOT_' in s]
    result = dict(case=case, events=events, console_sha256=file_digest(console_path))
    if case == 'missing':
        diagnostics = [s for s in journal if ('failed' in s.lower() or 'no such file' in s.lower())]
        if (
            failure_began is None
            or not diagnostics
            or 'Dependency failed for scxboot-workload.service' not in clean
            or not any('scxboot@-.scheduler' in s and 'failed' in s.lower() for s in diagnostics)
            or events
        ):
            raise RuntimeError(
                f'{case}: expected a bounded failed dependency and zero command markers: {journal}'
            )
        elapsed = time.monotonic() - failure_began
        if elapsed > 8:
            raise RuntimeError(f'{case}: exceeded 8s scheduler startup timeout: {elapsed:.3f}s')
        result.update(
            failure_seconds=round(elapsed, 3), timeout_seconds=8, diagnostic=diagnostics, workload_markers=0
        )
    else:
        by_name = {entry['event']: entry for entry in events}
        first = by_name['workload-started']['first_usec']
        done = by_name['workload-finished']
        assert 'Reached target scxboot.target' in clean and first == done['first_usec']
        if case == 'delay':
            enter = done['active_enter_usec']
            assert (
                0 < by_name['loader-delayed']['usec'] < by_name['loader-released']['usec'] < enter <= first
            )
            assert by_name['loader-released']['usec'] - by_name['loader-delayed']['usec'] >= 1_500_000
            assert done['kernel_state'] == 'enabled' and done['ops'] == 'qmap'
        else:
            assert done['kernel_state'] == 'disabled' and done['active_enter_usec'] == 0
            assert not any(e['event'].startswith('loader') for e in events)
        if case == 'recovery':
            assert (
                shell_command and 'SCXBOOT_INITRD_CONSOLE' in clean and 'SCXBOOT_OVERRIDE_WRITTEN' in clean
            )
            result['recovery'] = ['initrd-console', 'host-root-mounted', 'late-root-override', 'switch-root']
        result['target'] = 'scxboot.target'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--metadata', type=Path, required=True, help='positive kernel provenance from guest.py metadata'
    )
    parser.add_argument(
        '--disk',
        type=Path,
        required=True,
        help='offline prepared guest disk, opened read-only as a backing image',
    )
    parser.add_argument('--format', choices=['qcow2', 'raw'], required=True)
    parser.add_argument('--kernel', type=Path, required=True)
    parser.add_argument('--initrd', type=Path, required=True)
    parser.add_argument(
        '--bash', type=Path, required=True, help='stock /usr/bin/bash from the same root filesystem'
    )
    parser.add_argument(
        '--root', required=True, help='root partition identifier from the original guest command line'
    )
    parser.add_argument(
        '--logs', type=Path, required=True, help='new or empty directory for complete serial transcripts'
    )
    parser.add_argument(
        '--accel',
        choices=['hvf', 'kvm', 'tcg'],
        default='hvf'
        if platform.system() == 'Darwin'
        else 'kvm'
        if os.access('/dev/kvm', os.W_OK)
        else 'tcg',
    )
    parser.add_argument('--boot-timeout', type=int, default=300)
    parser.add_argument('--cpus', type=int, default=8, help='number of vCPUs for each cold-boot guest')
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    if any(args.logs.iterdir()):
        parser.error('--logs must be empty')
    for command in ('qemu-img', 'qemu-system-aarch64', 'cpio'):
        if not shutil.which(command):
            parser.error(f'{command} missing')
    metadata = json.loads(args.metadata.read_text())
    if (
        metadata['kernel_sha256'] != file_digest(args.kernel)
        or metadata['probe']['hierarchy'] != 'supported'
    ):
        parser.error('guest metadata must describe this kernel with hierarchy support')
    result = dict(guest=metadata, vcpu_count=args.cpus, initrd_sha256=file_digest(args.initrd), cases=[])
    with tempfile.TemporaryDirectory() as tmp:
        repaired_initrd = recovery_initrd(args.initrd, args.bash, Path(tmp))
        result.update(
            recovery_initrd_sha256=file_digest(repaired_initrd), bash_sha256=file_digest(args.bash)
        )
        for case in ('delay', 'missing', 'recovery', 'control'):
            overlay = Path(tmp) / (case + '.qcow2')
            subprocess.run(
                [
                    'qemu-img',
                    'create',
                    '-q',
                    '-f',
                    'qcow2',
                    '-F',
                    args.format,
                    '-b',
                    args.disk.resolve(strict=True),
                    overlay,
                ],
                check=True,
            )
            result['cases'].append(
                boot(
                    args,
                    case,
                    overlay,
                    args.logs / (case + '.log'),
                    repaired_initrd if case == 'recovery' else args.initrd,
                )
            )
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
