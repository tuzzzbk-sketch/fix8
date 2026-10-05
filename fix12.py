#!/usr/bin/env python3
"""Explore FDI backpressure with the existing Streaming/F2 test.

Keep beside fix10.py in incoming/fix8. No third-party Python packages.
python3 fix12.py             # backup + apply experimental configuration
python3 fix12.py --build     # foreground compile/run + concise result summary
python3 fix12.py --summary   # read the existing debug12 log
python3 fix12.py --undo      # restore the copied test if no later edits exist

DS/US active adapter configs and enabled passive mirrors get implicit
backpressure=1 and tx_retry_buffer_size=2. The value 2 is exploratory,
not a documented minimum or a qualified setting. Existing traffic, retry,
protocol, topology, core SVA, payload extraction and vendor installation
are not edited. Stall coverage must be observed; applying knobs is no proof.
Local self-test uses synthetic source and fake gmake, not VCS.
"""
import argparse
import contextlib
import io
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

try:
    import fix10
except ImportError:
    sys.exit('STOP: put fix12.py beside fix10.py')

BEGIN = '// BEGIN M1DE_BP12_EXPERIMENT_V1'
END = '// END M1DE_BP12_EXPERIMENT_V1'
TEST_REL = fix10.TB_REL / 'tests' / ('ts.' + fix10.TEST + '.sv')
VENDOR_DEFAULT = '/u/svc-uciephyverif/p4_ws/designware_home/ucie_vip'


def clean(source):
    return re.sub(r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), source)


def payload(size):
    lines = ['  ' + BEGIN,
             '  // Experimental buffer size; verify vendor acceptance and raw FDI covers.',
             '  if (env_cfg.ds_d2d_adapter_cfg == null || env_cfg.us_d2d_adapter_cfg == null)',
             '    `uvm_fatal("M1DE_BP12_CFG", "DS/US active adapter config missing")']
    for role in ('ds', 'us'):
        cfg = 'env_cfg.' + role + '_d2d_adapter_cfg'
        lines += ["  if (%s.retry !== 1'b1)" % cfg,
                  '    `uvm_fatal("M1DE_BP12_CFG", "%s retry must already be enabled")' % role.upper(),
                  "  %s.enable_dda_implicit_backpressure = 1'b1;" % cfg,
                  "  %s.tx_retry_buffer_size = 8'd%d;" % (cfg, size),
                  '  `uvm_info("M1DE_BP12_CFG", $sformatf("role=%s retry=%%0b implicit_bp=%%0b tx_retry_buffer_size=%%0d EXPERIMENT", %s.retry, %s.enable_dda_implicit_backpressure, %s.tx_retry_buffer_size), UVM_NONE)' % (role.upper(), cfg, cfg, cfg)]
        mirror = 'env_cfg.' + role + '_d2d_adapter_passive_cfg'
        lines += ["  if (env_cfg.include_%s_d2d_adapter_passive === 1'b1) begin" % role,
                  '    if (%s == null)' % mirror,
                  '      `uvm_fatal("M1DE_BP12_CFG", "%s passive adapter config missing")' % role.upper(),
                  "    %s.enable_dda_implicit_backpressure = 1'b1;" % mirror,
                  "    %s.tx_retry_buffer_size = 8'd%d;" % (mirror, size),
                  '  end']
    lines.append('  ' + END)
    return '\n'.join(lines) + '\n'


