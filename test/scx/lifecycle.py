#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Exercise native .scheduler units on a disposable stock-kernel VM booted with the patched system manager."""

import array
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

SCX = Path('/sys/kernel/sched_ext')
CG = Path('/sys/fs/cgroup')
UNITS = Path('/run/systemd/system')
ROOT = 'scxlifecycle@-.scheduler'
CHILD = 'scxlifecycle@scxlifecycle.scheduler'
TARGET = CG / 'scxlifecycle.slice'


def ctl(*args, check=True):
    result = subprocess.run(['systemctl', *args], text=True, capture_output=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError(f'systemctl {args}: {result.stderr.strip()}')
    return result


def state(unit):
    return ctl('show', '-P', 'ActiveState', unit).stdout.strip()


def wait_for(fn, desc):
    deadline = time.monotonic() + 12
    while not fn():
        if time.monotonic() >= deadline:
            raise RuntimeError(f'timed out waiting for {desc}')
        time.sleep(.02)


def packet(sock):
    data, ancillary, flags, _ = sock.recvmsg(2048, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
    if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
        raise RuntimeError('truncated packet')
    fds = array.array('i')
    for level, typ, contents in ancillary:
        if (level, typ) != (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            raise RuntimeError('unknown control message')
        fds.frombytes(contents)
    return data, fds


def send(sock, data, fds):
    return sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)] if fds else [])


def proxy(binary, directory, generation, fd, role):
    directory = Path(directory)
    fault = json.loads((directory / 'action').read_text()) if role == 'child' else 'normal'
    target_fd = int(fd)
    with ExitStack() as stack:
        if fault == 'wrong':
            target_fd = os.open(directory / 'wrong-target', os.O_PATH | os.O_DIRECTORY)
            stack.callback(os.close, target_fd)
        mgr = stack.enter_context(socket.socket(fileno=0))
        channel, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        stack.enter_context(channel)
        stack.enter_context(child)
        proc = subprocess.Popen([binary, generation, str(target_fd), role], stdin=child, pass_fds=(target_fd,))
        child.close()
        (directory / f'{role}.pid').write_text(f'{os.getpid()} {proc.pid}')
        try:
            while True:
                ready, _, _ = select.select([mgr, channel], [], [], 5)
                if mgr in ready:
                    command = mgr.recv(1)
                    if not command or command == b'Q':
                        return
                    channel.send(command)
                if channel not in ready:
                    continue
                data, fds = packet(channel)
                with ExitStack() as release:
                    for descriptor in fds:
                        release.callback(os.close, descriptor)
                    if not data:
                        return
                    if data.startswith(b'attach ') and role == 'child':
                        (directory / 'received').write_text(generation)
                        if fault == 'die':
                            return
                        if fault == 'timeout':
                            # Remain responsive to stop, while withholding the attach confirmation.
                            continue
                        if fault in ('delay', 'recreate'):
                            wait_for(lambda: (directory / 'release').exists(), 'release')
                        if fault == 'stale':
                            data = data.replace(generation.encode(), (directory / 'previous').read_bytes(), 1)
                        if fault == 'hold' and fds:
                            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as holder:
                                holder.connect(str(directory / 'hold.socket'))
                                send(holder, b'H', fds)
                    send(mgr, data, fds)
        finally:
            channel.close()
            proc.wait(timeout=10)


@contextmanager
def foreign(binary):
    channel, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with channel, child, ExitStack() as cleanup:
        fd = os.open(TARGET, os.O_PATH | os.O_DIRECTORY)
        cleanup.callback(os.close, fd)
        proc = subprocess.Popen([binary, '0' * 32, str(fd), 'child'], stdin=child, pass_fds=(fd,))
        child.close()
        try:
            data, fds = packet(channel)
            for link in fds:
                cleanup.callback(os.close, link)
            assert len(fds) == 1 and data.startswith(b'attach ')
            yield fds[0]
        finally:
            channel.send(b'Q')
            proc.wait(timeout=10)


def link_id(fd):
    fields = dict(line.split(':\t', 1) for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
                  if ':\t' in line)
    assert fields['link_type'] == 'struct_ops'
    return int(fields['link_id'])


