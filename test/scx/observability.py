#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Cross-check D-Bus and systemctl status against live kernel nodes on the disposable VM."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from lifecycle import CG, SCX, UNITS, ctl, foreign, link_id, state, wait_for

SCHEDULER = 'org.freedesktop.systemd1.Scheduler'
SLICE = 'org.freedesktop.systemd1.Slice'
MANAGER = 'org.freedesktop.systemd1'
ROOT = 'partition@-.scheduler'
MIDDLE = 'partition@lab.scheduler'
LEAF = 'batch@lab-build.scheduler'


def bus(*args):
    value = json.loads(subprocess.check_output(['busctl', '--json=short', '--', *args], text=True))
    return value['data']


def property_value(unit, interface, prop):
    path, = bus('call', MANAGER, '/org/freedesktop/systemd1', MANAGER + '.Manager', 'GetUnit', 's', unit)
    return bus('get-property', MANAGER, path, interface, prop)


def compare(unit, interface, prop, expected):
    value = property_value(unit, interface, prop)
    assert value == expected, (unit, prop, value, expected)
    cli = ctl('show', '-P', prop, '--', unit).stdout.strip()
    assert cli == str(expected), (unit, prop, cli, expected)
    return dict(unit=unit, property=prop, bus=value, cli=cli)


