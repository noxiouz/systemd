#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Compare a cooperative scheduler's per-start channel with the kernel's scheduler tree."""

import argparse
import array
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

CG = Path('/sys/fs/cgroup')
SCX = Path('/sys/kernel/sched_ext')
ROOT = SCX / 'root'


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError('kernel state did not settle')
        time.sleep(0.02)


def inode(path):
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return None


def fdinfo(fd):
    return dict(
        line.split(':\t', 1)
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
        if ':\t' in line
    )


def explicit_detach(session, foreign, case):
    """Detach our received link while a separate process retains another descriptor for it."""
    libbpf = ctypes.CDLL('libbpf.so.1', use_errno=True)
    with subprocess.Popen(['/usr/bin/sleep', 'infinity'], pass_fds=(session.link_fd,)) as holder:
        try:
            held = Path(f'/proc/{holder.pid}/fdinfo/{session.link_fd}')
            assert held.is_file()
            result = libbpf.bpf_link_detach(session.link_fd)
            if result < 0:
                raise OSError(ctypes.get_errno(), 'BPF_LINK_DETACH')
            wait_for(lambda: not session.node.exists())
            status = session.status()
            case(
                'explicit-owned-detach',
                session,
                session.target,
                False,
                status,
                third_party_fd_open=held.is_file(),
                other_link=foreign.link_id,
            )
            assert held.is_file() and status['exit_kind'] and session.proc.poll() is None
            case('unrelated-sibling-after-detach', foreign, foreign.target, True)
        finally:
            holder.terminate()


def kernel_tree():
    """Read the actual topology independently of the provider, control packets and requested parent."""
    tree = {}
    for ops in ROOT.rglob('ops'):
        ids = [CG.stat().st_ino]
        ids.extend(int(n[4:]) for n in ops.parent.relative_to(ROOT).parts if n.startswith('sub-'))
        tree[ids[-1]] = dict(
            target=ids[-1],
            parent=ids[-2] if len(ids) > 1 else None,
            inode=ops.parent.stat().st_ino,
            ops=ops.read_text().strip(),
        )
    return tree


