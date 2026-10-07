#!/usr/bin/env python3
"""Bounded vendor-API experiment for Stack0 FDI backpressure (DS + US).

Keep beside fix10.py. Python standard library only, Python 3.6+.
  python3 fix13.py             backup + patch the copied example test
  python3 fix13.py --build     compile/run, exclusive debug13 log, summarize
  python3 fix13.py --summary   summarize an existing log without loading it all
  python3 fix13.py --undo      exact restore; refuses later source edits
  python3 fix13.py --self-test synthetic patch/log/build tests; NOT VCS

Uses svt_ucie_fdi_virtual_api_collection_sequence.drive_pl_signal(PL_TRDY)
on the D2D Adapter FDI virtual sequencers [0][0]. API availability and routing
were read from the installed source; runtime behavior remains experimental.
No force/deposit, retry-buffer tuning, checker suppression, core/topology edit.
The baseline traffic branch remains intact inside an additional fork.
Only positive raw stall/recovery evidence AND a clean run can pass the gate.
"""
import argparse
from collections import deque
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
    sys.exit('STOP: put fix13.py beside fix10.py')

TAG = 'M1DE_BP13_API_V1'
TEST_REL = fix10.TB_REL / 'tests' / ('ts.' + fix10.TEST + '.sv')
VENDOR_DEFAULT = '/u/svc-uciephyverif/p4_ws/designware_home/ucie_vip'


