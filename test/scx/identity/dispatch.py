#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Measure which live scheduler actually runs each probe on the reference kernel."""

import argparse
import ctypes
import hashlib
import json
import os
import select
import subprocess
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from matrix import CG, ROOT, SCX, provider, wait_for

SCHED_EXT = getattr(os, 'SCHED_EXT', 7)


class Key(ctypes.Structure):
    _fields_ = [('pid', ctypes.c_uint32), ('padding', ctypes.c_uint32), ('node', ctypes.c_uint64)]


@contextmanager
def observer(binary):
    with ExitStack() as stack:
        path = Path('/sys/fs/bpf') / f'scx-dispatch-{uuid.uuid4().hex}'
        path.mkdir()
        stack.callback(path.rmdir)
        maps = path / 'maps'
        maps.mkdir()
        stack.callback(maps.rmdir)
        link = path / 'observer'
        counts = maps / 'dispatches'
        stack.callback(counts.unlink, missing_ok=True)
        stack.callback(link.unlink, missing_ok=True)
        subprocess.run(['bpftool', 'prog', 'load', binary, link, 'pinmaps', maps, 'autoattach'], check=True)
        lib = ctypes.CDLL('libbpf.so.1', use_errno=True)
        fd = lib.bpf_obj_get(os.fsencode(counts))
        if fd < 0:
            raise OSError(ctypes.get_errno(), 'open dispatch map')
        stack.callback(os.close, fd)

        def read(pid):
            result = {}
            previous = None
            key = Key()
            while lib.bpf_map_get_next_key(fd, previous, ctypes.byref(key)) == 0:
                if key.pid == pid:
                    count = ctypes.c_uint64()
                    if lib.bpf_map_lookup_elem(fd, ctypes.byref(key), ctypes.byref(count)) < 0:
                        raise OSError(ctypes.get_errno(), 'read dispatch counter')
                    result[key.node] = count.value
                previous = ctypes.byref(Key.from_buffer_copy(key))
            return result

        yield read