@contextmanager
def provider(binary, target, mode, parent=None):
    with ExitStack() as stack:
        channel, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        stack.callback(channel.close)
        stack.callback(other.close)
        channel.settimeout(10)
        target_fd = os.open(target, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, target_fd)
        generation = uuid.uuid4().hex
        proc = subprocess.Popen(
            [binary, generation, str(target_fd), mode], stdin=other, pass_fds=(target_fd,)
        )
        other.close()

        def stop():
            try:
                channel.send(b'Q')
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            if proc.returncode:
                raise RuntimeError(f'provider exit {proc.returncode}')

        stack.callback(stop)
        session = Session(channel, proc, generation, target_fd, target, parent)
        session.attach, link_fd = session.receive()
        if link_fd is not None:
            stack.callback(os.close, link_fd)
            session.link_fd = link_fd
            info = fdinfo(link_fd)
            if info['link_type'] != 'struct_ops':
                raise RuntimeError(f'unexpected BPF link: {info}')
            session.link_id = int(info['link_id'])
        if session.attach['event'] != 'attach' or session.attach['generation'] != generation:
            raise RuntimeError('unexpected initial reply')
        session.node = (
            ROOT
            if mode != 'child'
            else ((parent.node if parent else ROOT) / 'sub' / f'sub-{os.fstat(target_fd).st_ino}')
        )
        if session.node.is_dir():
            session.node_fd = os.open(session.node, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
            stack.callback(os.close, session.node_fd)
        yield session


class Session:
    def __init__(self, channel, proc, generation, target_fd, target, parent):
        self.channel = channel
        self.proc = proc
        self.generation = generation
        self.target_fd = target_fd
        self.target = target
        self.parent = parent
        self.node_fd = None
        self.link_fd = None
        self.link_id = None

    def receive(self):
        fds = array.array('i')
        data, aux, flags, _ = self.channel.recvmsg(
            2048, socket.CMSG_SPACE(fds.itemsize), socket.MSG_CMSG_CLOEXEC
        )
        for level, kind, content in aux:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds.frombytes(content)
        if not data or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(fds) > 1:
            raise RuntimeError('invalid private provider reply')
        event, gen, target, err, kind, code, msg = data.decode().split(' ', 6)
        return dict(
            event=event,
            generation=gen,
            target=int(target),
            errno=int(err),
            exit_kind=int(kind),
            exit_code=int(code),
            message=msg,
        ), fds[0] if fds else None

    def status(self):
        self.channel.send(b'S')
        while True:
            packet, fd = self.receive()
            assert fd is None
            if packet['event'] == 'status':
                return packet
            assert packet['event'] == 'exit'

    def observes(self):
        return (
            self.node_fd is not None
            and inode(self.node) == os.fstat(self.node_fd).st_ino
            and (self.node / 'ops').is_file()
        )

    def ready(self, packet, target):
        # The provider promises to use the supplied FD, report only after attach returns, and publish its
        # own durable ops.exit record. A kernel directory can exist before attach has finished.
        return (
            self.attach['target'] == os.fstat(self.target_fd).st_ino
            and not self.attach['errno']
            and not self.attach['exit_kind']
            and packet['generation'] == self.generation
            and packet['target'] == inode(target)
            and packet['target'] == os.fstat(self.target_fd).st_ino
            and not packet['errno']
            and not packet['exit_kind']
            and self.link_id is not None
            and self.observes()
            and (self.parent is None or self.parent.ready(self.parent.status(), self.parent.target))
        )

    def identity(self):
        return dict(
            generation=self.generation,
            target=os.fstat(self.target_fd).st_ino,
            link=self.link_id,
            node_inode=os.fstat(self.node_fd).st_ino if self.node_fd is not None else None,
        )


def run(binary, report):
    rows = report['cases'] = []

    def case(name, current, target, expected, packet=None, **extra):
        status = packet or current.status()
        actual = current.ready(status, target)
        tree = kernel_tree()
        row = dict(
            case=name,
            expected=expected,
            ready=actual,
            expected_target=inode(target),
            status=status,
            instance=current.identity(),
            parent=current.parent.identity() if current.parent else None,
            kernel_path=str(current.node),
            node_current=inode(current.node),
            owned_node=current.observes(),
            oracle_expected_target=tree.get(inode(target)),
            oracle_provider_target=tree.get(os.fstat(current.target_fd).st_ino),
            loader_alive=current.proc.poll() is None,
            **extra,
        )
        rows.append(row)
        if actual != expected:
            raise RuntimeError(f'{name}: expected ready={expected}, got {actual}')
        if expected:
            oracle = row['oracle_expected_target']
            assert oracle['inode'] == row['instance']['node_inode'] and oracle['ops'] == 'qmap'
            assert oracle['parent'] == (row['parent']['target'] if row['parent'] else None)
        return row

    if subprocess.run(['systemd-detect-virt', '--quiet', '--container']).returncode == 0:
        raise RuntimeError('full-system VM required')
    subprocess.run(['systemd-detect-virt', '--quiet', '--vm'], check=True)
    if (SCX / 'state').read_text().strip() != 'disabled' or ROOT.exists():
        raise RuntimeError('an external scheduler is already active')
    btf = subprocess.check_output(
        ['bpftool', 'btf', 'dump', 'file', '/sys/kernel/btf/vmlinux', 'format', 'raw']
    )
    report['hierarchy_supported'] = b" FUNC 'scx_bpf_sub_dispatch' " in btf
    if not report['hierarchy_supported']:
        raise RuntimeError('sub-scheduler capability absent')

    with ExitStack() as top:
        base = CG / f'scx-id-{uuid.uuid4().hex}'
        base.mkdir()
        top.callback(base.rmdir)
        (base / 'cgroup.subtree_control').write_text('+cpu')
        target, other, narrow = (base / name for name in ('target', 'other', 'narrow'))
        with ExitStack() as groups:
            for path in (target, other, narrow, narrow / 'child'):
                path.mkdir()
                groups.callback(path.rmdir)
            (narrow / 'cpu.weight').write_text('1')
            with provider(binary, target, 'child') as absent:
                case('missing-parent', absent, target, False, parent_sub=False)
            with provider(binary, CG, 'flat') as flat:
                case('root-without-delegation', flat, CG, True, parent_sub=(ROOT / 'sub').exists())
                assert not (ROOT / 'sub').exists()
                with provider(binary, target, 'child', flat) as denied:
                    case('no-sub-attach', denied, target, False)
            wait_for(lambda: not ROOT.exists())

            with provider(binary, CG, 'root') as root:
                case('root', root, CG, True, parent_sub=(ROOT / 'sub').is_dir())
                assert (ROOT / 'sub').is_dir()
                with provider(binary, other, 'child', root) as wrong:
                    row = case('wrong-target', wrong, target, False)
                    assert wrong.observes() and row['oracle_expected_target'] is None
                    assert row['oracle_provider_target']['target'] == inode(other)
                with provider(binary, target, 'child', root) as branch:
                    case('configured-intermediate', branch, target, True)
                    nested = target / 'nested'
                    nested.mkdir()
                    try:
                        with provider(binary, nested, 'child', root) as misplaced:
                            row = case('wrong-parent', misplaced, nested, False)
                            assert row['oracle_expected_target']['parent'] == inode(target)
                    finally:
                        nested.rmdir()
                with provider(binary, narrow, 'child', root) as intermediate:
                    case('capable-parent', intermediate, narrow, True)
                    assert (intermediate.node / 'sub').is_dir()
                    with provider(binary, narrow / 'child', 'child', intermediate) as veto:
                        failure = veto.status()
                        case('parent-veto', veto, veto.target, False, failure)
                        assert veto.proc.poll() is None and veto.link_id and failure['exit_kind']
                        assert 'parent rejected' in failure['message'] and not veto.observes()
                with provider(binary, target, 'child', root) as first:
                    saved = first.status()
                    first_id = first.identity()
                    case('same-algorithm-child', first, target, True, saved)
                    assert first.link_id != root.link_id
                    assert (first.node / 'ops').read_text() == (root.node / 'ops').read_text()
                    with provider(binary, other, 'child', root) as foreign:
                        explicit_detach(first, foreign, case)
                with provider(binary, target, 'child', root) as replacement:
                    replaced_status = replacement.status()
                    case(
                        'same-name-replacement',
                        replacement,
                        target,
                        True,
                        replaced_status,
                        replaced=first_id,
                    )
                    assert first_id['target'] == inode(target) and first_id['link'] != replacement.link_id
                    assert first_id['node_inode'] != replacement.identity()['node_inode']
                    case('previous-start-reply', replacement, target, False, saved)
                    target.rmdir()
                    wait_for(lambda: not replacement.node.exists())
                    dropped, fd = replacement.receive()
                    assert fd is None and dropped['event'] == 'exit'
                    # Deliberately discard the edge and recover by querying the private, persistent exit record.
                    terminal = replacement.status()
                    row = case(
                        'dropped-loss-notification',
                        replacement,
                        target,
                        False,
                        terminal,
                        link_still_open=int(fdinfo(replacement.link_fd)['link_id']) == replacement.link_id,
                    )
                    assert (
                        row['link_still_open'] and terminal['exit_kind'] and replacement.proc.poll() is None
                    )
                    target.mkdir()
                    with provider(binary, target, 'child', root) as recreated:
                        case('recreated-cgroup', recreated, target, True, previous=replacement.identity())
                        case('recreated-cgroup-old-instance', replacement, target, False, replaced_status)
                        assert os.fstat(replacement.target_fd).st_ino != inode(target)
            wait_for(lambda: not ROOT.exists())
    report['after'] = (SCX / 'state').read_text().strip()
    assert report['after'] == 'disabled'
    report['false_readiness'] = [r['case'] for r in rows if r['ready'] and not r['expected']]
    report['false_readiness_if_link_and_loader_only'] = [
        r['case'] for r in rows if r['instance']['link'] and r['loader_alive'] and not r['expected']
    ]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider', type=Path)
    parser.add_argument('--provider-revision', required=True)
    parser.add_argument('--image-sha256', required=True)
    args = parser.parse_args()
    report = dict(
        kernel=os.uname().release,
        provider_revision=args.provider_revision,
        image_sha256=args.image_sha256,
        provider_sha256=hashlib.sha256(args.provider.read_bytes()).hexdigest(),
        matrix_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    try:
        run(args.provider.resolve(), report)
    except Exception as exc:
        report['error'] = repr(exc)
        raise
    finally:
        print(json.dumps(report, indent=2))