def run(binary, rows):
    if os.getuid() != 0 or subprocess.run(['systemd-detect-virt', '--quiet', '--vm']).returncode or \
            not subprocess.run(['systemd-detect-virt', '--quiet', '--container']).returncode:
        raise RuntimeError('exclusive full-system guest required')
    assert (SCX / 'state').read_text().strip() == 'disabled'
    rows.append(dict(case='kernel-default', observation=compare('-.slice', SLICE, 'EffectiveScheduler', 'kernel')))
    script = str(Path(binary).resolve())
    units = ['lab.slice', 'lab-build.slice', 'lab-build-ci.slice']
    with ExitStack() as files:
        def config(file, body):
            with file.open('x') as out:
                out.write(body)
            files.callback(file.unlink)

        workload = 'scx-observe-foreign.service'
        marker = Path(files.enter_context(tempfile.TemporaryDirectory(dir='/run'))) / 'ran'
        config(UNITS / workload, f'[Service]\nSlice={units[-1]}\nType=oneshot\nExecStart=/usr/bin/touch {marker}\n')
        root = UNITS / '-.slice.d'
        if not root.exists():
            root.mkdir()
            files.callback(root.rmdir)
        config(root / '98-scx-observe.conf', '[Slice]\nScheduler=partition@.scheduler\n')
        for policy in ('partition', 'batch'):
            config(UNITS / f'{policy}@.scheduler', f'[Scheduler]\nExecStart={script}\n')
        for unit, policy in zip(units[:2], ('partition', 'batch')):
            config(UNITS / unit, f'[Slice]\nScheduler={policy}@.scheduler\n')
        try:
            ctl('daemon-reload')
            ctl('start', LEAF, units[-1])
            listing = ctl('list-units', '--type=scheduler', '--all', '--plain', '--no-legend').stdout
            for unit in (ROOT, MIDDLE, LEAF):
                assert sum(line.split()[0] == unit for line in listing.splitlines()) == 1
            observations = []
            observations.append(compare(LEAF, SCHEDULER, 'TargetSlice', 'lab-build.slice'))
            observations.append(compare(LEAF, SCHEDULER, 'ParentScheduler', MIDDLE))
            observations.append(compare(units[1], SLICE, 'Scheduler', LEAF))
            observations.append(compare(units[2], SLICE, 'Scheduler', ''))
            observations.append(compare(units[2], SLICE, 'ConfiguredScheduler', LEAF))
            observations.append(compare(units[2], SLICE, 'EffectiveScheduler', LEAF))
            status = ctl('status', '--no-pager', LEAF).stdout
            assert 'Target: lab-build.slice' in status and f'Parent: {MIDDLE}' in status
            assert "Unknown section 'Scheduler'" not in status

            target = CG / 'lab.slice' / 'lab-build.slice'
            middle = SCX / 'root/sub' / f'sub-{target.parent.stat().st_ino}'
            leaf = middle / 'sub' / f'sub-{target.stat().st_ino}'
            origin = leaf.stat().st_ino
            algorithm = (leaf / 'ops').read_text().strip()
            owned = property_value(LEAF, SCHEDULER, 'LinkID')
            rows.append(dict(case='managed', link=owned, node=origin, parent=middle.stat().st_ino,
                             configured=LEAF, effective=LEAF, inherited=units[2], comparisons=observations,
                             listed=listing.splitlines(), status=status.splitlines()))

            with foreign(script, target / units[-1]) as foreign_link:
                compare(units[-1], SLICE, 'EffectiveScheduler', 'foreign')
                assert ctl('start', workload, check=False).returncode != 0 and not marker.exists()
                foreign_id = link_id(foreign_link)
            ctl('start', workload)
            assert marker.exists()
            rows.append(dict(case='foreign-inherited-admission', foreign_link=foreign_id, recovered=LEAF))

            libbpf = ctypes.CDLL('libbpf.so.1', use_errno=True)
            link = libbpf.bpf_link_get_fd_by_id(owned)
            assert link >= 0
            try:
                cgroup = ctl('show', '-P', 'ControlGroup', LEAF).stdout.strip()
                provider, = (CG / cgroup.lstrip('/') / 'cgroup.procs').read_text().split()
                pid = int(provider)
                os.kill(pid, signal.SIGSTOP)
                try:
                    wait_for(lambda: Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] == 'T',
                             'stopped loader')
                    assert libbpf.bpf_link_detach(link) == 0
                    wait_for(lambda: not leaf.exists(), 'kernel eviction')
                    os.kill(pid, 0)
                finally:
                    os.kill(pid, signal.SIGCONT)
                wait_for(lambda: state(LEAF) == 'failed', 'terminal state')
                observations = [compare(units[2], SLICE, 'ConfiguredScheduler', LEAF)]
                observations.append(compare(units[2], SLICE, 'EffectiveScheduler', MIDDLE))
                reason = property_value(LEAF, SCHEDULER, 'FailureReason')
                assert '(kind=64, code=0)' in reason, reason
                observations.append(compare(LEAF, SCHEDULER, 'FailureReason', reason))
                observations.append(compare(LEAF, SCHEDULER, 'Result', 'kernel-exit'))
                status = ctl('status', '--no-pager', LEAF, check=False)
                assert status.returncode == 3 and f'Failure: {reason}' in status.stdout
                rows.append(dict(case='evicted', stopped_live_provider=pid, fallback_node=middle.stat().st_ino, effective=MIDDLE,
                                 terminal=reason, owned_link_fd_still_open=link_id(link),
                                 comparisons=observations, status=status.stdout.splitlines()))
            finally:
                os.close(link)

            with foreign(script, target) as foreign_link:
                assert (leaf / 'ops').read_text().strip() == algorithm
                assert origin != leaf.stat().st_ino and owned != link_id(foreign_link)
                observations = [compare(units[2], SLICE, 'ConfiguredScheduler', LEAF),
                                compare(units[2], SLICE, 'EffectiveScheduler', 'foreign')]
                rows.append(dict(case='foreign-replacement', node=leaf.stat().st_ino,
                                 link=link_id(foreign_link), algorithm=algorithm, effective='foreign',
                                 comparisons=observations))
        finally:
            for unit in (LEAF, MIDDLE, ROOT, *reversed(units)):
                ctl('stop', unit, check=False)
            files.close()
            ctl('daemon-reload')
    assert (SCX / 'state').read_text().strip() == 'disabled'


if __name__ == '__main__':
    report = {'kernel': os.uname().release, 'cases': []}
    try:
        run(sys.argv[1], report['cases'])
        report['result'] = 'PASS'
    except Exception as exc:
        report['error'] = repr(exc)
        raise
    finally:
        print(json.dumps(report, indent=2))
