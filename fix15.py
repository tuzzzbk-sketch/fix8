#!/usr/bin/env python3
"""Bounded baseline readiness wait before the existing BP13 API experiment.

Keep beside fix10.py, fix13.py, fix14.py; Python 3.6+ standard library only.
Requires applied fix13 + fix14. Only the copied test PREP block is updated.
  python3 fix15.py --dry-run
  python3 fix15.py
  python3 fix15.py --build --seed 52
  python3 fix15.py --summary --seed 52
  python3 fix15.py --undo
  python3 fix15.py --self-test

Each side waits at most 128 local falling edges for reset=0 and pl_trdy=1.
The original random draw, active Stack0 config, traffic, API low/high controls,
SVA and result gates stay intact. No baseline signal is driven by this wait.
Both sides are rechecked before starting the API. Unknown values never qualify.
Independent .fix15_backup; undo fix15 before fix14, and fix14 before fix13.
Use this script for builds while fix15 is applied. Logs are exclusive debug15.
The clock-based watchdog and inherited wall-clock/log-size limits abort stalls.
Passing a run is not full Format 2 qualification.
"""
import argparse
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import tempfile

try:
    import fix10
    import fix13
    import fix14
except ImportError:
    sys.exit('STOP: put fix15.py beside fix10.py, fix13.py and fix14.py')

TAG = 'M1DE_BP15_BASELINE_WAIT_V1'
OLD_PREP = fix13.PREP


def baseline_wait():
    lines = ['  // BEGIN ' + TAG,
             '  // ACTIVE status may precede sampled readiness. Never drive the baseline.',
             '  begin : m1de_bp15_baseline',
             '    bit baseline_done = 0;',
             '    fork : m1de_bp15_baseline_guard',
             '      begin',
             '        fork']
    for role in ('ds', 'us'):
        v = fix13.vif(role)
        lines += ['          begin',
                  '            `uvm_info("M1DE_BP15_WAIT", $sformatf("role=%s initial reset=%%b ready=%%b", %s.reset, %s.pl_trdy), UVM_NONE)' % (role.upper(), v, v),
                  '            for (int n = 0; n < 128; n++) begin',
                  '              // Falling edge avoids inspecting posedge NBA updates in the same delta.',
                  '              @(negedge %s.lclk);' % v,
                  "              if (%s.reset === 1'b0 && %s.pl_trdy === 1'b1) break;" % (v, v),
                  '            end',
                  "            if (%s.reset !== 1'b0 || %s.pl_trdy !== 1'b1)" % (v, v),
                  '              `uvm_fatal("M1DE_BP15", $sformatf("role=%s baseline timeout after 128 local edges: reset=%%b ready=%%b", %s.reset, %s.pl_trdy))' % (role.upper(), v, v),
                  '            `uvm_info("M1DE_BP15_BASELINE", $sformatf("role=%s reset=%%b ready=%%b observed before API", %s.reset, %s.pl_trdy), UVM_NONE)' % (role.upper(), v, v),
                  '          end']
    lines += ['        join', '        baseline_done = 1;', '      end', '      begin',
              '        repeat (256) @(posedge %s.lclk);' % fix13.vif('ds'),
              '        if (!baseline_done)',
              '          `uvm_fatal("M1DE_BP15", "Baseline watchdog expired; API not started")',
              '      end', '    join_any', '    disable m1de_bp15_baseline_guard;',
              '  end']
    for role in ('ds', 'us'):
        v = fix13.vif(role)
        lines += ["  if (%s.reset !== 1'b0 || %s.pl_trdy !== 1'b1)" % (v, v),
                  '    `uvm_fatal("M1DE_BP15", $sformatf("role=%s baseline changed before API: reset=%%b ready=%%b", %s.reset, %s.pl_trdy))' % (role.upper(), v, v)]
    lines += ['  // END ' + TAG, '']
    return '\n'.join(lines)


def upgraded_prep():
    result = OLD_PREP
    for role in ('ds', 'us'):
        v = fix13.vif(role)
        guard = ("  if (%s.reset !== 1'b0 || %s.pl_trdy !== 1'b1)\n" % (v, v) +
                 '    `uvm_fatal("M1DE_BP13", "%s baseline must be out of reset and ready")\n' % role.upper())
        if result.count(guard) != 1:
            raise ValueError('Unexpected fix13 readiness guard')
        result = result.replace(guard, '', 1)
    anchor = '  fork : m1de_bp13_prepare_guard\n'
    if result.count(anchor) != 1:
        raise ValueError('Unexpected fix13 preparation layout')
    return result.replace(anchor, baseline_wait() + anchor, 1)


NEW_PREP = upgraded_prep()


