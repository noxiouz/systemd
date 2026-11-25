#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Check scheduler ownership and pending jobs on a disposable system-manager guest."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from lifecycle import CG, SCX, UNITS, ctl, proxy, state, wait_for


ROOT = 'scxkeep@-.scheduler'
MID = 'scxkeep@scxkeep.scheduler'
BUILD = 'scxkeep@scxkeep-build.scheduler'
SIDE = 'scxkeep@scxkeep-side.scheduler'
NEW = 'scxnew@scxkeep-build.scheduler'
WORK = 'scxkeep-work.service'
MARK = 'scxkeep-mark.service'
GUEST_SLICES = ['scxkeep.slice', 'scxkeep-build.slice', 'scxkeep-side.slice', 'scxkeep-build-ci.slice']


def show(unit, prop):
    return ctl('show', '-P', prop, '--', unit).stdout.strip()


def launch(binary, directory, generation, descriptor, role):
    d = Path(directory)
    target = os.fstat(int(descriptor)).st_ino
    with (d / 'count').open('a') as out:
        print(target, generation, os.getpid(), file=out)
    if (d / 'hold').exists() and (d / 'hold').read_text() == str(target):
        proxy(binary, directory, generation, descriptor, role)
    else:
        os.execv(binary, [binary, generation, descriptor, role])


def snapshot(directory, units):
    nodes = {ROOT: SCX / 'root'}
    rows = {}
    for unit in units:
        target = show(unit, 'TargetSlice')
        cgroup = CG / show(target, 'ControlGroup').lstrip('/')
        ino = cgroup.stat().st_ino
        parent = show(unit, 'ParentScheduler')
        if parent:
            nodes[unit] = nodes[parent] / 'sub' / f'sub-{ino}'
        group = CG / show(unit, 'ControlGroup').lstrip('/')
        generation = show(unit, 'InvocationID')
        launches = (directory / 'count').read_text().splitlines()
        matching = [line.split() for line in launches if line.split()[1] == generation]
        assert len(matching) == 1 and int(matching[0][0]) == ino
        assert matching[0][2] in (group / 'cgroup.procs').read_text().split()
        rows[unit] = dict(link=int(show(unit, 'LinkID')), node=nodes[unit].stat().st_ino,
                          cgroup=ino, parent=parent, generation=generation, provider_pid=int(matching[0][2]))
    return rows


def job_id(unit):
    rows = ctl('list-jobs', '--no-legend', '--plain', '--', unit).stdout.splitlines()
    row, = [line.split() for line in rows if unit in line]
    return row[0]


def job_result(unit, job):
    subprocess.run(['journalctl', '--sync'], check=True, timeout=10)
    output = subprocess.check_output(['journalctl', '-b', '-o', 'json', '--no-pager', '_PID=1',
                                      f'UNIT={unit}', f'JOB_ID={job}'], text=True)
    results = [r['JOB_RESULT'] for line in output.splitlines() if 'JOB_RESULT' in (r := json.loads(line))]
    assert len(results) == 1, (job, unit, results)
    return results[0]


