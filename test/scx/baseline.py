#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Exercise upstream scx_qmap in a disposable full-system VM."""

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

SCX = Path('/sys/kernel/sched_ext')
CGROUP = Path('/sys/fs/cgroup')
BTF = Path('/sys/kernel/btf/vmlinux')


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def probe(path):
    try:
        dump = command('bpftool', 'btf', 'dump', 'file', path, 'format', 'raw')
    except (OSError, subprocess.CalledProcessError) as exc:
        return {'hierarchy': 'unknown', 'reason': str(exc)}
    marker = " FUNC 'scx_bpf_sub_dispatch' "
    return {'hierarchy': 'supported' if marker in dump else 'unsupported', 'btf': str(path)}


def wait_until(predicate, description):
    deadline = time.monotonic() + 20
    while not predicate():
        if time.monotonic() > deadline:
            raise RuntimeError(f'Timed out waiting for {description}')
        time.sleep(0.05)


def terminate(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise RuntimeError(f'Loader {process.pid} required SIGKILL')


def exercise(loader, report):
    if subprocess.run(['systemd-detect-virt', '--container', '--quiet']).returncode == 0:
        raise RuntimeError('A disposable full-system VM is required')
    command('systemd-detect-virt', '--vm')
    if (SCX / 'state').read_text().strip() != 'disabled' or (SCX / 'root').exists():
        raise RuntimeError('A scheduler is already attached')
    if not (CGROUP / 'cgroup.controllers').is_file():
        raise RuntimeError('The unified cgroup hierarchy is required')

    # Hold both branches live while testing the intermediate, grandchild and inheriting leaf.
    first = f'scxbase{os.getpid()}'
    second = f'scxsibling{os.getpid()}'
    names = [f'{first}.slice', f'{first}-build.slice', f'{first}-build-ci.slice', f'{second}.slice']
    paths = {}
    logs = {}
    processes = {}
    attachment = {'/': SCX / 'root'}
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        for name, top in ((names[2], names[0]), (names[3], names[3])):
            worker = name.removesuffix('.slice') + '.service'
            command('systemd-run', '--quiet', '--collect', '--unit', worker, '--slice', name,
                    '-p', 'CPUWeight=100', '/usr/bin/sleep', 'infinity')
            stack.callback(command, 'systemctl', 'stop', top)
        for name in names:
            paths[name] = CGROUP / command('systemctl', 'show', '-P', 'ControlGroup', name).lstrip('/')
        intermediate, grandchild, leaf, sibling = names
        # qmap can only delegate exclusive CPUs. Reserve enough for the intermediate
        # scheduler to retain whole CPUs when it hands some to its own child.
        (paths[intermediate] / 'cpu.weight').write_text('300')
        (paths[sibling] / 'cpu.weight').write_text('50')
        report['cgroups'] = {
            n: {'path': str(p), 'id': p.stat().st_ino, 'cpu_weight': int((p / 'cpu.weight').read_text())}
            for n, p in paths.items()
        }
        for name, parent in ((sibling, '/'), (intermediate, '/'), (grandchild, intermediate)):
            attachment[name] = attachment[parent] / 'sub' / f'sub-{paths[name].stat().st_ino}'

        def check_loaders():
            for key, process in processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f'{key}: loader exited: {logs[key].read_text(errors="replace")[-5000:]}')

        for name, target in attachment.items():
            logs[name] = Path(tmp) / ('root.log' if name == '/' else name + '.log')
            with logs[name].open('wb') as f:
                args = [loader, '-i', '1']
                if name != '/':
                    args += ['-c', paths[name]]
                process = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT)
            processes[name] = process
            stack.callback(terminate, process)

            def attached():
                check_loaders()
                return (target / 'ops').is_file() and 'scx_qmap started:' in logs[name].read_text(errors='replace')

            wait_until(attached, f'{name} at {target}')
        time.sleep(2)
        check_loaders()
        observed = set((SCX / 'root').rglob('ops'))
        expected = {p / 'ops' for p in attachment.values()}
        if observed != expected:
            raise RuntimeError(f'Unexpected kernel attachment tree: {observed ^ expected}')
        report['observed_unix_ns'] = time.time_ns()
        report['attachments'] = {
            n: {'sysfs': str(p), 'ops': (p / 'ops').read_text().strip(), 'pid': processes[n].pid}
            for n, p in attachment.items()
        }
        ancestor = next(p for p in paths[leaf].parents if p in (paths[n] for n in names if n in attachment))
        if ancestor != paths[grandchild]:
            raise RuntimeError(f'Unexpected nearest scheduler for {leaf}: {ancestor}')

        report['inheritance'] = {'leaf': leaf, 'direct_attachments': 0, 'nearest_ancestor': grandchild}
    wait_until(lambda: not (SCX / 'root').exists() and (SCX / 'state').read_text().strip() == 'disabled',
               'kernel detach')
    wait_until(lambda: not any(p.exists() for p in paths.values()), 'test cgroups to be removed')
    report['teardown'] = {'state': 'disabled', 'cgroups_removed': True,
                          'loader_exit': {k: p.returncode for k, p in processes.items()}}
    if any(p.returncode for p in processes.values()):
        raise RuntimeError(f'Loader teardown failed: {report["teardown"]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='only inspect kernel BTF')
    parser.add_argument('--btf', type=Path, default=BTF, help='input for --probe')
    parser.add_argument('--loader', type=Path)
    parser.add_argument('--image-sha256')
    parser.add_argument('--provider-revision')
    parser.add_argument('--repo-revision')
    args = parser.parse_args()
    if args.btf != BTF and not args.probe:
        parser.error('--btf requires --probe')
    report = probe(args.btf)
    report['kernel'] = release = platform.release()
    if args.probe or report['hierarchy'] != 'supported':
        print(json.dumps(report, sort_keys=True))
        return {'supported': 0, 'unsupported': 77, 'unknown': 1}[report['hierarchy']]
    if not args.loader:
        parser.error('the baseline needs a loader')
    report.update(btf_sha256=sha256(BTF), bpftool=command('bpftool', 'version').splitlines()[0],
                  harness_sha256=sha256(Path(__file__)), loader_sha256=sha256(args.loader),
                  cpus_online=Path('/sys/devices/system/cpu/online').read_text().strip(),
                  vcpu_count=os.cpu_count())
    if args.image_sha256:
        if not args.provider_revision or not args.repo_revision:
            parser.error('the reference image requires provider and systemd revisions')
        config = Path('/boot') / f'config-{release}'
        report.update(image_sha256=args.image_sha256, provider_revision=args.provider_revision,
                      systemd_revision=args.repo_revision, installed_systemd=command('systemctl', '--version').splitlines()[0],
                      cpu_topology=json.loads(command('lscpu', '--json', '--extended=CPU,CORE,SOCKET,NODE'))['cpus'],
                      toolchain=command('clang-23', '--version').splitlines()[0],
                      kernel_sha256=sha256(Path('/boot') / f'vmlinuz-{release}'), kernel_config_sha256=sha256(config),
                      kernel_config=[line for line in config.read_text().splitlines() if line.startswith(
                          ('CONFIG_SCHED_CLASS_EXT=', 'CONFIG_EXT_SUB_SCHED=', 'CONFIG_DEBUG_INFO_BTF='))])
    try:
        exercise(args.loader, report)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        report['error'] = str(exc)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int('error' in report)


if __name__ == '__main__':
    sys.exit(main())
