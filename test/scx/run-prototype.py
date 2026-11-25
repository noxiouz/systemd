#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Build and run the complete sched_ext prototype on two disposable stock-kernel VMs."""

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BASE = '/var/tmp/scx'
TEST = f'{BASE}/src/test/scx'
MIXED = f'{BASE}/mixed'
QMAP = f'{BASE}/linux/tools/sched_ext/build/bin/scx_qmap'
PROVIDER = 'c03c1a14507dfaf8b856c2608f9ac216021fc222'
GUEST_PATH = 'PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
MESON_TESTS = ['test-engine', 'test-bus-unit-util', 'test-unit-file', 'test-unit-name', 'test-scheduler']
EXPECTED = {
    '01': ['supported', 'inconclusive', 'four-attachments-and-inheritance', 'unsupported'],
    '02': ['attachment-identity-and-loss'],
    '03': ['resolver', 'verify-valid', 'wrong', 'missing', 'conflict'],
    '04': ['activation-transactions', 'native-service-admission'],
    '05': ['mixed-dispatch-inheritance-and-partial'],
    '06': ['native-lifecycle'],
    '07': ['strict-admission'],
    '08': ['observability'],
    '09': ['branch-restart-and-loss'],
    '10': ['reload-and-reexec'],
    '11': ['cold-boot-and-recovery', 'root-only-kernel'],
}
PREFLIGHT = {
    'kernel': ('01', EXPECTED['01'][:-1]),
    'identity': ('02', EXPECTED['02']),
    'mixed': ('05', EXPECTED['05']),
}
ROWS = {
    '02': ['missing-parent', 'root-without-delegation', 'no-sub-attach', 'root', 'wrong-target',
           'configured-intermediate', 'wrong-parent', 'capable-parent', 'parent-veto', 'same-algorithm-child',
           'explicit-owned-detach', 'unrelated-sibling-after-detach', 'same-name-replacement', 'previous-start-reply',
           'dropped-loss-notification', 'recreated-cgroup', 'recreated-cgroup-old-instance'],
    '06': ['delayed-direct-start', 'wrong', 'stale', 'die', 'timeout', 'recreated-target',
           'cancel-in-flight-attachment', 'owned-stop-with-duplicate', 'retry-owned-detach', 'occupied-target',
           'clean-restart', 'short-startup-timeout-after-readiness'],
    '08': ['kernel-default', 'managed', 'evicted', 'foreign-replacement', 'foreign-inherited-admission'],
    '10': ['healthy-reexec', 'restored-parent-restart', 'pending-ack-reexec', 'pending-exit-reexec',
           'reload-changed-binding', 'reload-intermediate-binding', 'queued-consumer-reexec', 'journal'],
}
GROUPS = {
    '05': {'probes': ['root', 'middle', 'inheriting', 'sibling_before', 'sibling_after'], 'partial': ['ext', 'ordinary']},
    '07': {'entry_points': ['normalcold', 'normalwarm', 'transientcold', 'transientwarm', 'socketcold', 'socketwarm'],
           'failures': ['veto', 'timeout'], 'recovery': ['veto', 'timeout'], 'concurrent_starts': ['shared_loader_starts'],
           'loss_after_admission': ['existing_pid', 'new_start']},
    '09': {'restart': ['detach_order', 'restoration', 'sibling_dispatch', 'build_dispatch'],
           'intermediate_conflict': ['preserved', 'errno'], 'leaf_loss': ['effective', 'fallback_dispatch'],
           'root_loss': ['effective', 'restored']},
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def image_digest(config):
    return re.search(r'^  digest: sha256:([a-f0-9]{64})$', config.read_text(), re.M)[1]


def verify_stock(report, reference):
    for field in ('kernel', 'image_sha256', 'kernel_sha256', 'kernel_config_sha256', 'btf_sha256'):
        if report[field] != reference[field]:
            raise RuntimeError(f'{field} differs from the stock image reference')


def git(*args):
    return subprocess.check_output(['git', '-C', REPO, *args], text=True).strip()


class Replay:
    def __init__(self, output, work, revision, preflight):
        self.preflight = preflight
        self.expected = dict([PREFLIGHT[preflight]]) if preflight else EXPECTED
        self.out = output
        self.work = work
        self.revision = revision
        self.env = dict(os.environ, LIMA_HOME=str(work / 'lima'), TMPDIR=str(work))
        self.guests = []
        self.cases = {}
        self.running_case = None
        self.observations = []
        self.log = output / 'commands.log'

    def command(self, args, *, dest=None, expected=0):
        args = list(map(str, args))
        with self.log.open('ab') as log:
            log.write(f'\n$ {shlex.join(args)}\n'.encode())
            log.flush()
            if dest is None:
                p = subprocess.run(args, env=self.env, stdout=log, stderr=subprocess.STDOUT)
            else:
                with dest.open('wb') as out:
                    p = subprocess.run(args, env=self.env, stdout=out, stderr=log)
            if expected is not None and p.returncode != expected:
                raise RuntimeError(f'{shlex.join(args)} exited {p.returncode}, expected {expected}; see {self.log}')
            return p.returncode

    def boot(self, name, config):
        self.guests.append(name)
        self.command(['limactl', 'start', f'--name={name}', '--tty=false', config])

    def guest(self, name, *args, **kwargs):
        return self.command(['limactl', 'shell', '--workdir=/tmp', name, '--', *args], **kwargs)

    def copy(self, src, dst):
        self.command(['limactl', 'copy', src, dst])

    def case(self, ticket, name, guest, *args, rc=0, json_output=True, stock=None, hierarchy=None, diagnostic=None):
        print(f'SCX-{ticket} {name}', flush=True)
        path = self.out / f'{ticket}-{name}.{"json" if json_output else "log"}'
        self.running_case = (ticket, name, path.name)
        self.guest(guest, *args, dest=path, expected=rc)
        data = json.loads(path.read_text()) if json_output else None
        if isinstance(data, dict) and ('error' in data or data.get('result') in ('FAIL', 'SKIP')):
            raise RuntimeError(f'{name}: {data}')
        if stock:
            verify_stock(data, stock)
        if hierarchy and data['hierarchy'] != hierarchy:
            raise RuntimeError(f'{name}: {data}')
        if diagnostic and diagnostic not in path.read_text():
            raise RuntimeError(f'{name}: missing diagnostic {diagnostic}')
        if ticket in ROWS:
            names = [row['case'] for row in data['cases']]
            if sorted(names) != sorted(ROWS[ticket]):
                raise RuntimeError(f'{name}: unexpected cases {names}')
            self.observations.extend(dict(ticket='SCX-' + ticket, name=label, result='PASS',
                                          evidence=f'{path.name}#/cases/{i}') for i, label in enumerate(names))
        for group, keys in GROUPS.get(ticket, {}).items():
            for key in keys:
                if key not in data[group]:
                    raise RuntimeError(f'{name}: missing {group}/{key}')
                self.observations.append(dict(ticket='SCX-' + ticket, name=f'{group}/{key}', result='PASS',
                                              evidence=f'{path.name}#/{group}/{key}'))
        self.cases[ticket, name] = path.name
        self.running_case = None
        return data

    def run(self):
        positive, negative = 'reference', 'control'
        pcfg, ncfg = HERE / 'reference.yaml', HERE / 'replay/negative.yaml'
        dig, negdig = image_digest(pcfg), image_digest(ncfg)
        reference = json.loads((HERE / 'replay/kernels.json').read_text())
        archive = self.work / 'source.tar'
        self.command(['git', '-C', REPO, 'archive', '--format=tar', '-o', archive, self.revision])
        self.boot(positive, pcfg)
        self.copy(archive, f'{positive}:/tmp/scx-source.tar')
        self.guest(positive, 'sh', '-ec', f'mkdir -p {BASE}/src; tar xf /tmp/scx-source.tar -C {BASE}/src')
        self.guest(positive, 'bash', f'{TEST}/replay/build.sh', PROVIDER, self.preflight or 'full')
        root = ['sudo', 'env', GUEST_PATH]
        py = [*root, 'python3']
        image = ['--image-sha256', dig]
        provider = ['--provider-revision', PROVIDER]
        identity = MIXED + '/identity-provider'
        if self.preflight in (None, 'kernel'):
            for name, extra, code, status in [('supported', [], 0, 'supported'),
                                              ('inconclusive', ['--btf', '/does-not-exist'], 1, 'unknown')]:
                self.case('01', name, positive, *py, f'{TEST}/baseline.py', '--probe', *extra, rc=code, hierarchy=status)
            self.case('01', 'four-attachments-and-inheritance', positive, *py, f'{TEST}/baseline.py',
                      '--loader', QMAP, *image, *provider, '--repo-revision', self.revision, stock=reference['hierarchical'])
        if self.preflight in (None, 'identity'):
            self.case('02', 'attachment-identity-and-loss', positive, *py, f'{TEST}/identity/matrix.py', identity,
                      *image, *provider)
        if self.preflight in (None, 'mixed'):
            self.case('05', 'mixed-dispatch-inheritance-and-partial', positive, *py, f'{TEST}/identity/dispatch.py',
                      MIXED, *image, *provider, '--fixture-revision', self.revision)

        if self.preflight:
            return

        self.guest(positive, 'sh', '-ec', f'cd {BASE}; sha256sum package.tar package/systemd package/systemd-executor '
                   'package/systemctl package/systemd-analyze mixed/identity-provider mixed/fifo-provider '
                   'mixed/simple-provider linux/tools/sched_ext/build/bin/scx_qmap',
                   dest=self.out / 'build-artifacts.sha256')

        mlog = self.out / 'meson-verbose.log'
        self.guest(positive, 'bash', '-ec', 'sudo systemd-run --quiet --pipe --wait --collect -p User="$(id -un)" '
                   f'-p Delegate=yes --working-directory={BASE} /usr/bin/meson test -C build -j1 -v "$@"',
                   'sh', *MESON_TESTS, dest=mlog)
        meson = self.out / 'meson-testlog.jsonl'
        self.copy(f'{positive}:{BASE}/build/meson-logs/testlog.json', meson)
        records = {d['name'].rsplit(' / ', 1)[-1].rsplit(':', 1)[-1]: d['result']
                   for d in map(json.loads, meson.read_text().splitlines())}
        if records != dict.fromkeys(MESON_TESTS, 'OK'):
            raise RuntimeError(f'required Meson tests: {records}')
        self.cases['03', 'resolver'] = self.cases['04', 'activation-transactions'] = meson.name
        fixtures = BASE + '/src/test/test-scheduler'
        verify = ['env', f'SYSTEMD_UNIT_PATH={fixtures}:', BASE + '/build/systemd-analyze', 'verify']
        self.case('03', 'verify-valid', positive, *verify, *(f'{fixtures}/{f}.slice' for f in
                  ['lab', 'lab-build', 'interactive']), json_output=False)
        for name, setting in [('wrong', 'Scheduler=batch@wrong.service'), ('missing', 'Scheduler=absent@missing.scheduler'),
                              ('conflict', 'Scheduler=batch@lab.scheduler')]:
            # Preserve stderr from the verifier in the case evidence.
            self.case('03', name, positive, 'sh', '-c', 'exec "$@" 2>&1', 'sh', *verify, f'{fixtures}/{name}.slice',
                      rc=1, json_output=False, diagnostic=setting)

        self.guest(positive, *root, 'bash', f'{TEST}/replay/deploy.sh', self.revision)
        self.guest(positive, *root, 'install', '-Dm755', identity, '/usr/local/libexec/scx-test-provider')
        self.case('04', 'native-service-admission', positive, *root, 'bash',
                  f'{BASE}/src/test/units/TEST-99-SCHED-EXT.sh', json_output=False)
        for ticket, path, arg in [('06', 'lifecycle.py', identity), ('07', 'admission/run.py', MIXED),
                                  ('08', 'observability.py', identity), ('09', 'branch.py', MIXED),
                                  ('10', 'reexecution.py', identity)]:
            self.case(ticket, EXPECTED[ticket][0], positive, *py, f'{TEST}/{path}', arg)
        self.guest(positive, *py, f'{TEST}/boot/guest.py', 'install', identity)
        boot_meta = self.out / 'boot-guest.json'
        self.guest(positive, *py, f'{TEST}/boot/guest.py', 'metadata', dig, dest=boot_meta)

        self.guest(positive, *root, 'sh', '-ec', 'cp /boot/vmlinuz-"$(uname -r)" /tmp/scx-kernel; '
                   'cp /boot/initrd.img-"$(uname -r)" /tmp/scx-initrd; chmod 644 /tmp/scx-kernel /tmp/scx-initrd')
        for remote, local in [('/tmp/scx-kernel', 'kernel'), ('/tmp/scx-initrd', 'initrd'), ('/usr/bin/bash', 'bash'),
                              (BASE + '/package.tar', 'package.tar')]:
            self.copy(f'{positive}:{remote}', self.work / local)
        cmdline = self.out / 'guest-cmdline.txt'
        self.guest(positive, 'cat', '/proc/cmdline', dest=cmdline)
        root_arg = next(a[5:] for a in cmdline.read_text().split() if a.startswith('root='))
        self.command(['limactl', 'stop', positive])
        disk = Path(self.env['LIMA_HOME']) / positive / 'disk'
        diskinfo = self.work / 'disk.json'
        self.command(['qemu-img', 'info', '--output=json', disk], dest=diskinfo)
        cold = self.out / '11-cold-boot-and-recovery.json'
        self.running_case = ('11', 'cold-boot-and-recovery', cold.name)
        self.command([sys.executable, HERE / 'boot/cold.py', '--disk', disk, '--format', json.loads(diskinfo.read_text())['format'],
                      '--kernel', self.work / 'kernel', '--initrd', self.work / 'initrd', '--bash', self.work / 'bash',
                      '--metadata', boot_meta, '--root', root_arg, '--cpus', '2', '--logs', self.out / 'serial'], dest=cold)
        if [c['case'] for c in json.loads(cold.read_text())['cases']] != ['delay', 'missing', 'recovery', 'control']:
            raise RuntimeError('incomplete boot matrix')
        self.cases['11', 'cold-boot-and-recovery'] = cold.name
        self.running_case = None
        self.observations.extend(dict(ticket='SCX-11', name=c['case'], result='PASS', evidence=f'{cold.name}#/cases/{i}')
                                 for i, c in enumerate(json.loads(cold.read_text())['cases']))

        self.boot(negative, ncfg)
        self.copy(archive, f'{negative}:/tmp/scx-source.tar')
        self.copy(self.work / 'package.tar', f'{negative}:/tmp/scx-package.tar')
        self.guest(negative, 'sh', '-ec', f'mkdir -p {BASE}/src; tar xf /tmp/scx-source.tar -C {BASE}/src; '
                   f'mv /tmp/scx-package.tar {BASE}/package.tar')
        self.guest(negative, *root, 'bash', f'{TEST}/replay/deploy.sh', self.revision)
        self.case('01', 'unsupported', negative, *py, f'{TEST}/baseline.py', '--probe', rc=77, hierarchy='unsupported')
        self.case('11', 'root-only-kernel', negative, *py, f'{TEST}/boot/unsupported.py',
                  f'/opt/scx-replay/{self.revision}/simple-provider', '--image-sha256', negdig, stock=reference['root_only'])
        if set(self.cases) != {(t, c) for t, cases in EXPECTED.items() for c in cases}:
            raise RuntimeError('incomplete reference run')

    def close(self):
        for name in self.guests:
            self.command(['limactl', 'delete', '--force', name], expected=None)

    def failure_logs(self):
        for name in self.guests:
            self.guest(name, 'sudo', 'journalctl', '-b', '-n', '500', '--no-pager', '-o', 'short-monotonic',
                       dest=self.out / f'{name}-journal.log', expected=None)

    def report(self, error):
        paths = sorted(p for p in self.out.rglob('*') if p.is_file())
        cases = []
        for ticket, names in self.expected.items():
            for name in names:
                evidence = self.cases.get((ticket, name))
                result = 'PASS' if evidence else 'INCOMPLETE'
                if self.running_case and self.running_case[:2] == (ticket, name):
                    result, evidence = 'FAIL', self.running_case[2]
                cases.append(dict(ticket='SCX-' + ticket, name=name, result=result, evidence=evidence))
        write_json(self.out / 'manifest.json', dict(
            revision=self.revision, tree=git('rev-parse', self.revision + '^{tree}'), provider_revision=PROVIDER,
            host=platform.uname()._asdict(), result='FAIL' if error else 'PASS', error=error,
            observations=self.observations, cases=cases,
            sha256={str(p.relative_to(self.out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='new or empty directory for logs, fixture reports and manifest')
    parser.add_argument('--preflight', choices=PREFLIGHT,
                        help='only test the selected provider or kernel capability in one guest')
    args = parser.parse_args()
    if git('status', '--porcelain'):
        parser.error('use a clean checkout of the revision to test')
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        parser.error('output directory must be empty')
    # /tmp is commonly a RAM-backed filesystem smaller than the two VM disks.
    with tempfile.TemporaryDirectory(prefix='scx-replay-', dir='/var/tmp') as tmp:
        replay = Replay(args.output.resolve(), Path(tmp), git('rev-parse', 'HEAD'), args.preflight)
        error = None
        try:
            replay.run()
        except Exception as exc:
            error = str(exc) or repr(exc)
            replay.failure_logs()
            raise
        finally:
            replay.close()
            replay.report(error)
    print(args.output / 'manifest.json')


if __name__ == '__main__':
    main()