def run(binary, rows):
    if os.getuid() != 0 or subprocess.run(['systemd-detect-virt', '--quiet', '--vm']).returncode or \
            not subprocess.run(['systemd-detect-virt', '--quiet', '--container']).returncode:
        raise RuntimeError('dedicated full-system VM required')
    assert (SCX / 'state').read_text().strip() == 'disabled'
    tree = [ROOT, MID, BUILD, SIDE]
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix='scxkeep-', dir='/run') as directory, ExitStack() as files:
        d = Path(directory)
        marker = d / 'workload'
        def config(path, body):
            with path.open('x') as out:
                out.write(body)
            files.callback(path.unlink)
        def ready(units):
            wait_for(lambda: all(state(unit) == 'active' for unit in units), 'verified tree')
        root_drop = UNITS / '-.slice.d'
        if not root_drop.exists():
            root_drop.mkdir()
            files.callback(root_drop.rmdir)
        config(root_drop / '98-scxkeep.conf', '[Slice]\nScheduler=scxkeep@.scheduler\n')
        for name in ['scxkeep', 'scxnew']:
            config(UNITS / f'{name}@.scheduler',
                   f'[Scheduler]\nExecStart=/usr/bin/python3 {Path(__file__).resolve()} launch {binary} {d}\n')
        for name in GUEST_SLICES[:3]:
            config(UNITS / name, '[Slice]\nScheduler=scxkeep@.scheduler\n')
        config(UNITS / WORK, '[Service]\nSlice=scxkeep-build-ci.slice\nExecStart=/usr/bin/sleep infinity\n')
        blocker = 'scxkeep-block.service'
        hold_job = d / 'hold-job'
        config(UNITS / blocker, f"[Service]\nType=oneshot\nExecStart=/bin/sh -c 'while test -e {hold_job}; do sleep .02; done'\n")
        config(UNITS / MARK, f'[Unit]\nRequires={blocker}\nAfter={blocker}\n\n[Service]\nSlice=scxkeep-build-ci.slice\nType=oneshot\nExecStart=/usr/bin/touch {marker}\n')
        since = time.time()
        try:
            ctl('daemon-reload')
            ctl('start', WORK, SIDE)
            before = snapshot(d, tree)
            counts = (d / 'count').read_text()
            workload = int(show(WORK, 'MainPID'))
            ctl('daemon-reexec')
            ready(tree)
            after = snapshot(d, tree)
            assert after == before and counts == (d / 'count').read_text() and workload == int(show(WORK, 'MainPID'))
            assert MID in show(BUILD, 'PartOf').split() and MID in show(SIDE, 'PartOf').split()
            rows.append(dict(case='healthy-reexec', before=before, after=after, workload_pid=workload,
                             launches=len(counts.splitlines())))
            hold_job.touch()
            ctl('start', '--no-block', MARK)
            wait_for(lambda: state(blocker) == 'activating', 'independent ordering blocker')
            pending_job = job_id(MARK)
            assert not ctl('list-jobs', '--no-legend', '--plain', BUILD).stdout.strip()
            provider = before[BUILD]['provider_pid']
            os.kill(provider, signal.SIGSTOP)
            try:
                ctl('daemon-reexec')
                assert state(BUILD) == 'activating'
                hold_job.unlink()
                wait_for(lambda: state(blocker) == 'inactive', 'release independent ordering blocker')
                assert job_id(MARK) == pending_job and not marker.exists()
            finally:
                os.kill(provider, signal.SIGCONT)
            wait_for(marker.exists, 'queued workload admitted after reconfirmation')
            assert job_result(MARK, pending_job) == 'done'
            marker.unlink()
            rows.append(dict(case='queued-consumer-reexec', job=pending_job, provider_pid=provider))

            ctl('restart', MID)
            ready(tree)
            restarted = snapshot(d, tree)
            assert restarted[ROOT] == after[ROOT] and workload == int(show(WORK, 'MainPID'))
            assert all(restarted[u]['link'] != after[u]['link'] for u in (MID, BUILD, SIDE))
            rows.append(dict(case='restored-parent-restart', parent=MID, links={u: restarted[u]['link'] for u in tree}))
            ctl('stop', BUILD)

            (d / 'action').write_text(json.dumps('delay'))
            (d / 'hold').write_text(str(before[BUILD]['cgroup']))
            ctl('start', '--no-block', MARK)
            wait_for(lambda: (d / 'received').exists(), 'provider awaiting acknowledgement')
            jobs = {unit: job_id(unit) for unit in (BUILD, MARK)}
            generation = show(BUILD, 'InvocationID')
            counts = (d / 'count').read_text()
            ctl('daemon-reexec')
            assert state(BUILD) == 'activating' and not marker.exists() and show(BUILD, 'InvocationID') == generation
            assert {unit: job_id(unit) for unit in jobs} == jobs
            (d / 'release').touch()
            wait_for(marker.exists, 'admission after acknowledgement')
            ready(tree)
            wait_for(lambda: state(MARK) == 'inactive', 'oneshot completed')
            results = {unit: job_result(unit, job) for unit, job in jobs.items()}
            assert set(results.values()) == {'done'} and counts == (d / 'count').read_text()
            rows.append(dict(case='pending-ack-reexec', generation=generation, jobs=jobs, terminal_results=results))
            marker.unlink()
            ctl('stop', BUILD)
            (d / 'release').unlink()
            (d / 'action').write_text(json.dumps('delay-status'))
            ctl('start', '--no-block', MARK)
            wait_for(lambda: (d / 'status-held').exists(), 'withheld readiness for pending exit')
            jobs = {unit: job_id(unit) for unit in (BUILD, MARK)}
            generation = show(BUILD, 'InvocationID')
            owned = int(show(BUILD, 'LinkID'))
            libbpf = ctypes.CDLL('libbpf.so.1', use_errno=True)
            link = libbpf.bpf_link_get_fd_by_id(owned)
            assert link >= 0
            try:
                assert libbpf.bpf_link_detach(link) == 0
                leaf = SCX / 'root/sub' / f"sub-{before[MID]['cgroup']}" / 'sub' / f"sub-{before[BUILD]['cgroup']}"
                wait_for(lambda: not leaf.exists(), 'kernel exit pending')
                counts = (d / 'count').read_text()
                ctl('daemon-reexec')
                assert state(BUILD) == 'activating' and not marker.exists() and show(BUILD, 'InvocationID') == generation
                assert {unit: job_id(unit) for unit in jobs} == jobs
                (d / 'release').touch()
                wait_for(lambda: state(BUILD) == 'failed', 'restored terminal event')
                wait_for(lambda: not ctl('list-jobs', '--no-legend', '--plain', MARK).stdout.strip(), 'failed admission')
                results = {unit: job_result(unit, job) for unit, job in jobs.items()}
                assert results[BUILD] == 'failed' and results[MARK] == 'dependency', results
                assert not marker.exists() and counts == (d / 'count').read_text()
                reason = show(BUILD, 'FailureReason')
                assert '(kind=64, code=0)' in reason, reason
                rows.append(dict(case='pending-exit-reexec', link=owned, jobs=jobs, terminal_results=results,
                                 terminal=reason, premature_commands=0))
            finally:
                os.close(link)
                (d / 'release').touch()
                (d / 'hold').unlink()

            ctl('reset-failed', BUILD)
            ctl('stop', MID)
            middle_config = UNITS / GUEST_SLICES[0]
            middle_config.write_text('[Slice]\nScheduler=\n')
            ctl('daemon-reload')
            ctl('start', BUILD)
            assert show(BUILD, 'ParentScheduler') == ROOT
            old_link = show(BUILD, 'LinkID')
            middle_config.write_text('[Slice]\nScheduler=scxkeep@.scheduler\n')
            ctl('daemon-reload')
            ready([ROOT, BUILD])
            assert ctl('start', MARK, check=False).returncode != 0 and not marker.exists()
            assert show(BUILD, 'LinkID') == old_link and show(BUILD, 'ParentScheduler') == ROOT
            ctl('restart', BUILD)
            ctl('start', SIDE, MARK)
            assert show(BUILD, 'ParentScheduler') == MID and show(BUILD, 'LinkID') != old_link
            assert marker.exists() and workload == int(show(WORK, 'MainPID'))
            marker.unlink()
            rows.append(dict(case='reload-intermediate-binding', previous_parent=ROOT, parent=MID,
                             previous_link=old_link, link=show(BUILD, 'LinkID'), workload_pid=workload))

            before = snapshot(d, tree)
            limit = UNITS / f'{BUILD}.d'
            limit.mkdir()
            files.callback(limit.rmdir)
            config(limit / 'stop.conf', '[Unit]\nStopWhenUnneeded=yes\n')
            (UNITS / GUEST_SLICES[1]).write_text('[Slice]\nScheduler=scxnew@.scheduler\n')
            ctl('daemon-reload')
            ready(tree)
            assert show(GUEST_SLICES[1], 'ConfiguredScheduler') == NEW
            assert show(GUEST_SLICES[-1], 'EffectiveScheduler') == BUILD
            assert snapshot(d, tree) == before
            assert BUILD in show(WORK, 'Wants').split() and NEW not in show(WORK, 'Wants').split()
            assert not ctl('list-jobs', '--no-legend', '--plain', BUILD).stdout.strip()
            ctl('stop', BUILD)
            ctl('start', NEW)
            assert show(GUEST_SLICES[-1], 'EffectiveScheduler') == NEW
            after = snapshot(d, [ROOT, MID, NEW, SIDE])
            assert before[BUILD]['link'] != after[NEW]['link'] and before[SIDE] == after[SIDE]
            assert workload == int(show(WORK, 'MainPID'))
            rows.append(dict(case='reload-changed-binding', configured=NEW, before=before, after=after,
                             still_running_workload=workload))
            subprocess.run(['journalctl', '--sync'], check=True, timeout=10)
            journal = subprocess.check_output(['journalctl', '-b', '_PID=1', '--since', f'@{since:.6f}',
                                              '--grep=Reexecuting', '-o', 'cat', '--no-pager'], text=True).splitlines()
            assert len(journal) >= 3, journal
            rows.append(dict(case='journal', reexecutions=journal))
        finally:
            hold_job.unlink(missing_ok=True)
            for unit in [MARK, blocker, WORK, NEW, BUILD, SIDE, MID, ROOT, *reversed(GUEST_SLICES)]:
                ctl('stop', unit, check=False)
            files.close()
            ctl('daemon-reload')
    assert (SCX / 'state').read_text().strip() == 'disabled'


if __name__ == '__main__':
    if sys.argv[1:2] == ['launch']:
        launch(*sys.argv[2:])
    else:
        report = {'kernel': os.uname().release, 'cases': []}
        try:
            run(sys.argv[1], report['cases'])
            report['result'] = 'PASS'
        except Exception as exc:
            report['error'] = repr(exc)
            raise
        finally:
            print(json.dumps(report, indent=2), flush=True)