@contextmanager
def probe(group, explicit_ext=False):
    proc = subprocess.Popen(
        [sys.executable, __file__, 'probe', group, str(int(explicit_ext))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        if reply(proc) != 'ready':
            raise RuntimeError(f'probe {proc.pid} failed to enter {group}')
        yield proc
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        proc.stdout.close()


def reply(proc):
    if not select.select([proc.stdout], [], [], 10)[0]:
        raise RuntimeError(f'timed out waiting for probe {proc.pid}')
    return proc.stdout.readline().strip()


def measure(proc, instance, counters, expected_ext=True):
    previous = counters(proc.pid)
    for _ in range(12):
        proc.stdin.write('wake\n')
        proc.stdin.flush()
        if reply(proc) != 'ran':
            raise RuntimeError(f'probe {proc.pid} did not run')
    observed = counters(proc.pid)
    node = os.fstat(instance.node_fd).st_ino
    if expected_ext:
        if set(observed) != {node} or observed[node] <= previous.get(node, 0):
            raise RuntimeError(
                f'probe {proc.pid}: expected dispatch on {node}, observed {previous} -> {observed}'
            )
    elif observed:
        raise RuntimeError(f'ordinary probe {proc.pid} used sched_ext in partial mode: {observed}')
    return dict(
        pid=proc.pid,
        policy=os.sched_getscheduler(proc.pid),
        wake_replies=12,
        expected_scx=expected_ext,
        kernel_node=node,
        invocation=instance.generation,
        link_id=instance.link_id,
        dispatches_before=previous,
        dispatches_after=observed,
    )


def ready(session):
    if not session.ready(session.status(), session.target):
        raise RuntimeError(f'{session.target} did not attach: {session.status()}')
    return session.identity() | {
        'ops': (session.node / 'ops').read_text().strip(),
        'sysfs': str(session.node),
    }


def run(build, report):
    if subprocess.run(['systemd-detect-virt', '--quiet', '--container']).returncode == 0:
        raise RuntimeError('full-system VM required')
    subprocess.run(['systemd-detect-virt', '--quiet', '--vm'], check=True)
    if (SCX / 'state').read_text().strip() != 'disabled' or ROOT.exists():
        raise RuntimeError('an external scheduler is already active')
    qmap, fifo = build / 'identity-provider', build / 'fifo-provider'
    report['binaries'] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (qmap, fifo, build / 'dispatch.bpf.o')
    }

    with ExitStack() as stack, observer(build / 'dispatch.bpf.o') as counters:

        def group(parent, name, *, children=False):
            path = parent / name
            path.mkdir()
            stack.callback(path.rmdir)
            if children:
                (path / 'cgroup.subtree_control').write_text('+cpu')
            return path

        base = group(CG, f'scx-mixed-{uuid.uuid4().hex}', children=True)
        middle = group(base, 'middle', children=True)
        sibling = group(base, 'sibling')
        leaf = group(middle, 'fifo', children=True)
        inherit = group(leaf, 'inheriting.slice')
        root_work = group(base, 'root-work')
        middle_work = group(middle, 'middle-work')
        (middle / 'cpu.weight').write_text('300')
        (sibling / 'cpu.weight').write_text('50')
        with (
            provider(qmap, CG, 'root') as root,
            provider(qmap, sibling, 'child', root) as side,
            provider(qmap, middle, 'child', root) as parent,
            probe(sibling) as sibling_probe,
        ):
            with provider(fifo, leaf, 'child', parent) as child:
                sessions = dict(root=root, intermediate=parent, fifo=child, sibling=side)
                report['full'] = {n: ready(s) for n, s in sessions.items()}
                for name, instance in report['full'].items():
                    if instance['ops'] != ('fixture_fifo' if name == 'fifo' else 'qmap'):
                        raise RuntimeError(f'{name} loaded the wrong BPF policy')
                if len(list(ROOT.rglob('ops'))) != 4:
                    raise RuntimeError('unexpected number of direct attachments')
                report['probes'] = {}
                for name, target, expected in [
                    ('root', root_work, root),
                    ('middle', middle_work, parent),
                    ('inheriting', inherit, child),
                ]:
                    with probe(target) as p:
                        report['probes'][name] = measure(p, expected, counters) | {'cgroup': str(target)}
                report['probes']['sibling_before'] = measure(sibling_probe, side, counters)
            wait_for(lambda: not child.node.exists())
            if ready(side) != report['full']['sibling']:
                raise RuntimeError('sibling identity changed after child stop')
            report['probes']['sibling_after'] = measure(sibling_probe, side, counters)

        wait_for(lambda: not ROOT.exists())
        with provider(qmap, CG, 'partial') as root:
            report['partial'] = {'root': ready(root)}
            with probe(root_work, True) as ext, probe(root_work) as ordinary:
                report['partial']['ext'] = measure(ext, root, counters)
                report['partial']['ordinary'] = measure(ordinary, root, counters, expected_ext=False)
                if (report['partial']['ext']['policy'], report['partial']['ordinary']['policy']) != (
                    SCHED_EXT,
                    os.SCHED_OTHER,
                ):
                    raise RuntimeError('partial-mode probes did not have the requested policies')
    wait_for(lambda: not ROOT.exists() and (SCX / 'state').read_text().strip() == 'disabled')
    report['teardown'] = 'disabled'


def main():
    if sys.argv[1:2] == ['probe']:
        Path(sys.argv[2], 'cgroup.procs').write_text(str(os.getpid()))
        if int(sys.argv[3]):
            os.sched_setscheduler(0, SCHED_EXT, os.sched_param(0))
        Path('/proc/self/comm').write_text('scx05-probe')
        print('ready', flush=True)
        for _ in sys.stdin:
            print('ran', flush=True)
        return
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('build', type=Path)
    p.add_argument('--provider-revision', required=True)
    p.add_argument('--fixture-revision', required=True)
    p.add_argument('--image-sha256', required=True)
    args = p.parse_args()
    fifo_source = Path(__file__).parent / 'mixed/fifo.bpf.c'
    report = dict(
        kernel=os.uname().release,
        image_sha256=args.image_sha256,
        fixture_revision=args.fixture_revision,
        providers={
            'qmap': {
                'source': 'torvalds/linux/tools/sched_ext/scx_qmap.bpf.c',
                'revision': args.provider_revision,
                'algorithm': 'Weighted rotation across five FIFO queues',
                'roles': ['root', 'delegating intermediate', 'leaf'],
                'changes': 'Cooperative loader configures the upstream BPF program with the target and mode.',
                'requires': ['sched_ext', 'CID struct_ops', 'BPF arena', 'sub-scheduling'],
            },
            'fixture_fifo': {
                'source': 'test/scx/identity/mixed/fifo.bpf.c',
                'revision': args.fixture_revision,
                'source_sha256': hashlib.sha256(fifo_source.read_bytes()).hexdigest(),
                'algorithm': 'Single shared FIFO dispatch queue',
                'roles': ['delegated leaf'],
                'changes': 'Test-owned BPF policy and cooperative loader, built separately in userspace.',
                'requires': ['sched_ext', 'CID struct_ops', 'BPF arena', 'sub-scheduling'],
            },
        },
        observer='BTF fentry/set_next_task_scx with per-PID, per-kernfs-inode counters',
    )
    try:
        run(args.build.resolve(), report)
    except Exception as exc:
        report['error'] = repr(exc)
        raise
    finally:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