def edited(source, size=2):
    block = payload(size)
    if BEGIN in source or END in source:
        if source.count(block) == 1 and source.count(BEGIN) == source.count(END) == 1:
            return source, False
        raise ValueError('Existing BP12 block differs; undo with its original size first')
    scrubbed = clean(source)
    declaration = r'\bfunction\s+void\s+' + re.escape(fix10.TEST) + r'\s*::\s*create_cfg\s*\(\s*\)\s*;'
    starts = list(re.finditer(declaration, scrubbed))
    if len(starts) != 1:
        raise ValueError('Expected exactly one concrete create_cfg() function')
    start = starts[0].end()
    finish = re.search(r'\bendfunction\b', scrubbed[start:])
    if not finish:
        raise ValueError('create_cfg has no endfunction')
    stop = start + finish.start()
    body = scrubbed[start:stop]
    if not re.search(r'\bbit\s+enable_retry\s*=\s*1\s*;', body):
        raise ValueError('Expected local enable_retry=1 baseline missing')
    for role in ('ds', 'us'):
        if not re.search(r'\benv_cfg\s*\.\s*' + role + r'_d2d_adapter_cfg\s*\.\s*retry\s*=\s*enable_retry\s*;', body):
            raise ValueError('Expected retry assignment missing for ' + role)
        if 'include_' + role + '_d2d_adapter_passive' not in body:
            raise ValueError('Expected optional passive mirror guard missing for ' + role)
    if re.search(r'\b(?:tx_retry_buffer_size|enable_dda_implicit_backpressure)\b', body):
        raise ValueError('Test already sets experimental knobs; refusing an override')
    # Insert outside the completed DS/US loops, immediately before the final info.
    exits = list(re.finditer(r'^[ \t]*`uvm_info\(\s*"create_cfg"\s*,\s*"Exiting\.\.\."\s*,\s*UVM_MEDIUM\s*\)[ \t]*;?[ \t]*(?:\n|$)', source[start:stop], re.M))
    if len(exits) != 1:
        raise ValueError('Expected one final create_cfg Exiting info anchor')
    anchor = start + exits[0].start()
    if clean(source[start + exits[0].end():stop]).strip():
        raise ValueError('create_cfg has statements after the final info anchor')
    # Macro branch mismatches would make a source-position patch ambiguous.
    depth = 0
    for directive in re.finditer(r'^\s*`(ifdef|ifndef|endif)\b', body, re.M):
        depth += -1 if directive[1] == 'endif' else 1
        if depth < 0:
            raise ValueError('create_cfg contains an unmatched preprocessor branch')
    if depth:
        raise ValueError('create_cfg contains an unclosed preprocessor branch')
    return source[:anchor] + block + source[anchor:], True


def patch(target, size, dry_run=False, undo=False):
    before = target.read_bytes()
    backup = target.with_name(target.name + '.fix12_backup')
    if undo:
        original = backup.read_bytes()
        expected, changed = edited(original.decode('utf-8'), size)
        if not changed or before != expected.encode('utf-8'):
            raise ValueError('Test changed after BP12 or buffer size differs; undo refused')
        target.write_bytes(original)
        print('RESTORED_BASELINE:', target)
        return
    after, changed = edited(before.decode('utf-8'), size)
    print('TEST:', target)
    if not changed:
        print('ALREADY_APPLIED: BP12 experiment, buffer=%d' % size)
        return
    if dry_run:
        print('DRY_RUN_OK: experimental config only, buffer=%d' % size)
        return
    if backup.exists() and backup.read_bytes() != before:
        raise ValueError('Different BP12 backup exists; refusing overwrite')
    if not backup.exists():
        with backup.open('xb') as output:
            output.write(before)
    target.write_bytes(after.encode('utf-8'))
    print('BACKUP:', backup)
    print('APPLIED_EXPERIMENT: DS/US implicit_bp=1 tx_retry_buffer_size=%d' % size)
    print('NEXT: python3 fix12.py --build')


def summary(log):
    source = log.read_text(errors='replace')
    notable = re.compile(r'M1DE_BP12_CFG|UCIE_E2E_SUMMARY|SvtTestEpilog|UVM_ERROR|UVM_FATAL|UCIE_FDI_(?:TX|RX|CFG|RXACT|STRM|FMT)_|m1de_sva_(?:ds|us)\.c_|Error-|IRIPS|M1DE_BUILD_EXIT|M1DE_BUILD_TIMEOUT')
    lines = source.splitlines()
    indexes = set()
    for i, line in enumerate(lines):
        if notable.search(line):
            indexes.add(i)
            if line.rstrip().endswith('SvtTestEpilog:') and i + 1 < len(lines):
                indexes.add(i + 1)
    print('LOG:', log)
    for i in sorted(indexes)[-50:]:
        print(lines[i])
    hits = {}
    for role in ('ds', 'us'):
        records = re.findall(r'm1de_sva_' + role + r'\.c_tx_stall_then_accept\s*,\s*(\d+)\s+attempts\s*,\s*(\d+)\s+match', source)
        hits[role] = tuple(map(int, records[-1])) if records else None
    print('STALL_COVER: DS=%s US=%s' % (hits['ds'], hits['us']))
    smoke_ok = bool(re.search(r'SvtTestEpilog:\s*Passed', source)
                    and re.search(r'^\s*UVM_ERROR\s*:\s*0\s*$', source, re.M)
                    and re.search(r'^\s*UVM_FATAL\s*:\s*0\s*$', source, re.M)
                    and re.search(r'\[UCIE_E2E_SUMMARY\]\s+matched=[1-9]\d*\s+violations=0\b', source)
                    and 'M1DE_BUILD_EXIT rc=0' in source)
    configured = all(re.search(r'role=' + role + r' retry=1 implicit_bp=1 tx_retry_buffer_size=\d+ EXPERIMENT', source) for role in ('DS', 'US'))
    if smoke_ok and configured and all(v and v[0] > 0 and v[1] > 0 for v in hits.values()):
        print('RESULT: smoke checks PASS and DS/US stall covers hit; retain log for review')
    elif smoke_ok and configured:
        print('RESULT: smoke checks PASS; backpressure coverage NOT complete')
    else:
        print('RESULT: run incomplete, failed, or evidence missing; inspect log')