def exercise(binary, results):
    if os.getuid() != 0 or subprocess.run(['systemd-detect-virt', '--quiet', '--vm']).returncode or \
            not subprocess.run(['systemd-detect-virt', '--quiet', '--container']).returncode:
        raise RuntimeError('requires root on a dedicated full-system guest')
    if (SCX / 'state').read_text().strip() != 'disabled' or (SCX / 'root').exists():
        raise RuntimeError('an external scheduler is already present')
    with tempfile.TemporaryDirectory(prefix='scxlifecycle-', dir='/run') as directory, ExitStack() as files:
        d = Path(directory)

        def action(name):
            (d / 'action').write_text(json.dumps(name))
            (d / 'release').unlink(missing_ok=True)
            (d / 'received').unlink(missing_ok=True)

        def create(path, content):
            with path.open('x') as file:
                file.write(content)
            files.callback(path.unlink)

        def start():
            return ctl('start', '--no-block', CHILD)

        def wait_failed(case):
            wait_for(lambda: state(CHILD) == 'failed', case)
            invocation = ctl('show', '-P', 'InvocationID', CHILD).stdout.strip()
            subprocess.run(['journalctl', '--sync'], check=True, timeout=10)
            journal = subprocess.check_output(['journalctl', '-b', '--no-pager', '-o', 'json',
                                               '_PID=1', f'UNIT={CHILD}', f'INVOCATION_ID={invocation}'], text=True)
            failures = [entry['MESSAGE'] for line in journal.splitlines()
                        if (entry := json.loads(line)).get('ERRNO')]
            assert failures, f'missing failure record for {case}/{invocation}'
            results.append(dict(case=case, state=state(CHILD), invocation=invocation, failures=failures))

        def node():
            return SCX / 'root' / 'sub' / f'sub-{TARGET.stat().st_ino}'

        dropin = UNITS / '-.slice.d'
        if not dropin.exists():
            dropin.mkdir()
            files.callback(dropin.rmdir)
        create(dropin / '98-scxlifecycle.conf', '[Slice]\nScheduler=scxlifecycle@.scheduler\n')
        create(UNITS / 'scxlifecycle.slice', '[Slice]\nScheduler=scxlifecycle@.scheduler\n')
        create(UNITS / 'scxlifecycle@.scheduler',
               f'[Scheduler]\nExecStart=/usr/bin/python3 {Path(__file__).resolve()} proxy {binary} {d}\nTimeoutStartSec=4s\n')
        create(UNITS / 'scxshort.slice', '[Slice]\nScheduler=scxshort@.scheduler\n')
        create(UNITS / 'scxshort@.scheduler', f'[Scheduler]\nExecStart={binary}\nTimeoutStartSec=200ms\n')
        try:
            ctl('daemon-reload')
            assert state('scxlifecycle.slice') == 'inactive' and not TARGET.exists()
            action('delay')
            start()
            wait_for(lambda: (d / 'received').exists(), 'provider attachment')
            assert node().is_dir() and state(ROOT) == 'active' and state(CHILD) == 'activating'
            (d / 'release').touch()
            wait_for(lambda: state(CHILD) == 'active', 'native confirmation')
            (d / 'previous').write_bytes((d / 'received').read_bytes())
            results.append(dict(case='delayed-direct-start', target=TARGET.stat().st_ino, kernel_node=node().stat().st_ino))
            ctl('stop', CHILD)

            wrong = CG / d.name
            wrong.mkdir()
            files.callback(wrong.rmdir)
            (d / 'wrong-target').symlink_to(wrong)
            for fault in ('wrong', 'stale', 'die', 'timeout'):
                action(fault)
                start()
                wait_failed(fault)
                assert not node().exists()
                ctl('reset-failed', CHILD)

            action('recreate')
            start()
            wait_for(lambda: (d / 'received').exists(), 'pinned old target')
            old = TARGET.stat().st_ino
            TARGET.rmdir()
            TARGET.mkdir()
            assert old != TARGET.stat().st_ino
            (d / 'release').touch()
            wait_failed('recreated-target')
            ctl('reset-failed', CHILD)

            action('hold')
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as holder:
                holder.bind(str(d / 'hold.socket'))
                holder.settimeout(12)
                start()
                _, fds = packet(holder)
                assert len(fds) == 1
                try:
                    wait_for(lambda: state(CHILD) == 'active', 'restarted scheduler')
                    owned = link_id(fds[0])
                    pids = [int(p) for p in (d / 'child.pid').read_text().split()]
                    ctl('stop', CHILD)
                    assert not node().exists() and link_id(fds[0]) == owned and state(CHILD) == 'inactive'
                    assert all(not Path(f'/proc/{pid}').exists() for pid in pids)
                    results.append(dict(case='owned-stop-with-duplicate', link=owned, reaped=pids))
                finally:
                    os.close(fds[0])

            action('normal')
            with foreign(binary) as fd:
                foreign_link = link_id(fd)
                current_node = node().stat().st_ino
                start()
                wait_failed('occupied-target')
                assert link_id(fd) == foreign_link and node().stat().st_ino == current_node
                results[-1].update(foreign_link=foreign_link, kernel_node=current_node)

            ctl('reset-failed', CHILD)
            start()
            wait_for(lambda: state(CHILD) == 'active', 'final clean restart')
            results.append(dict(case='clean-restart', kernel_node=node().stat().st_ino))

            short = 'scxshort@scxshort.scheduler'
            ctl('start', short)
            # Exercise multiple status polls after a startup budget shorter than one poll interval.
            time.sleep(1.25)
            assert state(short) == 'active'
            results.append(dict(case='short-startup-timeout-after-readiness', state=state(short)))
        finally:
            ctl('stop', 'scxshort@scxshort.scheduler', 'scxshort.slice', check=False)
            ctl('stop', CHILD, check=False)
            ctl('stop', ROOT, check=False)
            ctl('stop', 'scxlifecycle.slice', check=False)
            files.close()
            ctl('daemon-reload')
    assert not (SCX / 'root').exists()


if __name__ == '__main__':
    if sys.argv[1:2] == ['proxy']:
        proxy(*sys.argv[2:])
    elif len(sys.argv) == 2:
        report = {'kernel': os.uname().release, 'cases': []}
        try:
            exercise(Path(sys.argv[1]).resolve(), report['cases'])
            report['result'] = 'PASS'
        except Exception as exc:
            report['error'] = repr(exc)
            raise
        finally:
            print(json.dumps(report, indent=2))
    else:
        sys.exit(f'Usage: {sys.argv[0]} /absolute/path/to/identity-provider')
