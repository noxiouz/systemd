#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Check branch restart and loss on a disposable VM running the patched system manager."""

import ctypes
import errno
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from lifecycle import CG, SCX, UNITS, ctl, state, wait_for

sys.path.insert(0, str(Path(__file__).resolve().parent / 'identity'))
from dispatch import observer
from matrix import provider

ROOT = 'partition@-.scheduler'
MIDDLE = 'partition@lab.scheduler'
LEAF = 'batch@lab-build.scheduler'
SIBLING = 'latency@interactive.scheduler'
IDLE = 'batch@lab-idle.scheduler'
LATE = 'late@extra.scheduler'
BUILD = 'scx09-build.service'
INTERACTIVE = 'scx09-interactive.service'
WORKSLICE = 'lab-build-ci.slice'


def prop(unit, name):
    return ctl('show', '--property', name, '--value', unit).stdout.strip()


def cgroup(unit):
    value = prop(unit, 'ControlGroup')
    if not value:
        raise RuntimeError(f'{unit} has no realized cgroup')
    return CG / value.lstrip('/')


def snapshot():
    nodes = {}
    for ops in (SCX / 'root').rglob('ops'):
        ids = [int(p[4:]) for p in ops.parent.parts if p.startswith('sub-')]
        target = ids[-1] if ids else CG.stat().st_ino
        if target in nodes:
            raise RuntimeError(f'multiple kernel attachments to {target}')
        nodes[target] = {'node': ops.parent.stat().st_ino, 'parent': ids[-2] if len(ids) > 1 else
                         (CG.stat().st_ino if ids else None), 'ops': ops.read_text().strip()}
    return nodes


def loader(binary, directory, unit, generation, fd, mode):
    d = Path(directory)
    with (d / 'starts').open('a') as log:
        log.write(f'{unit} {generation} {os.getpid()}\n')
    if (d / ('hold-' + unit)).exists():
        (d / ('held-' + unit)).touch()
        wait_for(lambda: not (d / ('hold-' + unit)).exists(), 'release ' + unit)
    os.execv(binary, [binary, generation, fd, mode])


def worker():
    Path('/proc/self/comm').write_text('scx05-probe')
    while True:
        time.sleep(.02)


def link_id(directory, unit):
    lines = (directory / 'starts').read_text().splitlines()
    pid = next(int(line.split()[2]) for line in reversed(lines) if line.split()[0] == unit)
    for path in Path(f'/proc/{pid}/fdinfo').iterdir():
        data = path.read_text()
        if re.search(r'^link_type:\s+struct_ops$', data, re.M):
            return int(re.search(r'^link_id:\s+(\d+)$', data, re.M)[1])
    raise RuntimeError(f'no struct_ops link in loader {pid}')


def detach(number):
    lib = ctypes.CDLL('libbpf.so.1', use_errno=True)
    fd = lib.bpf_link_get_fd_by_id(number)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'open test link')
    try:
        if lib.bpf_link_detach(fd) < 0:
            raise OSError(ctypes.get_errno(), 'detach test link')
    finally:
        os.close(fd)