def summary(log, seed):
    result = fix13.analyze(log)
    ready_roles = set()
    diagnostics = []
    with log.open(errors='replace') as source:
        for line in source:
            if 'M1DE_BP15_WAIT' in line or 'M1DE_BP15_BASELINE' in line:
                diagnostics.append(line.rstrip()[:1600])
                diagnostics = diagnostics[-6:]
            match = re.search(r'\[M1DE_BP15_BASELINE\] role=(DS|US) reset=0 ready=1 observed before API', line)
            if match:
                ready_roles.add(match[1])
    print('LOG:', log)
    for line in diagnostics + list(result['tail']):
        print(line[:1600])
    print('SMOKE_GATE:', 'PASS' if fix13.smoke_ok(result) else 'INCOMPLETE_OR_FAIL')
    print('BASELINE_READY_GATE:', 'PASS' if ready_roles == {'DS', 'US'} else 'INCOMPLETE_OR_FAIL')
    for role in ('ds', 'us'):
        print('STALL_COVER_%s:' % role.upper(), result['covers'].get((role, 'c_tx_stall_then_accept')))
    print('SEED_GATE: requested=%d observed=%s %s' %
          (seed, sorted(result['seeds']), 'PASS' if fix13.seed_ok(result, seed) else 'FAIL'))
    passed = (ready_roles == {'DS', 'US'} and fix13.bp_ok(result) and fix13.seed_ok(result, seed))
    print('BACKPRESSURE_GATE:', 'PASS_THIS_RUN' if passed else 'NOT_QUALIFIED')
    print('NOTE: this gate covers this Streaming/F2 Stack0 FDI64 run only.')
    return passed


def edited(source):
    if TAG in source:
        if source.count(NEW_PREP) != 1 or source.count(TAG) != 2:
            raise ValueError('Unknown/altered fix15 block; exact edit refused')
        original = source.replace(NEW_PREP, OLD_PREP, 1)
        if edited(original) != source:
            raise ValueError('Unexpected fix15 block placement')
        return source
    if source.count(OLD_PREP) != 1 or fix13.edited(source)[1]:
        raise ValueError('Expected exactly applied BP13 experiment')
    if fix14.edit_test(source) != source:
        raise ValueError('Apply fix14 active Stack0 profile before fix15')
    return source.replace(OLD_PREP, NEW_PREP, 1)


def backup_for(target):
    return target.with_name(target.name + '.fix15_backup')


def profile(tb, original):
    target = tb / 'tests' / ('ts.' + fix10.TEST + '.sv')
    baseline = fix14.backup_for(target).read_bytes()
    if original != fix14.edit_test(baseline.decode('utf-8')).encode('utf-8'):
        raise ValueError('Test differs from the exact applied fix14 profile')
    options = tb / 'sim_run_options'
    base_options = fix14.backup_for(options).read_bytes()
    if options.read_bytes() != fix14.edit_options(base_options.decode('utf-8')).encode('utf-8'):
        raise ValueError('sim_run_options changed after fix14')
    fix14.check_vcs_options(tb)


def patch(target, dry_run=False, undo=False):
    backup = backup_for(target)
    if target.resolve() != target.absolute() or backup.is_symlink():
        raise ValueError('Symlink target/backup refused')
    current = target.read_bytes()
    original = backup.read_bytes() if backup.exists() else current
    after = edited(original.decode('utf-8')).encode('utf-8')
    if after == original:
        raise ValueError('fix15 marker exists without its original backup')
    if current not in (original, after):
        raise ValueError('Later source edit; exact patch/undo refused')
    if undo and not backup.exists():
        raise ValueError('No fix15 backup exists')
    destination = original if undo else after
    if current == destination:
        print('ALREADY_RESTORED' if undo else 'ALREADY_APPLIED: bounded baseline wait')
        return
    print('TEST:', target)
    if dry_run:
        print('DRY_RUN_OK: bounded DS/US readiness wait; original API, traffic and result gates retained')
        return
    if not backup.exists():
        with backup.open('xb') as out:
            out.write(original)
    fix14.atomic_write(target, destination)
    print('BACKUP:', backup)
    print('RESTORED: exact fix14 test' if undo else 'APPLIED: wait for actual ready before API; 128 local edges per side')
    if not undo:
        print('NEXT: python3 fix15.py --build --seed 52')


