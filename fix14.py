#!/usr/bin/env python3
"""Select active Stack0 and remove the fixed seed from the copied integration TB.

Keep beside fix10.py and fix13.py. Python 3.6+, standard library only.
  python3 fix14.py --dry-run
  python3 fix14.py
  python3 fix14.py --build --seed 51
  python3 fix14.py --summary --seed 51
  python3 fix14.py --undo
  python3 fix14.py --self-test

Requires the applied fix13 backpressure experiment. Changes only the copied
test create_cfg and sim_run_options; consumes the original random draw before
selecting active Stack0 and mapping it to DS/US. The overlay guard stays intact.
Preserves UVM_TESTNAME and other options. SEED is supplied by fix13/gmake.
Independent .fix14_backup files; undo fix14 before undoing fix13.
Build uses exclusive debug14 logs and all existing fix13 simulation/SVA gates.
Passing one seed is not full Format 2 or multi-seed qualification.
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
except ImportError:
    sys.exit('STOP: put fix14.py beside fix10.py and fix13.py')

TAG = 'M1DE_CFG14_ACTIVE_STACK0_V1'
BLOCK = ("  // BEGIN " + TAG + "\n"
         "  // Keep the original random draw; qualify active Stack0 across seeds.\n"
         "  is_stack0_passive = 1'b0;\n"
         "  // END " + TAG + "\n")
CFG_PATTERN = re.compile(
    r'(?P<draw>^[ \t]*is_stack0_passive[ \t]*=[ \t]*'
    r'\$urandom_range\([ \t]*0[ \t]*,[ \t]*1[ \t]*\)[ \t]*;[ \t]*\r?\n)'
    r'(?P<mapping>^[ \t]*env_cfg\.include_ds_protocol_fdi_stack0_passive'
    r'[ \t]*=[ \t]*is_stack0_passive[ \t]*;[ \t]*\r?\n'
    r'^[ \t]*env_cfg\.include_us_protocol_fdi_stack0_passive'
    r'[ \t]*=[ \t]*is_stack0_passive[ \t]*;)', re.M)
SEED_TOKEN = re.compile(r'(?<!\S)\+ntb_random_seed=50(?=\s|$)')
TESTNAME = '+UVM_TESTNAME=ucie_opfmt_snps_e2e_test'


def edit_test(source):
    if TAG in source:
        if source.count(BLOCK) != 1 or source.count(TAG) != 2:
            raise ValueError('Unknown/altered fix14 block; refusing edit')
        original = source.replace(BLOCK, '', 1)
        if edit_test(original) != source:
            raise ValueError('fix14 block is outside the expected cfg mapping')
        return source
    if fix13.edited(source)[1]:
        raise ValueError('Apply fix13 before fix14')
    funcs = list(re.finditer(
        r'\bfunction\s+void\s+' + re.escape(fix10.TEST) +
        r'::create_cfg\s*\(\s*\)\s*;', source))
    if len(funcs) != 1:
        raise ValueError('Expected one out-of-line create_cfg function')
    start = funcs[0].end()
    stop = re.search(r'\bendfunction\b', source[start:])
    if stop is None:
        raise ValueError('create_cfg endfunction missing')
    body = source[start:start + stop.start()]
    matches = list(CFG_PATTERN.finditer(body))
    if len(matches) != 1:
        raise ValueError('Expected one random draw immediately before DS/US passive mapping')
    if len(re.findall(r'\bis_stack0_passive\s*=(?!=)', fix13.clean(body))) != 1:
        raise ValueError('Unexpected extra Stack0 assignment in create_cfg')
    if len(re.findall(r'\binclude_(?:ds|us)_protocol_fdi_stack0_passive\s*=(?!=)',
                      fix13.clean(body))) != 2:
        raise ValueError('Unexpected extra DS/US passive assignment in create_cfg')
    point = start + matches[0].start('mapping')
    return source[:point] + BLOCK + source[point:]


def check_options(source):
    first = source.splitlines()[0] if source.splitlines() else ''
    if first.split().count(TESTNAME) != 1:
        raise ValueError('Expected exactly one E2E UVM_TESTNAME in first options line')
    if len(re.findall(r'(?<!\S)\+UVM_TESTNAME=', first)) != 1:
        raise ValueError('Conflicting UVM_TESTNAME options')
    return first


def edit_options(source):
    first = check_options(source)
    if len(SEED_TOKEN.findall(first)) != 1 or first.count('+ntb_random_seed') != 1:
        raise ValueError('Expected exactly one fixed +ntb_random_seed=50 in first options line')
    # Remove only that token; retain every other byte, including later lines.
    end = len(first)
    return SEED_TOKEN.sub('', source[:end], count=1) + source[end:]


def backup_for(path):
    return path.with_name(path.name + '.fix14_backup')


def atomic_write(path, content):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.fix14_', dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def pairs(tb):
    return [(tb / 'tests' / ('ts.' + fix10.TEST + '.sv'), edit_test),
            (tb / 'sim_run_options', edit_options)]


def check_vcs_options(tb):
    path = tb / 'vcs_run_options'
    if path.is_symlink():
        raise ValueError('vcs_run_options is a symlink; inspect its target first')
    if path.exists():
        lines = path.read_text().splitlines()
        if lines and '+ntb_random_seed' in lines[0]:
            raise ValueError('vcs_run_options also adds a seed; no change made')


def transaction(tb, dry_run=False, undo=False):
    check_vcs_options(tb)
    planned = []
    for path, edit in pairs(tb):
        backup = backup_for(path)
        if path.resolve() != path.absolute() or backup.is_symlink():
            raise ValueError('Symlink target/backup refused: ' + str(path))
        current = path.read_bytes()
        if backup.exists():
            original = backup.read_bytes()
            expected = edit(original.decode('utf-8')).encode('utf-8')
            if current not in (original, expected):
                raise ValueError('Later edit/conflicting backup; refusing overwrite: ' + str(path))
        else:
            if undo:
                raise ValueError('Missing fix14 backup: ' + str(path))
            original = current
            expected = edit(original.decode('utf-8')).encode('utf-8')
            if expected == original:
                raise ValueError('fix14 marker exists without its original backup')
        planned.append((path, backup, current, original, expected))
    # Refuse a mixed state rather than silently finishing an interrupted edit.
    if not (all(row[2] == row[3] for row in planned) or
            all(row[2] == row[4] for row in planned)):
        raise ValueError('Mixed fix14 state; inspect both files/backups before continuing')
    destination = 3 if undo else 4
    if all(row[2] == row[destination] for row in planned):
        print('ALREADY_RESTORED' if undo else 'ALREADY_APPLIED: active Stack0; fixed seed removed')
        return
    for path, backup, current, original, expected in planned:
        print('FILE:', path)
    if dry_run:
        print('DRY_RUN_OK: original RNG draw and traffic preserved; Stack0 active; fixed seed removed')
        return
    # Finish all validation and create both immutable backups before editing.
    for path, backup, current, original, expected in planned:
        if not backup.exists():
            with backup.open('xb') as out:
                out.write(original)
        print('BACKUP:', backup)
    changed = []
    try:
        for row in planned:
            atomic_write(row[0], row[destination])
            changed.append(row)
    except BaseException:
        for row in reversed(changed):
            atomic_write(row[0], row[2])
        raise
    print('RESTORED: fix13 test and fixed seed options' if undo else
          'APPLIED: active Stack0 DS/US; seed supplied only through gmake SEED')
    if not undo:
        print('NEXT: python3 fix14.py --build --seed 51')


def require_applied(tb):
    check_vcs_options(tb)
    for path, edit in pairs(tb):
        original = backup_for(path).read_bytes()
        if path.read_bytes() != edit(original.decode('utf-8')).encode('utf-8'):
            raise ValueError('Apply fix14 / resolve later edits before --build')
    first = check_options((tb / 'sim_run_options').read_text())
    if '+ntb_random_seed' in first:
        raise ValueError('Fixed seed remains in sim_run_options')


def fixture():
    return fix13.edited(fix13.fixture())[0] + '''
function void ucie_protocol_mainband_transfer_both_stack_streaming_multi_stack_test::create_cfg();
  super.create_cfg();
  local_dont_set_up_dp_in_advcap = $urandom_range(0,1);
  is_stack0_passive = $urandom_range(0,1);
  env_cfg.include_ds_protocol_fdi_stack0_passive = is_stack0_passive;
  env_cfg.include_us_protocol_fdi_stack0_passive = is_stack0_passive;
  env_cfg.is_both_stack_streaming = 1'b1;
endfunction
'''


def must_reject(call):
    try:
        call()
    except (ValueError, FileNotFoundError):
        return
    raise AssertionError('Unsafe input accepted')


def self_test():
    original = fixture()
    after = edit_test(original)
    assert edit_test(after) == after and fix13.edited(after) == (after, False)
    assert after.replace(BLOCK, '', 1) == original
    draw = 'is_stack0_passive = $urandom_range(0,1);'
    assert after.count(draw) == 1
    assert after.index(draw) < after.index("is_stack0_passive = 1'b0;")
    assert after.index("is_stack0_passive = 1'b0;") < after.index('env_cfg.include_ds_protocol')
    opts = TESTNAME + ' +ntb_random_seed=50 +EXTRA=keep\nsecond line unchanged\n'
    clean = edit_options(opts)
    assert clean == opts.replace('+ntb_random_seed=50', '', 1)
    for bad in (original + original, original.replace(draw, 'is_stack0_passive = 0;'),
                after.replace("is_stack0_passive = 1'b0;", "is_stack0_passive = 1'b1;"),
                original.replace('  env_cfg.is_both_stack_streaming',
                                 '  is_stack0_passive = 1;\n  env_cfg.is_both_stack_streaming')):
        must_reject(lambda: edit_test(bad))
    for bad in (opts.replace('=50', '=51'), opts + TESTNAME,
                opts.replace(TESTNAME, '+UVM_TESTNAME=other'),
                opts.replace('+EXTRA=keep', '+ntb_random_seed=50')):
        # Later options lines are deliberately not interpreted by the Makefile.
        if bad == opts + TESTNAME:
            assert edit_options(bad).endswith(TESTNAME)
        else:
            must_reject(lambda: edit_options(bad))
    with tempfile.TemporaryDirectory() as td:
        tb = Path(td)
        (tb / 'tests').mkdir()
        target, option = [item[0] for item in pairs(tb)]
        target.write_text(original)
        option.write_text(opts)
        transaction(tb, dry_run=True)
        assert target.read_text() == original and not backup_for(target).exists()
        # A failed second file write must restore the first file too.
        writer = globals()['atomic_write']
        count = [0]
        def fail_second(path, content):
            count[0] += 1
            if count[0] == 2:
                raise OSError('synthetic second write failure')
            writer(path, content)
        try:
            globals()['atomic_write'] = fail_second
            try:
                transaction(tb)
            except OSError:
                pass
            else:
                raise AssertionError('Expected synthetic write failure')
        finally:
            globals()['atomic_write'] = writer
        assert target.read_text() == original and option.read_text() == opts
        transaction(tb)
        require_applied(tb)
        transaction(tb)
        assert target.read_text() == after and option.read_text() == clean
        option.write_text(clean + '\nlater change\n')
        must_reject(lambda: transaction(tb, undo=True))
        assert target.read_text() == after
        option.write_text(clean)
        transaction(tb, undo=True)
        assert target.read_text() == original and option.read_text() == opts
        transaction(tb)
        (tb / 'vcs_run_options').write_text('+ntb_random_seed=50\n')
        must_reject(lambda: require_applied(tb))
        (tb / 'vcs_run_options').unlink()
        # Even though no gmake compile runs here, exercise the inherited gates.
        log = tb / 'synthetic.log'
        log.write_text('./output/simvcsvlog +ntb_random_seed=51 run\n' + fix13.log_fixture())
        assert fix13.summary(log, 51)
        for bad in ('+ntb_random_seed=51 +ntb_random_seed=50', '+ntb_random_seed=50'):
            log.write_text('./output/simvcsvlog ' + bad + ' run\n' + fix13.log_fixture())
            assert not fix13.summary(log, 51)
        log.write_text('./output/simvcsvlog +ntb_random_seed=51 run\nUVM_FATAL @ 0: fail\n' +
                       fix13.log_fixture())
        assert not fix13.summary(log, 51)


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
        print('SELF_TEST_PASS: cfg placement/RNG preservation, option preservation, backups, exact undo, seed/fatal gates; no VCS validation')
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
    tb = topology.parent
    if args.summary:
        name = args.log_name or ('compile_pipe_debug14_seed%d.log' % args.seed)
        if not re.fullmatch(r'compile_pipe_debug14_seed\d+(?:_[A-Za-z0-9-]+)?\.log', name):
            raise ValueError('Use compile_pipe_debug14_seedN[_suffix].log')
        return 0 if fix13.summary(project / 'logs' / name, args.seed) else 2
    if args.undo:
        transaction(tb, undo=True)
        return 0
    fix13.preflight(project, topology, Path(args.vendor_root))
    if args.build:
        require_applied(tb)
        name = args.log_name or ('compile_pipe_debug14_seed%d.log' % args.seed)
        if not re.fullmatch(r'compile_pipe_debug14_seed\d+(?:_[A-Za-z0-9-]+)?\.log', name):
            raise ValueError('Use compile_pipe_debug14_seedN[_suffix].log')
        print('PROFILE: active Stack0 DS/US; original RNG draw retained; no fixed seed in option files', flush=True)
        return fix13.build(project, topology, project / fix13.TEST_REL,
                           project / 'logs' / name, args.timeout, args.max_log_mb, seed=args.seed)
    transaction(tb, dry_run=args.dry_run)
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