def preflight(project, topology, vendor_root):
    if fix10.edited(topology.read_text())[1]:
        raise ValueError('Required DS/US fix10 SVA connections missing')
    if not (topology.parent / fix10.FILELIST).is_file():
        raise ValueError('Expected compile filelist missing')
    adapters = list((project / 'adapter/synopsys').glob('*/adapter/ucie_opfmt_snps_adapter.sv'))
    if len(adapters) != 1 or 'raw_byte7=' not in adapters[0].read_text():
        raise ValueError('Expected fix11 byte-7 diagnostic baseline missing')
    vendor = clean((vendor_root / 'src/sverilog/vcs/svt_ucie_d2d_adapter_configuration.sv').read_text())
    if not re.search(r'\bbit\s*\[\s*7\s*:\s*0\s*\]\s+tx_retry_buffer_size\b', vendor):
        raise ValueError('Expected 8-bit tx_retry_buffer_size declaration missing')
    if not re.search(r'\bbit\s+enable_dda_implicit_backpressure\b', vendor):
        raise ValueError('Expected implicit backpressure declaration missing')
    # These declarations establish API availability, not valid capacity limits.
    baseline = project / 'logs/compile_pipe_debug11.log'
    if not baseline.is_file() or not re.search(r'SvtTestEpilog:\s*Passed', baseline.read_text(errors='replace')):
        raise ValueError('Expected passing debug11 baseline log missing')