def self_test():
    original = fix14.edit_test(fix14.fixture())
    after = edited(original)
    assert edited(after) == after
    assert after.replace(NEW_PREP, OLD_PREP, 1) == original
    assert NEW_PREP.count('for (int n = 0; n < 128; n++)') == 2
    assert NEW_PREP.count('baseline changed before API') == 2
    assert 'repeat (256)' in NEW_PREP and 'disable m1de_bp15_baseline_guard;' in NEW_PREP
    assert NEW_PREP.index('M1DE_BP15_BASELINE') < NEW_PREP.index('.drive_pl_signal')
    for bad in (original + original, after.replace('n < 128', 'n < 999', 1),
                original.replace(fix14.BLOCK, ''),
                original.replace('ready=0 observed', 'other')):
        fix14.must_reject(lambda: edited(bad))
    with tempfile.TemporaryDirectory() as td:
        tb = Path(td)
        (tb / 'tests').mkdir()
        target = tb / 'tests' / ('ts.' + fix10.TEST + '.sv')
        target.write_text(original)
        patch(target, dry_run=True)
        assert target.read_text() == original and not backup_for(target).exists()
        patch(target)
        patch(target)
        assert target.read_text() == after
        target.write_text(after + '\n// later edit\n')
        fix14.must_reject(lambda: patch(target, undo=True))
        target.write_text(after)
        patch(target, undo=True)
        assert target.read_text() == original
        patch(target)
        # Register the upgraded expected block only for the inherited build validator.
        previous = fix13.PREP
        try:
            fix13.PREP = NEW_PREP
            assert fix13.edited(after) == (after, False)
        finally:
            fix13.PREP = previous
        fix14.backup_for(target).write_text(fix14.fixture())
        options = tb / 'sim_run_options'
        original_options = fix14.TESTNAME + ' +ntb_random_seed=50\n'
        fix14.backup_for(options).write_text(original_options)
        options.write_text(fix14.edit_options(original_options))
        profile(tb, original.encode())
        log = tb / 'synthetic.log'
        seed = './output/simvcsvlog +ntb_random_seed=52 run\n'
        ready = ''.join('[M1DE_BP15_BASELINE] role=%s reset=0 ready=1 observed before API\n' %
                        role for role in ('DS', 'US'))
        log.write_text(seed + ready + fix13.log_fixture())
        assert summary(log, 52)
        for extra in ('UVM_FATAL @ 1: [M1DE_BP15] baseline timeout\n',
                      'M1DE_BUILD_TIMEOUT\n',
                      './output/simvcsvlog +ntb_random_seed=50 run\n'):
            log.write_text(seed + ready + extra + fix13.log_fixture())
            assert not summary(log, 52)
        for bad_ready in ('', ready.replace('role=US', 'role=OTHER'),
                          ready.replace('reset=0', 'reset=x'), ready.replace('ready=1', 'ready=0')):
            log.write_text(seed + bad_ready + fix13.log_fixture())
            assert not summary(log, 52)


def log_name(args):
    name = args.log_name or ('compile_pipe_debug15_seed%d.log' % args.seed)
    if not re.fullmatch(r'compile_pipe_debug15_seed\d+(?:_[A-Za-z0-9-]+)?\.log', name):
        raise ValueError('Use compile_pipe_debug15_seedN[_suffix].log')
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    for flag in ('dry-run', 'undo', 'build', 'summary', 'self-test'):
        mode.add_argument('--' + flag, action='store_true')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--log-name')
    parser.add_argument('--vendor-root', default=os.environ.get('DIR_VIP', fix13.VENDOR_DEFAULT))
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--max-log-mb', type=int, default=128)
    args = parser.parse_args()
    if args.self_test:
        with contextlib.redirect_stdout(io.StringIO()):
            self_test()
        print('SELF_TEST_PASS: bounded-wait structure, exact source/option profile, backup/undo, inherited seed/fatal gates; no VCS validation')
        return 0
    if args.build or args.summary:
        if args.seed is None or not 1 <= args.seed <= 2147483647:
            raise ValueError('--build/--summary requires --seed from 1 to 2147483647')
    elif args.seed is not None or args.log_name is not None:
        raise ValueError('--seed/--log-name requires --build or --summary')
    if args.timeout < 30 or args.max_log_mb < 1:
        raise ValueError('Timeout must be >=30 seconds; max log must be >=1 MiB')
    topology = fix10.locate(None)
    project = fix10.project_for(topology)
    target = project / fix13.TEST_REL
    if args.summary:
        return 0 if summary(project / 'logs' / log_name(args), args.seed) else 2
    if args.undo:
        patch(target, undo=True)
        return 0
    original = backup_for(target).read_bytes() if backup_for(target).exists() else target.read_bytes()
    profile(topology.parent, original)
    fix13.preflight(project, topology, Path(args.vendor_root))
    if args.build:
        if target.read_bytes() != edited(original.decode()).encode():
            raise ValueError('Apply fix15 / resolve later edits before --build')
        print('PROFILE: active Stack0; bounded DS/US baseline wait; original BP13 API experiment', flush=True)
        # The common build verifier now recognizes this exact PREP revision.
        # This changes Python validation in this process, not fix13.py on disk.
        fix13.PREP = NEW_PREP
        fix13.summary = summary
        return fix13.build(project, topology, target, project / 'logs' / log_name(args),
                           args.timeout, args.max_log_mb, seed=args.seed)
    patch(target, dry_run=args.dry_run)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('INTERRUPTED: inspect the exclusive build log before rerun', file=sys.stderr)
        sys.exit(130)
