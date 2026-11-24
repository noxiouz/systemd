#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Observe real service admission by a patched PID 1 in a disposable sched_ext guest."""

import ctypes
import errno
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

CG = Path('/sys/fs/cgroup')
ROOT = Path('/sys/kernel/sched_ext/root')
UNITS = Path('/run/systemd/system')
LAB = 'scx07lab'
BUILD = LAB + '-build'
SIDE = 'scx07interactive'
LAYERS = {'-': 'scx07qmap', LAB: 'scx07qmap', BUILD: 'scx07fifo', SIDE: 'scx07qmap'}
STAGES = ('condition', 'pre', 'main')


def run(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=25)


def ctl(*args, check=True):
    return run('systemctl', *args, check=check)


def prop(unit, key):
    return ctl('show', '-P', key, '--', unit).stdout.strip()


def wait_for(test, what):
    deadline = time.monotonic() + 15
    while not test():
        if time.monotonic() >= deadline:
            raise RuntimeError(f'timed out waiting for {what}')
        time.sleep(.02)


def scheduler(name):
    return f'{LAYERS[name]}@{name}.scheduler'


def slice_path(name):
    parts = name.split('-')
    return CG.joinpath(*['-'.join(parts[:i]) + '.slice' for i in range(1, len(parts) + 1)])


def kernel_chain(names):
    node = ROOT
    result = []
    for name in names:
        if name != '-':
            node = node / 'sub' / f'sub-{slice_path(name).stat().st_ino}'
        result.append(dict(name=scheduler(name), target=(CG if name == '-' else slice_path(name)).stat().st_ino,
                           inode=node.stat().st_ino, ops=(node / 'ops').read_text().strip(),
                           active_us=int(prop(scheduler(name), 'ActiveEnterTimestampMonotonic'))))
    return result


def loader(binary, directory, generation, fd, role):
    directory = Path(directory)
    target = '-' if role == 'root' else Path(os.readlink(f'/proc/self/fd/{fd}')).stem
    with (directory / (target + '.starts')).open('a') as f:
        print(generation, os.getpid(), file=f)
    if target == BUILD:
        # Delay the actual provider launch. The fixture still consumes manager cancellation promptly.
        with socket.socket(fileno=os.dup(0)) as channel:
            while (directory / 'hold').exists():
                if select.select([channel], [], [], .02)[0] and channel.recv(1) in (b'', b'Q'):
                    return
    os.execv(binary, [binary, generation, fd, role])


class Execution(ctypes.Structure):
    _fields_ = [('ns', ctypes.c_uint64), ('count', ctypes.c_uint64), ('pid', ctypes.c_uint32),
                ('level', ctypes.c_int32), ('nodes', ctypes.c_uint64 * 3)]


@contextmanager
def trace(binary, name):
    with ExitStack() as stack:
        pins = Path('/sys/fs/bpf') / name
        pins.mkdir()
        stack.callback(pins.rmdir)
        maps = pins / 'maps'
        maps.mkdir()
        stack.callback(maps.rmdir)
        keymap = maps / 'executions'
        link = pins / 'observer'
        stack.callback(keymap.unlink, missing_ok=True)
        stack.callback(link.unlink, missing_ok=True)
        run('bpftool', 'prog', 'load', binary, link, 'pinmaps', maps, 'autoattach')
        lib = ctypes.CDLL('libbpf.so.1', use_errno=True)
        fd = lib.bpf_obj_get(os.fsencode(keymap))
        if fd < 0:
            raise OSError(ctypes.get_errno(), 'open exec observation map')
        stack.callback(os.close, fd)

        def read(path):
            key = ctypes.c_uint64(path.stat().st_ino)
            value = Execution()
            if lib.bpf_map_lookup_elem(fd, ctypes.byref(key), ctypes.byref(value)) < 0:
                if ctypes.get_errno() == errno.ENOENT:
                    return dict(count=0)
                raise OSError(ctypes.get_errno(), 'read exec observation')
            return dict(count=value.count, first_ns=value.ns, pid=value.pid, level=value.level,
                        nodes=list(value.nodes[:max(0, value.level + 1)]))
        yield read