def stop_process(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def build(project, topology, target, size, log, timeout):
    if edited(target.read_text(), size)[1]:
        raise ValueError('Apply BP12 before --build')
    if not shutil.which('gmake'):
        raise ValueError('gmake unavailable')
    command = ['gmake', '-B', fix10.TEST, 'USE_SIMULATOR=vcsvlog',
               'SVT_UCIE_COMPILE_FILE=' + fix10.FILELIST,
               'SVT_UCIE_TOPOLOGY_FILE=' + fix10.NAME]
    print('BUILD_CWD:', topology.parent, flush=True)
    print('LOG:', log, flush=True)
    with log.open('xb') as output:
        proc = subprocess.Popen(command, cwd=str(topology.parent), stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        print('BUILD_STARTED: pid=%d; wait for BUILD_EXIT' % proc.pid, flush=True)
        started = time.monotonic()
        try:
            while True:
                try:
                    code = proc.wait(timeout=min(30, max(1, timeout - (time.monotonic() - started))))
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    print('BUILD_RUNNING: elapsed=%ds' % elapsed, flush=True)
                    if elapsed >= timeout:
                        stop_process(proc)
                        output.write(b'\nM1DE_BUILD_TIMEOUT\n')
                        code = 124
                        break
        except KeyboardInterrupt:
            stop_process(proc)
            output.write(b'\nM1DE_BUILD_EXIT rc=130 INTERRUPTED\n')
            raise
        output.write(('\nM1DE_BUILD_EXIT rc=%d\n' % code).encode())
    print('BUILD_EXIT: rc=%d; coverage/result checks follow' % code)
    summary(log)
    return code if code >= 0 else 128 - code


def self_test():
    fixture = ('function void ' + fix10.TEST + '::create_cfg();\n'
               '  bit enable_retry = 1;\n'
               '  env_cfg.ds_d2d_adapter_cfg.retry = enable_retry;\n'
               '  env_cfg.us_d2d_adapter_cfg.retry = enable_retry;\n'
               '  if (env_cfg.include_ds_d2d_adapter_passive) begin end\n'
               '  if (env_cfg.include_us_d2d_adapter_passive) begin end\n'
               '  `uvm_info("create_cfg", "Exiting...", UVM_MEDIUM)\n'
               'endfunction\n`endif\n')
    expected, changed = edited(fixture)
    assert changed and edited(expected) == (expected, False)
    for bad in (fixture.replace('enable_retry = 1', 'enable_retry = 0'),
                fixture + fixture, expected.replace("8'd2", "8'd3", 1),
                fixture.replace('endfunction', '  do_more();\nendfunction'),
                fixture.replace('  bit enable_retry', '`ifdef X\n  bit enable_retry')):
        try:
            edited(bad)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe fixture accepted')
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        target = project / TEST_REL
        target.parent.mkdir(parents=True)
        target.write_text(fixture)
        patch(target, 2, dry_run=True)
        assert target.read_text() == fixture
        patch(target, 2)
        patch(target, 2)
        target.write_text(expected + '// later edit\n')
        try:
            patch(target, 2, undo=True)
        except ValueError:
            pass
        else:
            raise AssertionError('Undo overwrote later edits')
        target.write_text(expected)
        topology = target.parent.parent / fix10.NAME
        topology.write_text(fix10.PAYLOAD)
        log = project / 'debug12.log'
        bin_dir = project / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'gmake'
        fake.write_text('#!' + sys.executable + '\nimport os,sys\nprint(os.getcwd())\nprint(repr(sys.argv[1:]))\nsys.exit(3)\n')
        fake.chmod(0o755)
        old_path = os.environ.get('PATH', '')
        try:
            os.environ['PATH'] = str(bin_dir) + os.pathsep + old_path
            assert build(project, topology, target, 2, log, 60) == 3
            saved = log.read_bytes()
            assert str(topology.parent).encode() in saved
            for arg in (fix10.TEST, fix10.FILELIST, fix10.NAME, 'USE_SIMULATOR=vcsvlog'):
                assert arg.encode() in saved
            assert b'M1DE_BUILD_EXIT rc=3' in saved
            try:
                build(project, topology, target, 2, log, 60)
            except FileExistsError:
                pass
            else:
                raise AssertionError('Build overwrote old log')
            assert log.read_bytes() == saved
        finally:
            os.environ['PATH'] = old_path
        patch(target, 2, undo=True)
        assert target.read_text() == fixture
        evidence = ('M1DE_BP12_CFG role=DS retry=1 implicit_bp=1 tx_retry_buffer_size=2 EXPERIMENT\n'
                    'M1DE_BP12_CFG role=US retry=1 implicit_bp=1 tx_retry_buffer_size=2 EXPERIMENT\n'
                    '[UCIE_E2E_SUMMARY] matched=20 violations=0 epochs=0\n'
                    'SvtTestEpilog: Passed\nUVM_ERROR : 0\nUVM_FATAL : 0\n'
                    'test_top.m1de_sva_ds.c_tx_stall_then_accept, 717 attempts, 3 match\n'
                    'test_top.m1de_sva_us.c_tx_stall_then_accept, 717 attempts, 0 match\n'
                    'M1DE_BUILD_EXIT rc=0\n')
        for candidate, expected_message in (
                (evidence, 'backpressure coverage NOT complete'),
                (evidence.replace('717 attempts, 0 match', '717 attempts, 2 match'), 'DS/US stall covers hit'),
                (evidence.replace('UVM_ERROR : 0', 'UVM_ERROR : 1'), 'evidence missing')):
            log.write_text(candidate)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                summary(log)
            assert expected_message in captured.getvalue()
    print('SELF_TEST_PASS: guarded insertion, idempotence, backup/undo and fake build cwd/args/exit/log protection')
    print('NOTE: no VCS run; buffer=2 remains exploratory.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    for name in ('build', 'summary', 'dry-run', 'undo', 'self-test'):
        operation.add_argument('--' + name, action='store_true')
    parser.add_argument('--buffer-size', type=int, default=2)
    parser.add_argument('--vendor-root', type=Path, default=Path(os.environ.get('DIR_VIP', VENDOR_DEFAULT)))
    parser.add_argument('--log-name', default='compile_pipe_debug12.log')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not 1 <= args.buffer_size <= 255:
        raise ValueError('Buffer size must fit the nonzero 8-bit field; vendor limits remain to be tested')
    if not re.fullmatch(r'compile_pipe_debug12(?:[a-z0-9_-]*)\.log', args.log_name):
        raise ValueError('Use a log name such as compile_pipe_debug12b.log')
    if args.timeout < 30:
        raise ValueError('Timeout must be at least 30 seconds')
    topology = fix10.locate(None)
    project = fix10.project_for(topology)
    target = project / TEST_REL
    log = project / 'logs' / args.log_name
    if args.summary:
        summary(log)
        return 0
    if not args.undo:
        preflight(project, topology, args.vendor_root)
    if args.build:
        return build(project, topology, target, args.buffer_size, log, args.timeout)
    patch(target, args.buffer_size, args.dry_run, args.undo)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nINTERRUPTED: build process group stopped; inspect debug12 log', file=sys.stderr)
        sys.exit(130)