def run(build, report):
    if os.getuid() != 0 or subprocess.run(['systemd-detect-virt', '--vm', '--quiet']).returncode or not \
            subprocess.run(['systemd-detect-virt', '--container', '--quiet']).returncode:
        raise RuntimeError('root on a dedicated full-system VM is required')
    if (SCX / 'state').read_text().strip() != 'disabled' or (SCX / 'root').exists():
        raise RuntimeError('guest already has an active scheduler')
    binary = build / 'identity-provider'
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(dir='/run', prefix='scx09-') as tmp, ExitStack() as stack:
        d = Path(tmp)

        def write(path, content):
            with path.open('x') as f:
                f.write(content)
            stack.callback(path.unlink)

        dropin = UNITS / '-.slice.d'
        if not dropin.exists():
            dropin.mkdir()
            stack.callback(dropin.rmdir)
        write(dropin / '98-scx09.conf', '[Slice]\nScheduler=partition@.scheduler\n')
        for unit, scheduler, weight in [('lab', 'partition', 300), ('lab-build', 'batch', 100),
                                        ('interactive', 'latency', 50), ('lab-idle', 'batch', 100)]:
            write(UNITS / (unit + '.slice'), f'[Slice]\nScheduler={scheduler}@.scheduler\nCPUWeight={weight}\n')
        for name in ('partition', 'batch', 'latency', 'late'):
            write(UNITS / (name + '@.scheduler'), '[Scheduler]\n' +
                  f'ExecStart=/usr/bin/python3 {script} loader {binary} {d} %n\nTimeoutStartSec=20s\n')
        for unit, sl in [(BUILD, WORKSLICE), (INTERACTIVE, 'interactive.slice')]:
            write(UNITS / unit, f'[Service]\nSlice={sl}\nExecStart=/usr/bin/python3 {script} worker\n')
        write(UNITS / 'scx09-gate@.service', f'[Service]\nSlice={WORKSLICE}\nType=oneshot\n' +
              f'ExecCondition=/usr/bin/touch {d}/%i.condition\nExecStartPre=/usr/bin/touch {d}/%i.pre\n' +
              f'ExecStart=/usr/bin/touch {d}/%i.start\n')

        # Reload the files exactly once on entry and again after removing them.
        try:
            ctl('daemon-reload')
            trace = Path('/sys/kernel/tracing/instances') / ('scx09-' + uuid.uuid4().hex)
            trace.mkdir()
            try:
                (trace / 'events/sched_ext/sched_ext_exit/enable').write_text('1')
                with observer(build / 'dispatch.bpf.o') as counter:
                    ctl('start', BUILD, INTERACTIVE)
                    wait_for(lambda: len(snapshot()) == 4, 'warm four-node tree')
                    ids = {n: cgroup(s).stat().st_ino for n, s in [(MIDDLE, 'lab.slice'), (LEAF, 'lab-build.slice'),
                                                                   (SIBLING, 'interactive.slice')]}
                    ids[ROOT] = CG.stat().st_ino
                    build_pid, side_pid = (int(prop(u, 'MainPID')) for u in [BUILD, INTERACTIVE])
                    healthy = snapshot()
                    side_link = link_id(d, SIBLING)

                    def progress(pid, node):
                        initial = counter(pid).get(node, 0)
                        wait_for(lambda: counter(pid).get(node, 0) > initial + 1, f'{pid} dispatch on {node}')
                        return [initial, counter(pid)[node]]

                    def hold(unit):
                        (d / ('held-' + unit)).unlink(missing_ok=True)
                        (d / ('hold-' + unit)).touch()

                    def held(unit):
                        wait_for(lambda: (d / ('held-' + unit)).exists(), 'held loader ' + unit)

                    def release(unit):
                        (d / ('hold-' + unit)).unlink()

                    def markers(case):
                        return [s for s in ('condition', 'pre', 'start') if (d / (case + '.' + s)).exists()]

                    hold(MIDDLE)
                    ctl('restart', '--no-block', MIDDLE)
                    held(MIDDLE)
                    between = snapshot()
                    if set(between) != {ids[ROOT], ids[SIBLING]}:
                        raise RuntimeError(f'unexpected tree while middle restarts: {between}')
                    sibling_delta = progress(side_pid, healthy[ids[SIBLING]]['node'])
                    assert link_id(d, SIBLING) == side_link and int(prop(INTERACTIVE, 'MainPID')) == side_pid
                    release(MIDDLE)
                    wait_for(lambda: state(LEAF) == 'active', 'restored leaf')
                    restored = snapshot()
                    assert state(IDLE) == 'inactive' and set(healthy) == set(restored)
                    assert restored[ids[LEAF]]['parent'] == ids[MIDDLE]
                    assert int(prop(BUILD, 'MainPID')) == build_pid
                    build_delta = progress(build_pid, restored[ids[LEAF]]['node'])
                    exits = [int(n) for n in re.findall(r'sub_cgroup_id (\d+)', (trace / 'trace').read_text())]
                    assert exits == [ids[LEAF], ids[MIDDLE]], exits
                    starts = [line.split()[0] for line in (d / 'starts').read_text().splitlines()]
                    assert starts[-2:] == [MIDDLE, LEAF] and IDLE not in starts
                    report['restart'] = dict(before=healthy, held=between, after=restored, detach_order=exits,
                                             restoration=starts[-2:], sibling_link=side_link, sibling_pid=side_pid,
                                             build_pid=build_pid, sibling_dispatch=sibling_delta, build_dispatch=build_delta)

                    # Install the extra.slice declaration only after a different live instance owns
                    # a descendant of its cgroup. The slice is first loaded on demand.
                    extra = CG / 'extra.slice'
                    extra.mkdir()
                    foreign_target = extra / 'foreign'
                    foreign_target.mkdir()
                    try:
                        with provider(binary, foreign_target, 'child') as foreign:
                            assert foreign.ready(foreign.status(), foreign_target)
                            original = snapshot()[foreign_target.stat().st_ino]
                            assert original['parent'] == ids[ROOT]
                            write(UNITS / 'extra.slice', '[Slice]\nScheduler=late@.scheduler\nCPUWeight=100\n')
                            assert ctl('start', LATE, check=False).returncode != 0 and state(LATE) == 'failed'
                            invocation = prop(LATE, 'InvocationID')
                            subprocess.run(['journalctl', '--sync'], check=True, timeout=10)
                            failures = subprocess.check_output(['journalctl', '-b', '--no-pager', '-o', 'json',
                                                                '_PID=1', f'UNIT={LATE}', f'INVOCATION_ID={invocation}',
                                                                f'ERRNO={errno.EBUSY}'], text=True)
                            assert failures.strip() and foreign.ready(foreign.status(), foreign_target)
                            assert snapshot()[foreign_target.stat().st_ino] == original
                            report['intermediate_conflict'] = dict(target=foreign_target.stat().st_ino,
                                                                  preserved=original, link=foreign.link_id,
                                                                  errno=errno.EBUSY,
                                                                  journal=json.loads(failures.splitlines()[0])['MESSAGE'])
                    finally:
                        foreign_target.rmdir()
                        ctl('stop', 'extra.slice', check=False)
                        if extra.exists():
                            extra.rmdir()

                    def launches():
                        return Counter(line.split()[0] for line in (d / 'starts').read_text().splitlines())

                    for case, lost, fallback in [('leaf', LEAF, MIDDLE), ('root', ROOT, None)]:
                        before_launches = launches()
                        hold(lost)
                        detach(link_id(d, lost))
                        wait_for(lambda: state(lost) == 'failed', 'lost ' + lost)
                        tree = snapshot()
                        if fallback:
                            assert ids[lost] not in tree and ids[fallback] in tree
                            fallback_dispatch = progress(build_pid, tree[ids[fallback]]['node'])
                        else:
                            assert not tree
                            assert re.search(r'^ext.enabled\s*:\s*0$', Path(f'/proc/{build_pid}/sched').read_text(), re.M)
                            fallback_dispatch = None
                        effective = prop(WORKSLICE, 'EffectiveScheduler')
                        assert effective == (fallback or 'kernel'), (case, effective, fallback)
                        gate = 'scx09-gate@' + case + '.service'
                        ctl('start', '--no-block', gate)
                        held(lost)
                        assert not markers(case)
                        release(lost)
                        wait_for(lambda: markers(case) == ['condition', 'pre', 'start'], 'admitted ' + case)
                        if lost == ROOT:
                            ctl('start', SIBLING)
                        current = snapshot()
                        assert set(current) == set(healthy) and int(prop(BUILD, 'MainPID')) == build_pid
                        fresh = launches() - before_launches
                        assert fresh == Counter([LEAF] if lost == LEAF else [ROOT, MIDDLE, LEAF, SIBLING]), fresh
                        report[case + '_loss'] = dict(fallback=tree, effective=effective, launches=fresh,
                                                    fallback_dispatch=fallback_dispatch, restored=current,
                                                    markers=markers(case), build_pid=build_pid)
            finally:
                (trace / 'events/sched_ext/sched_ext_exit/enable').write_text('0')
                trace.rmdir()
        finally:
            for hold_file in d.glob('hold-*'):
                hold_file.unlink()
            ctl('stop', BUILD, INTERACTIVE, LEAF, MIDDLE, SIBLING, ROOT, check=False)
            ctl('stop', 'lab.slice', 'interactive.slice', 'extra.slice', check=False)
            stack.close()
            ctl('daemon-reload')
    assert not (SCX / 'root').exists()


if __name__ == '__main__':
    if sys.argv[1:2] == ['loader']:
        loader(*sys.argv[2:])
    elif sys.argv[1:2] == ['worker']:
        worker()
    else:
        result = {}
        try:
            run(Path(sys.argv[1]).resolve(), result)
        except Exception as e:
            result['error'] = repr(e)
            raise
        finally:
            print(json.dumps(result, indent=2))