def clean(source):
    return re.sub(r'/\*[\s\S]*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), source)


def vif(role):
    return 'env_cfg.%s_d2d_adapter_cfg.fdi_cfg[0][0].vif' % role


def seqr(role):
    return 'ucie_env.env_virt_seqr.%s_d2d_adapter_virt_seqr.d2d_fdi_virt_seqr[0][0]' % role


DECL = '''
  // BEGIN M1DE_BP13_API_V1_DECL
  svt_ucie_fdi_virtual_api_collection_sequence m1de_bp13_ds_api;
  svt_ucie_fdi_virtual_api_collection_sequence m1de_bp13_us_api;
  bit m1de_bp13_prepare_done = 0;
  bit m1de_bp13_ds_done = 0;
  bit m1de_bp13_us_done = 0;
  bit m1de_bp13_ds_stall = 0;
  bit m1de_bp13_us_stall = 0;
  // END M1DE_BP13_API_V1_DECL
'''


def prepare():
    lines = ['  // BEGIN ' + TAG + '_PREP',
             '  // API experiment: prepare after ACTIVE, before original traffic.']
    for role in ('ds', 'us'):
        cfg = 'env_cfg.%s_d2d_adapter_cfg' % role
        s = seqr(role)
        v = vif(role)
        lines += ["  if (env_cfg.include_%s_d2d_adapter !== 1'b1)" % role,
                  '    `uvm_fatal("M1DE_BP13", "%s active adapter is required")' % role.upper(),
                  '  if (%s == null)' % cfg,
                  '    `uvm_fatal("M1DE_BP13", "%s adapter config missing")' % role.upper(),
                  '  if (%s.fdi_cfg[0][0] == null)' % cfg,
                  '    `uvm_fatal("M1DE_BP13", "%s Stack0 FDI config missing")' % role.upper(),
                  '  if (%s == null)' % v,
                  '    `uvm_fatal("M1DE_BP13", "%s Stack0 FDI vif missing")' % role.upper(),
                  '  if (ucie_env.env_virt_seqr.%s_d2d_adapter_virt_seqr == null)' % role,
                  '    `uvm_fatal("M1DE_BP13", "%s adapter virtual sequencer missing")' % role.upper(),
                  '  if (%s == null)' % s,
                  '    `uvm_fatal("M1DE_BP13", "%s Stack0 FDI sequencer missing")' % role.upper(),
                  "  if (%s.reset !== 1'b0 || %s.pl_trdy !== 1'b1)" % (v, v),
                  '    `uvm_fatal("M1DE_BP13", "%s baseline must be out of reset and ready")' % role.upper()]
    lines += ['  fork : m1de_bp13_prepare_guard', '    begin', '      fork']
    for role in ('ds', 'us'):
        a = 'm1de_bp13_%s_api' % role
        lines += ['        begin',
                  '          %s = svt_ucie_fdi_virtual_api_collection_sequence::type_id::create("%s");' % (a, a),
                  '          %s.set_sequencer(%s);' % (a, seqr(role)),
                  '          %s.start(%s);' % (a, seqr(role)),
                  "          %s.drive_pl_signal(svt_ucie_types::PL_TRDY, '0, null);" % a,
                  '        end']
    lines += ['      join', '      fork']
    for role in ('ds', 'us'):
        v = vif(role)
        lines += ['        begin',
                  '          for (int n = 0; n < 16; n++) begin',
                  '            @(posedge %s.lclk);' % v,
                  "            if (%s.pl_trdy === 1'b0) break;" % v,
                  '          end',
                  "          if (%s.pl_trdy !== 1'b0) begin" % v,
                  "            m1de_bp13_%s_api.drive_pl_signal(svt_ucie_types::PL_TRDY, 'd1, null);" % role,
                  '            `uvm_fatal("M1DE_BP13", "%s API did not produce ready=0; restore requested")' % role.upper(),
                  '          end',
                  '          `uvm_info("M1DE_BP13_PREP", "role=%s ready=0 observed", UVM_NONE)' % role.upper(),
                  '        end']
    lines += ['      join', '      m1de_bp13_prepare_done = 1;', '    end', '    begin',
              '      repeat (128) @(posedge %s.lclk);' % vif('ds'),
              '      if (!m1de_bp13_prepare_done)',
              '        `uvm_fatal("M1DE_BP13", "API preparation timeout; experiment aborted")',
              '    end', '  join_any', '  disable m1de_bp13_prepare_guard;',
              '  // END ' + TAG + '_PREP', '']
    return '\n'.join(lines)


OPEN = '  // BEGIN ' + TAG + '_FORK\n  fork\n    begin : m1de_bp13_original_traffic\n'


def close():
    lines = ['    end']
    for role in ('ds', 'us'):
        v = vif(role)
        a = 'm1de_bp13_%s_api' % role
        flag = 'm1de_bp13_%s_stall' % role
        done = 'm1de_bp13_%s_done' % role
        lines += ['    begin : m1de_bp13_%s_control' % role,
                  '      fork : m1de_bp13_%s_guard' % role,
                  '        begin',
                  '          for (int n = 0; n < 128; n++) begin',
                  '            @(posedge %s.lclk);' % v,
                  "            if (%s.reset === 1'b0 && %s.lp_valid === 1'b1 && %s.lp_irdy === 1'b1 && %s.pl_trdy === 1'b0) begin" % (v, v, v, v),
                  '              %s = 1;' % flag,
                  '              `uvm_info("M1DE_BP13_STALL", "role=%s raw TX stall observed", UVM_NONE)' % role.upper(),
                  '              break;', '            end', '          end',
                  '          // Restore on the falling edge after a sampled stall, or on the no-stall limit.',
                  '          @(negedge %s.lclk);' % v,
                  "          %s.drive_pl_signal(svt_ucie_types::PL_TRDY, 'd1, null);" % a,
                  '          for (int n = 0; n < 16; n++) begin',
                  '            @(posedge %s.lclk);' % v,
                  "            if (%s.pl_trdy === 1'b1) break;" % v,
                  '          end',
                  "          if (%s.pl_trdy !== 1'b1)" % v,
                  '            `uvm_fatal("M1DE_BP13", "%s ready did not recover")' % role.upper(),
                  '          `uvm_info("M1DE_BP13_RELEASE", "role=%s ready=1 observed", UVM_NONE)' % role.upper(),
                  '          if (!%s)' % flag,
                  '            `uvm_error("M1DE_BP13", "%s no raw TX stall observed in 128 clocks")' % role.upper(),
                  '          %s = 1;' % done,
                  '        end', '        begin',
                  '          repeat (256) @(posedge %s.lclk);' % v,
                  '          if (!%s)' % done,
                  '            `uvm_fatal("M1DE_BP13", "%s stall/release API timeout; experiment aborted")' % role.upper(),
                  '        end', '      join_any',
                  '      disable m1de_bp13_%s_guard;' % role,
                  '    end']
    lines += ['  join', '  // END ' + TAG + '_FORK', '']
    return '\n'.join(lines)


PREP = prepare()
CLOSE = close()


def edited(source):
    if TAG in source:
        if all(source.count(part) == 1 for part in (DECL, PREP, OPEN, CLOSE)):
            return source, False
        raise ValueError('Existing BP13 block differs; edit/undo refused')
    scrub = clean(source)
    if re.search(r'\b(?:tx_retry_buffer_size|enable_dda_implicit_backpressure)\b', scrub):
        raise ValueError('Experimental retry/buffer assignments present; restore baseline first')
    starts = list(re.finditer(r'(?m)^[ \t]*task\s+ucie_test_main_phase\s*\(\s*\)\s*;', scrub))
    if len(starts) != 1:
        raise ValueError('Expected one inline ucie_test_main_phase() task')
    start = starts[0].end()
    match = re.search(r'\bendtask\b', scrub[start:])
    if not match:
        raise ValueError('Main task end missing')
    stop = start + match.start()
    body = source[start:stop]
    macro = 'SVT_UCIE_VERIFY_MULTI_STACK_WITH_ONE_FDI_DISABLED'
    branches = list(re.finditer(r'(?m)^[ \t]*`ifdef\s+' + macro + r'\b', body))
    if len(branches) != 2:
        raise ValueError('Expected allocation and traffic preprocessor branches')
    branch = start + branches[-1].start()
    before = clean(source[start:branch])
    if (not re.search(r'\bjoin\s*$', before) or
            'fdi_cfg' not in before or before.count('SVT_UCIE_IF_WAIT_STATUS') < 4):
        raise ValueError('Expected ACTIVE waits immediately before traffic branch')
    traffic = source[branch:stop]
    if not re.search(r'`endif\s*$', traffic):
        raise ValueError('Expected final traffic endif immediately before endtask')
    if ('mainband_seq[0].start(ucie_env.env_virt_seqr)' not in traffic or
            'mainband_seq[1].start(ucie_env.env_virt_seqr)' not in traffic or
            'external_fdi_mainband_seq.start(ucie_env.env_virt_seqr)' not in traffic):
        raise ValueError('Expected baseline traffic start calls missing')
    return (source[:start] + DECL + source[start:branch] + PREP + OPEN +
            traffic + CLOSE + source[stop:]), True


def patch(target, dry_run=False, undo=False):
    current = target.read_bytes()
    backup = target.with_name(target.name + '.fix13_backup')
    if undo:
        original = backup.read_bytes()
        expected, changed = edited(original.decode('utf-8'))
        if not changed or current != expected.encode('utf-8'):
            raise ValueError('Test changed after BP13; exact undo refused')
        target.write_bytes(original)
        print('RESTORED_BASELINE:', target)
        return
    after, changed = edited(current.decode('utf-8'))
    print('TEST:', target)
    if not changed:
        print('ALREADY_APPLIED: BP13 vendor-API experiment')
        return
    if dry_run:
        print('DRY_RUN_OK: original traffic preserved; bounded DS/US API control added')
        return
    if backup.exists() and backup.read_bytes() != current:
        raise ValueError('Different fix13 backup exists; refusing overwrite')
    if not backup.exists():
        with backup.open('xb') as out:
            out.write(current)
    target.write_bytes(after.encode('utf-8'))
    print('BACKUP:', backup)
    print('APPLIED_EXPERIMENT: PL_TRDY 0 -> sampled stall -> PL_TRDY 1, Stack0 DS/US')
    print('NEXT: python3 fix13.py --build')


def analyze(log):
    result = {'passed': False, 'errors': False, 'uvm_error': None, 'uvm_fatal': None,
              'rc': None, 'e2e': None, 'shadow': None, 'covers': {}, 'api': set(), 'seeds': set(),
              'tail': deque(maxlen=35)}
    pending_epilog = False
    notable = re.compile(r'M1DE_BP13|UCIE_E2E_SUMMARY|UCIE_INT_SHADOW_SUMMARY|SvtTestEpilog|UVM_(?:ERROR|FATAL)|Error-|M1DE_BUILD_|m1de_sva_(?:ds|us)\.c_')
    with log.open(errors='replace') as source:
        for line in source:
            for seed in re.findall(r'(?:^|\s)\+ntb_random_seed=(\d+)(?=\s|$)', line):
                result['seeds'].add(int(seed))
            if notable.search(line) or pending_epilog:
                result['tail'].append(line.rstrip())
            if 'SvtTestEpilog:' in line:
                result['passed'] = bool(re.search(r'SvtTestEpilog:\s*Passed', line))
                pending_epilog = line.rstrip().endswith('SvtTestEpilog:')
            elif pending_epilog and line.strip():
                result['passed'] = line.strip() == 'Passed'
                pending_epilog = False
            # SVT prints demoted/caught report counters before the UVM totals.
            # Only exact, zero-valued counter lines are harmless; actual reports
            # and nonzero counters must still fail even if final totals are zero.
            uvm_counter = re.fullmatch(
                r'\s*(?:UVM_(?:ERROR|FATAL)|Number of (?:demoted|caught) '
                r'UVM_(?:ERROR|FATAL) reports)\s*:\s*(\d+)\s*', line)
            if (uvm_counter and int(uvm_counter[1]) != 0) or (
                    not uvm_counter and re.search(r'\bUVM_(?:ERROR|FATAL)\b', line)) or re.search(
                    r'Error-|M1DE_BUILD_TIMEOUT|M1DE_BUILD_LOG_LIMIT', line):
                result['errors'] = True
            for key in ('uvm_error', 'uvm_fatal'):
                count = re.match(r'\s*' + key.upper() + r'\s*:\s*(\d+)\s*$', line)
                if count:
                    result[key] = int(count[1])
            match = re.search(r'M1DE_BUILD_EXIT rc=(-?\d+)', line)
            if match:
                result['rc'] = int(match[1])
            match = re.search(r'\[UCIE_E2E_SUMMARY\]\s+matched=(\d+)\s+violations=(\d+)\s+epochs=(\d+)', line)
            if match:
                result['e2e'] = tuple(map(int, match.groups()))
            match = re.search(r'\[UCIE_INT_SHADOW_SUMMARY\]\s+accepted=(\d+)\s+rejected=(\d+)\s+stack=(\d+)\s+payload_bytes=(\d+)', line)
            if match:
                result['shadow'] = tuple(map(int, match.groups()))
            match = re.search(r'm1de_sva_(ds|us)\.(c_tx_accept|c_rx_transfer|c_tx_stall_then_accept)\s*,\s*(\d+)\s+attempts\s*,\s*(\d+)\s+match', line)
            if match:
                result['covers'][(match[1], match[2])] = (int(match[3]), int(match[4]))
            for role in ('DS', 'US'):
                for event, text in (('PREP', 'ready=0 observed'),
                                    ('STALL', 'raw TX stall observed'),
                                    ('RELEASE', 'ready=1 observed')):
                    if '[M1DE_BP13_' + event + ']' in line and 'role=' + role + ' ' + text in line:
                        result['api'].add((role, event))
    return result


def smoke_ok(r):
    return (r['passed'] and not r['errors'] and r['uvm_error'] == r['uvm_fatal'] == 0
            and r['rc'] == 0 and r['e2e'] == (20, 0, 0)
            and r['shadow'] == (40, 0, 0, 64)
            and all(r['covers'].get((role, cover), (0, 0))[1] == 10
                    for role in ('ds', 'us') for cover in ('c_tx_accept', 'c_rx_transfer')))


def bp_ok(r):
    return (smoke_ok(r)
            and all((role, event) in r['api'] for role in ('DS', 'US')
                    for event in ('PREP', 'STALL', 'RELEASE'))
            and all(r['covers'].get((role, 'c_tx_stall_then_accept'), (0, 0))[1] > 0
                    for role in ('ds', 'us')))


def seed_ok(r, expected_seed):
    return expected_seed is None or r['seeds'] == {expected_seed}


def summary(log, expected_seed=None):
    r = analyze(log)
    print('LOG:', log)
    for line in r['tail']:
        print(line[:1600])
    print('SMOKE_GATE:', 'PASS' if smoke_ok(r) else 'INCOMPLETE_OR_FAIL')
    for role in ('ds', 'us'):
        print('STALL_COVER_%s:' % role.upper(), r['covers'].get((role, 'c_tx_stall_then_accept')))
    if expected_seed is not None:
        print('SEED_GATE: requested=%d observed=%s %s' %
              (expected_seed, sorted(r['seeds']), 'PASS' if seed_ok(r, expected_seed) else 'FAIL'))
    qualified = bp_ok(r) and seed_ok(r, expected_seed)
    print('BACKPRESSURE_GATE:', 'PASS_THIS_RUN' if qualified else 'NOT_QUALIFIED')
    print('NOTE: this gate covers this Streaming/F2 Stack0 FDI64 run only.')
    return qualified


def preflight(project, topology, vendor_root):
    if fix10.edited(topology.read_text())[1]:
        raise ValueError('Required fix10 DS/US SVA connections missing')
    if not (topology.parent / fix10.FILELIST).is_file():
        raise ValueError('Expected filelist missing')
    adapters = list((project / 'adapter/synopsys').glob('*/adapter/ucie_opfmt_snps_adapter.sv'))
    if len(adapters) != 1 or 'raw_byte7=' not in adapters[0].read_text():
        raise ValueError('Expected fix11 adapter diagnostic baseline missing')
    baseline = project / 'logs/compile_pipe_debug11.log'
    if not baseline.is_file() or not smoke_ok(analyze(baseline)):
        raise ValueError('Expected complete passing debug11 baseline evidence missing')
    root = vendor_root / 'src/sverilog/vcs'
    types = clean((root / 'svt_ucie_types.sv').read_text())
    api = clean((root / 'svt_ucie_fdi_virtual_api_collection_sequence.sv').read_text())
    if not re.search(r'\bPL_TRDY\b', types) or not re.search(r'\bDRIVE_PL_SIGNAL\b', types):
        raise ValueError('Expected vendor PL_TRDY / DRIVE_PL_SIGNAL enums missing')
    for pattern in (r'::\s*drive_pl_signal\s*\(', r'p_sequencer\s*\.\s*fdi_service_seqr',
                    r'd2d_if_signal_val\s*==\s*local\s*::\s*pl_signal_val'):
        if not re.search(pattern, api):
            raise ValueError('Installed FDI API contract differs: ' + pattern)
    base = clean((project / fix10.TB_REL / 'env/ucie_system_base_sequence.sv').read_text())
    for role in ('ds', 'us'):
        pattern = role + r'_d2d_adapter_virt_seqr\s*\.\s*d2d_fdi_virt_seqr\s*\[\s*0\s*\]\s*\[\s*0\s*\]'
        if not re.search(pattern, base):
            raise ValueError('Expected D2D FDI API routing missing for ' + role)


def stop_process(proc):
    # Only terminate the process group spawned by this script.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def build(project, topology, target, log, timeout=600, max_log_mb=128, poll=30, seed=None):
    if edited(target.read_text())[1]:
        raise ValueError('Apply fix13 before --build')
    if not shutil.which('gmake'):
        raise ValueError('gmake unavailable in this shell')
    command = ['gmake', '-B', fix10.TEST, 'USE_SIMULATOR=vcsvlog',
               'SVT_UCIE_COMPILE_FILE=' + fix10.FILELIST,
               'SVT_UCIE_TOPOLOGY_FILE=' + fix10.NAME]
    if seed is not None:
        command.append('SEED=' + str(seed))
        print('REQUESTED_SEED:', seed, flush=True)
    print('BUILD_CWD:', topology.parent, flush=True)
    print('LOG:', log, flush=True)
    with log.open('xb') as out:
        proc = subprocess.Popen(command, cwd=str(topology.parent), stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        started = time.monotonic()
        print('BUILD_STARTED: pid=%d; wait for BUILD_EXIT' % proc.pid, flush=True)
        try:
            while True:
                try:
                    code = proc.wait(timeout=min(poll, max(0.01, timeout - (time.monotonic() - started))))
                    break
                except subprocess.TimeoutExpired:
                    elapsed = time.monotonic() - started
                    if elapsed >= timeout or log.stat().st_size > max_log_mb * 1024 * 1024:
                        reason = 'TIMEOUT' if elapsed >= timeout else 'LOG_LIMIT'
                        stop_process(proc)
                        code = 124 if reason == 'TIMEOUT' else 125
                        out.write(('\nM1DE_BUILD_%s\n' % reason).encode())
                        break
                    print('BUILD_RUNNING: elapsed=%ds log=%dMiB' %
                          (elapsed, log.stat().st_size // (1024 * 1024)), flush=True)
        except BaseException:
            stop_process(proc)
            out.write(b'\nM1DE_BUILD_INTERRUPTED\n')
            raise
        out.write(('\nM1DE_BUILD_EXIT rc=%d\n' % code).encode())
    print('BUILD_EXIT: rc=%d; simulation/SVA gate follows' % code, flush=True)
    passed = summary(log, seed)
    return (0 if passed else 2) if code == 0 else (code if code > 0 else 128 - code)


def fixture():
    return '''class example;
task ucie_test_main_phase();
 string method_name = "ucie_test_main_phase";
 super.ucie_test_main_phase();
 `ifdef SVT_UCIE_VERIFY_MULTI_STACK_WITH_ONE_FDI_DISABLED
 mainband_seq = new[1];
 `else
 mainband_seq = new[2];
 `endif
 `SVT_UCIE_IF_WAIT_STATUS(pl_state_sts, svt_ucie_types::ACTIVE, env_cfg.ds_d2d_adapter_cfg.rdi_cfg.vif);
 `SVT_UCIE_IF_WAIT_STATUS(pl_state_sts, svt_ucie_types::ACTIVE, env_cfg.us_d2d_adapter_cfg.rdi_cfg.vif);
 fork
 `SVT_UCIE_IF_WAIT_STATUS(pl_state_sts, svt_ucie_types::ACTIVE, env_cfg.ds_d2d_adapter_cfg.fdi_cfg[0][0].vif);
 `SVT_UCIE_IF_WAIT_STATUS(pl_state_sts, svt_ucie_types::ACTIVE, env_cfg.us_d2d_adapter_cfg.fdi_cfg[0][0].vif);
 join
 `ifdef SVT_UCIE_VERIFY_MULTI_STACK_WITH_ONE_FDI_DISABLED
 fork
 mainband_seq[0].start(ucie_env.env_virt_seqr);
 external_fdi_mainband_seq.start(ucie_env.env_virt_seqr);
 join
 `else
 fork
 mainband_seq[0].start(ucie_env.env_virt_seqr);
 mainband_seq[1].start(ucie_env.env_virt_seqr);
 join
 `endif
endtask:ucie_test_main_phase
endclass
'''


def log_fixture(bp=True):
    lines = ['SvtTestEpilog:\nPassed\nUVM_ERROR : 0\nUVM_FATAL : 0\n',
             '[UCIE_E2E_SUMMARY] matched=20 violations=0 epochs=0\n',
             '[UCIE_INT_SHADOW_SUMMARY] accepted=40 rejected=0 stack=0 payload_bytes=64\n',
             'M1DE_BUILD_EXIT rc=0\n']
    for role in ('ds', 'us'):
        for cover in ('c_tx_accept', 'c_rx_transfer', 'c_tx_stall_then_accept'):
            hit = (1 if bp else 0) if cover == 'c_tx_stall_then_accept' else 10
            lines.append('test.m1de_sva_%s.%s, 717 attempts, %d match\n' % (role, cover, hit))
        if bp:
            for event, text in (('PREP', 'ready=0 observed'), ('STALL', 'raw TX stall observed'),
                                ('RELEASE', 'ready=1 observed')):
                lines.append('[M1DE_BP13_%s] role=%s %s\n' % (event, role.upper(), text))
    return ''.join(lines)


def self_test():
    original = fixture()
    after, changed = edited(original)
    assert changed and edited(after) == (after, False)
    branch = original[original.rindex(' `ifdef'):original.index('endtask')]
    assert branch in after
    for bad in (original + original, original.replace('SVT_UCIE_IF_WAIT_STATUS', 'other'),
                original + 'x.tx_retry_buffer_size = 2;\n', after.replace('ready=0 observed', 'changed')):
        try:
            edited(bad)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe source accepted')
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / 'test.sv'
        target.write_text(original)
        patch(target, dry_run=True)
        assert target.read_text() == original
        patch(target)
        assert target.read_text() == after
        patch(target)
        target.write_text(after + '// later edit\n')
        try:
            patch(target, undo=True)
        except ValueError:
            pass
        else:
            raise AssertionError('Undo overwrote a later edit')
        target.write_text(after)
        patch(target, undo=True)
        assert target.read_text() == original
        patch(target)
        log = root / 'synthetic.log'
        log.write_text(log_fixture())
        assert smoke_ok(analyze(log)) and bp_ok(analyze(log))
        for seed_line, expected in (
                ('./output/simvcsvlog +ntb_random_seed=51 -l ./logs/simulate.log run\n', True),
                ('./output/simvcsvlog +ntb_random_seed=50 run\n', False),
                ('', False),
                ('./output/simvcsvlog +ntb_random_seed=51 +ntb_random_seed=50 run\n', False),
                ('M1DE_REQUESTED_SEED=51\n', False)):
            log.write_text(seed_line + log_fixture())
            assert seed_ok(analyze(log), 51) == expected
        log.write_text(log_fixture(False))
        assert smoke_ok(analyze(log)) and not bp_ok(analyze(log))
        counters = ''.join('Number of %s UVM_%s reports : 0\n' % (kind, severity)
                           for kind in ('demoted', 'caught')
                           for severity in ('FATAL', 'ERROR', 'WARNING'))
        log.write_text(counters + log_fixture(False))
        parsed = analyze(log)
        assert not parsed['errors'] and smoke_ok(parsed) and not bp_ok(parsed)
        log.write_text(counters + log_fixture())
        assert bp_ok(analyze(log))
        for bad_report in ('UVM_ERROR file.sv(578) @ 265000000: env [RETRY] overflow\n',
                           'UVM_FATAL @ 10: reporter [FAIL] failure\n',
                           'Number of caught UVM_ERROR reports : 1\n',
                           'Number of demoted UVM_FATAL reports : 1\n',
                           'Number of caught UVM_ERROR reports : unknown\n',
                           'UVM_ERROR : 1\n', 'Error-[SE] Syntax error\n',
                           'M1DE_BUILD_TIMEOUT\n'):
            # An earlier failure cannot be erased by later zero-valued totals.
            log.write_text(bad_report + counters + log_fixture())
            assert analyze(log)['errors'] and not smoke_ok(analyze(log))
        for text in (log_fixture() + 'UVM_ERROR bad\n',
                     log_fixture().replace('violations=0', 'violations=1'),
                     log_fixture().replace('accepted=40', 'accepted=39'),
                     log_fixture().replace('717 attempts, 1 match', '717 attempts, 0 match'),
                     log_fixture().replace('role=US ready=1 observed', 'missing'),
                     log_fixture().replace('rc=0', 'rc=124'),
                     log_fixture() + 'M1DE_BUILD_LOG_LIMIT\n'):
            log.write_text(text)
            assert not bp_ok(analyze(log))
        # Streaming reader must not call Path.read_text on simulation logs.
        log.write_text(('ordinary line\n' * 100000) + log_fixture())
        assert len(analyze(log)['tail']) <= 35 and bp_ok(analyze(log))
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'gmake'
        fake.write_text('#!' + sys.executable + '\nimport os, sys\n'
                        'print("CWD=" + os.getcwd())\nprint("ARGS=" + repr(sys.argv[1:]))\n'
                        'sys.exit(3)\n')
        fake.chmod(0o755)
        topology = root / fix10.NAME
        old_path = os.environ.get('PATH', '')
        try:
            os.environ['PATH'] = str(bin_dir) + os.pathsep + old_path
            build_log = root / 'build.log'
            assert build(root, topology, target, build_log, poll=0.05, seed=51) == 3
            saved = build_log.read_bytes()
            assert str(root).encode() in saved and b'M1DE_BUILD_EXIT rc=3' in saved
            assert b'SEED=51' in saved
            for value in (fix10.TEST, fix10.FILELIST, fix10.NAME):
                assert value.encode() in saved
            try:
                build(root, topology, target, build_log, poll=0.05)
            except FileExistsError:
                pass
            else:
                raise AssertionError('Existing log overwritten')
            fake.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(5)\n')
            assert build(root, topology, target, root / 'timeout.log', timeout=0.15, poll=0.05) == 124
        finally:
            os.environ['PATH'] = old_path
    print('SELF_TEST_PASS: patch guards, original traffic, idempotence, exact undo, log gates, streaming scan, build arguments, log protection, timeout cleanup')
    print('NOTE: synthetic source/fake gmake only; no VCS/SystemVerilog compile was run.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    for flag in ('build', 'summary', 'undo', 'dry-run', 'self-test'):
        mode.add_argument('--' + flag, action='store_true')
    parser.add_argument('--vendor-root', default=os.environ.get('DIR_VIP', VENDOR_DEFAULT))
    parser.add_argument('--log-name', default=None)
    parser.add_argument('--seed', type=int, help='Pass SEED to gmake and verify the simulation seed')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--max-log-mb', type=int, default=128)
    args = parser.parse_args()
    if args.self_test:
        with contextlib.redirect_stdout(io.StringIO()):
            self_test()
        print('SELF_TEST_PASS: patch/undo guards, bounded process cleanup, streaming result gates; no VCS validation')
        return 0
    if args.seed is not None and (not (args.build or args.summary) or not 1 <= args.seed <= 2147483647):
        raise ValueError('--seed requires --build/--summary and an integer from 1 to 2147483647')
    if args.log_name is None:
        args.log_name = ('compile_pipe_debug13_seed%d.log' % args.seed
                         if args.seed is not None else 'compile_pipe_debug13.log')
    if not re.fullmatch(r'compile_pipe_debug13(?:_[A-Za-z0-9-]+)?\.log', args.log_name):
        raise ValueError('Log name must be compile_pipe_debug13[_suffix].log')
    if args.timeout < 30 or args.max_log_mb < 1:
        raise ValueError('Timeout must be >=30 seconds; max log must be >=1 MiB')
    topology = fix10.locate(None)
    project = fix10.project_for(topology)
    target = project / TEST_REL
    log = project / 'logs' / args.log_name
    if args.summary:
        return 0 if summary(log, args.seed) else 2
    if args.undo:
        patch(target, undo=True)
        return 0
    preflight(project, topology, Path(args.vendor_root))
    if args.build:
        return build(project, topology, target, log, args.timeout, args.max_log_mb, seed=args.seed)
    patch(target, dry_run=args.dry_run)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('INTERRUPTED: own build group stopped; inspect log before rerun', file=sys.stderr)
        sys.exit(130)