class Fixture:
    def __init__(self, path, build, read, cleanup):
        self.path = path
        self.build = build
        self.read = read
        self.cleanup = cleanup
        self.commands = {}
        self.slices = {}
        self.services = []
        self.sockets = []

    def install(self, name, data):
        path = UNITS / name
        with path.open('x') as handle:
            handle.write(data)
        self.cleanup.callback(path.unlink)

    def create(self):
        control = Path('/run/systemd/system.control') / (LAB + '.slice.d')
        if control.exists():
            raise RuntimeError(f'test slice already has runtime properties at {control}')
        self.cleanup.callback(lambda: control.rmdir() if control.exists() else None)
        self.cleanup.callback((control / '50-CPUWeight.conf').unlink, missing_ok=True)
        dropins = UNITS / '-.slice.d'
        if not dropins.exists():
            dropins.mkdir()
            self.cleanup.callback(dropins.rmdir)
        self.install('-.slice.d/98-scx07.conf', '[Slice]\nScheduler=scx07qmap@.scheduler\n')
        for name, weight in ((LAB, 300), (BUILD, 100), (SIDE, 100)):
            self.install(name + '.slice', f'[Slice]\nScheduler={LAYERS[name]}@.scheduler\nCPUWeight={weight}\n')
        for template, binary in (('scx07qmap', 'identity-provider'), ('scx07fifo', 'fifo-provider')):
            self.install(template + '@.scheduler',
                         f'[Unit]\nStartLimitIntervalSec=0\n[Scheduler]\nTimeoutStartSec=5s\n'
                         f'ExecStart=/usr/bin/python3 {Path(__file__).resolve()} loader {self.build / binary} {self.path}\n')
        for name in ('builda', 'buildb', 'interactive', 'veto', 'timeout', 'retryveto', 'retrytimeout', 'postloss',
                     'normalcold', 'normalwarm', 'transientcold', 'transientwarm', 'socketcold', 'socketwarm'):
            self.service(name)

    def service(self, name):
        unit = 'scx07-' + name + '.service'
        self.services.append(unit)
        group = SIDE if name == 'interactive' else f'{BUILD}-{name}'
        self.slices[name] = group
        cmds = self.commands[name] = {}
        for stage in STAGES:
            dest = self.path / ('scx07-' + name + '-' + stage)
            shutil.copy('/usr/bin/sleep' if stage == 'main' else '/usr/bin/true', dest)
            cmds[stage] = dest
        if name.startswith('transient'):
            return
        self.install(unit, f'[Unit]\nStartLimitIntervalSec=0\n[Service]\nSlice={group}.slice\nType=exec\n'
                     f'ExecCondition={cmds["condition"]}\nExecStartPre={cmds["pre"]}\nExecStart={cmds["main"]} infinity\n')
        if name.startswith('socket'):
            sock = unit.removesuffix('.service') + '.socket'
            self.sockets.append(sock)
            self.install(sock, f'[Socket]\nListenStream={self.path / name}\n')

    def markers(self, name):
        return {s: self.read(path) for s, path in self.commands[name].items()}

    def absent(self, name):
        obs = self.markers(name)
        assert all(s['count'] == 0 for s in obs.values()), (name, obs)
        return obs

    def admitted(self, name):
        wait_for(lambda: self.read(self.commands[name]['main'])['count'] == 1, name + ' exec')
        chain = kernel_chain(['-', SIDE] if name == 'interactive' else ['-', LAB, BUILD])
        obs = self.markers(name)
        for stage in obs.values():
            assert stage['count'] == 1 and stage['nodes'] == [p['inode'] for p in chain], (name, stage, chain)
            assert stage['first_ns'] >= max(node['active_us'] for node in chain) * 1000, (name, stage, chain)
        return dict(exec=obs, chain=chain)

    def api(self, name):
        group = self.slices[name]
        target = slice_path(group)
        if name.endswith('warm'):
            ctl('start', group + '.slice')
        existed = target.is_dir()
        assert existed == name.endswith('warm'), (name, existed)
        unit = 'scx07-' + name
        if name.startswith('transient'):
            cmd = self.commands[name]
            run('systemd-run', '--quiet', '--unit=' + unit, '--slice=' + group + '.slice', '--service-type=exec',
                '-p', f'ExecCondition={cmd["condition"]}', '-p', f'ExecStartPre={cmd["pre"]}', cmd['main'], 'infinity')
        elif name.startswith('socket'):
            ctl('start', unit + '.socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(self.path / name))
        else:
            ctl('start', unit + '.service')
        return dict(target_preexisted=existed, **self.admitted(name))

    def stop(self):
        for names in [self.sockets, self.services, [scheduler(BUILD), scheduler(SIDE)], [scheduler(LAB)], [scheduler('-')]]:
            ctl('stop', '--', *names, check=False)
        ctl('stop', '--', *(n + '.slice' for n in set(self.slices.values())), check=False)
        ctl('stop', '--', BUILD + '.slice', LAB + '.slice', SIDE + '.slice', check=False)


def exercise(build, report):
    if os.geteuid() != 0 or run('systemd-detect-virt', '--quiet', '--container', check=False).returncode == 0:
        raise RuntimeError('requires root in a disposable full-system guest')
    run('systemd-detect-virt', '--quiet', '--vm')
    if ROOT.exists() or (ROOT.parent / 'state').read_text().strip() != 'disabled':
        raise RuntimeError('an external scheduler is active')

    with tempfile.TemporaryDirectory(dir='/run', prefix='scx07-') as tmp, ExitStack() as files:
        d = Path(tmp)
        read = files.enter_context(trace(build / 'admission-exec.bpf.o', d.name))
        fixture = Fixture(d, build, read, files)
        fixture.create()
        try:
            ctl('daemon-reload')
            assert not slice_path(LAB).exists() and not slice_path(SIDE).exists()
            hold = d / 'hold'
            hold.touch()
            ctl('start', '--no-block', 'scx07-builda.service')
            wait_for(lambda: (d / (BUILD + '.starts')).exists(), 'delayed build scheduler invocation')
            ctl('start', '--no-block', 'scx07-buildb.service', 'scx07-interactive.service')
            first = fixture.admitted('interactive')
            blocked = {n: fixture.absent(n) for n in ('builda', 'buildb')}
            hold.unlink()
            built = {n: fixture.admitted(n) for n in ('builda', 'buildb')}
            starts = (d / (BUILD + '.starts')).read_text().splitlines()
            assert len(starts) == 1
            assert len(list(ROOT.rglob('ops'))) == 4
            report['concurrent_starts'] = dict(interactive=first, before_release=blocked, builds=built,
                                               shared_loader_starts=starts)

            report['entry_points'] = {n: fixture.api(n) for n in fixture.commands if n.endswith(('cold', 'warm'))}
            for sock in fixture.sockets:
                ctl('stop', sock)
            # Remove completed branch clients while preserving the interactive workload and its scheduler.
            ctl('stop', *(u for u in fixture.services if u != 'scx07-interactive.service'), check=False)
            ctl('stop', scheduler(BUILD), scheduler(LAB))
            sibling_id = kernel_chain(['-', SIDE])[-1]
            sibling_pid = prop('scx07-interactive.service', 'MainPID')
            report['failures'] = {}
            report['recovery'] = {}
            for fault in ('veto', 'timeout'):
                ctl('set-property', '--runtime', LAB + '.slice', 'CPUWeight=' + ('1' if fault == 'veto' else '300'))
                if fault == 'timeout':
                    hold.touch()
                job = ctl('start', 'scx07-' + fault + '.service', check=False)
                assert job.returncode and prop(scheduler(BUILD), 'ActiveState') == 'failed', (fault, job)
                invocation = prop(scheduler(BUILD), 'InvocationID')
                run('journalctl', '--sync')
                journal = run('journalctl', '-b', '--output=cat', '--no-pager', '_PID=1',
                              'UNIT=' + scheduler(BUILD), 'INVOCATION_ID=' + invocation).stdout
                reason = 'parent rejected' if fault == 'veto' else 'timed out'
                assert reason in journal.lower(), (fault, journal)
                report['failures'][fault] = dict(returncode=job.returncode, reason=reason, invocation=invocation,
                                                  journal=journal.splitlines(),
                                                  markers=fixture.absent(fault))
                hold.unlink(missing_ok=True)
                ctl('reset-failed', scheduler(BUILD))
                ctl('stop', scheduler(LAB))
                ctl('set-property', '--runtime', LAB + '.slice', 'CPUWeight=300')
                name = 'retry' + fault
                ctl('start', 'scx07-' + name + '.service')
                report['recovery'][fault] = fixture.admitted(name)
                after = kernel_chain(['-', SIDE])[-1]
                assert after == sibling_id and prop('scx07-interactive.service', 'MainPID') == sibling_pid
                report['recovery'][fault].update(healthy_sibling=after, sibling_pid=int(sibling_pid))
                if fault == 'veto':
                    ctl('stop', 'scx07-retryveto.service', scheduler(BUILD))
                    ctl('stop', scheduler(LAB))

            pid = prop('scx07-retrytimeout.service', 'MainPID')
            # External loss after the first exec. Keep that task live and measure its PID across the loss.
            _, loader_pid = (d / (BUILD + '.starts')).read_text().splitlines()[-1].split()
            info = [p.read_text() for p in Path('/proc', loader_pid, 'fdinfo').iterdir()]
            links = [int(line.split()[1]) for content in info if 'link_type:\tstruct_ops' in content
                     for line in content.splitlines() if line.startswith('link_id:')]
            assert len(links) == 1
            lib = ctypes.CDLL('libbpf.so.1', use_errno=True)
            fd = lib.bpf_link_get_fd_by_id(links[0])
            if fd < 0:
                raise OSError(ctypes.get_errno(), 'open owned leaf link')
            try:
                if lib.bpf_link_detach(fd) < 0:
                    raise OSError(ctypes.get_errno(), 'detach leaf')
            finally:
                os.close(fd)
            wait_for(lambda: prop(scheduler(BUILD), 'ActiveState') == 'failed', 'post-admission loss')
            assert prop('scx07-retrytimeout.service', 'MainPID') == pid and Path('/proc', pid).exists()
            ctl('reset-failed', scheduler(BUILD))
            ctl('start', 'scx07-postloss.service')
            report['loss_after_admission'] = dict(existing_pid=int(pid), detached_link=links[0],
                                                  new_start=fixture.admitted('postloss'))
        finally:
            fixture.stop()
            files.close()
            ctl('daemon-reload')
    assert not ROOT.exists()
    report['teardown'] = (ROOT.parent / 'state').read_text().strip()


if __name__ == '__main__':
    if sys.argv[1:2] == ['loader']:
        loader(*sys.argv[2:])
    elif len(sys.argv) == 2:
        build = Path(sys.argv[1]).resolve()
        result = dict(kernel=os.uname().release, manager=run(
            'busctl', 'get-property', 'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
            'org.freedesktop.systemd1.Manager', 'Version').stdout.strip(),
            binaries={name: hashlib.sha256((build / name).read_bytes()).hexdigest()
                      for name in ('identity-provider', 'fifo-provider', 'admission-exec.bpf.o')},
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        try:
            exercise(build, result)
            result['outcome'] = 'PASS'
        except Exception as exc:
            result['error'] = repr(exc)
            raise
        finally:
            print(json.dumps(result, indent=2))
    else:
        sys.exit(f'Usage: {sys.argv[0]} PATH-TO-MIXED-PROVIDER-BUILD')
