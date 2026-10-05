#!/usr/bin/env python3
"""Passive FDI runtime probe for the debug8 Synopsys topology.

Run from the testbench or incoming Git checkout: python3 probe9.py
Options: --dry-run, --undo, --summary LOG, --self-test.
Edits only the copied topology, after its US clock-selection branch.
No reset polarity assumption, no SVA enable changes, no traffic drives.
"""
import argparse
import os
from pathlib import Path
import re
import sys
import tempfile

NAME = 'topology_snps_ucie_both_streaming_multi_stack_d2d_dut_with_shim.svi'
REL = Path('build/m1de_snps_discovery_01/design_dir/examples/sverilog/ucie_svt/tb_ucie_svt_uvm_basic_sys') / NAME
BEGIN = '// BEGIN M1DE_PROBE9_V1'
END = '// END M1DE_PROBE9_V1'


def sv_probe(label, iface):
    p = 'm1de_probe9_' + label.lower()
    return '''
  // Passive observations; counters below count sampled beats, not packets.
  longint unsigned @P@_clk = 0;
  longint unsigned @P@_r0 = 0, @P@_r1 = 0, @P@_rx = 0;
  longint unsigned @P@_tx0 = 0, @P@_tx1 = 0, @P@_txx = 0;
  longint unsigned @P@_rx0 = 0, @P@_rx1 = 0, @P@_rxx = 0;
  bit @P@_seen = 0;
  initial begin
    $display("M1DE_FDI_PROBE WIDTH @LABEL@ lp_bits=%0d pl_bits=%0d", $bits(@IF@.lp_data), $bits(@IF@.pl_data));
  end
  always @(posedge @IF@.lclk) begin
    @P@_clk++;
    if (@IF@.reset === 1'b0) @P@_r0++;
    else if (@IF@.reset === 1'b1) @P@_r1++;
    else @P@_rx++;
    if ((@IF@.lp_irdy === 1'b1) && (@IF@.lp_valid === 1'b1) && (@IF@.pl_trdy === 1'b1)) begin
      if (@IF@.reset === 1'b0) @P@_tx0++;
      else if (@IF@.reset === 1'b1) @P@_tx1++;
      else @P@_txx++;
    end
    if (@IF@.pl_valid === 1'b1) begin
      if (@IF@.reset === 1'b0) @P@_rx0++;
      else if (@IF@.reset === 1'b1) @P@_rx1++;
      else @P@_rxx++;
    end
    if (!@P@_seen && (((@IF@.lp_irdy === 1'b1) && (@IF@.lp_valid === 1'b1) && (@IF@.pl_trdy === 1'b1)) || (@IF@.pl_valid === 1'b1))) begin
      @P@_seen = 1;
      $display("M1DE_FDI_PROBE FIRST @LABEL@ reset=%b state=%h protocol=%h fmt=%h protocol_valid=%b", @IF@.reset, @IF@.pl_state_sts, @IF@.pl_protocol, @IF@.pl_protocol_flitfmt, @IF@.pl_protocol_valid);
    end
  end
  final begin
    $display("M1DE_FDI_PROBE CLOCK @LABEL@ edges=%0d reset0=%0d reset1=%0d resetXZ=%0d", @P@_clk, @P@_r0, @P@_r1, @P@_rx);
    $display("M1DE_FDI_PROBE BEATS @LABEL@ tx_reset0=%0d tx_reset1=%0d tx_resetXZ=%0d rx_reset0=%0d rx_reset1=%0d rx_resetXZ=%0d", @P@_tx0, @P@_tx1, @P@_txx, @P@_rx0, @P@_rx1, @P@_rxx);
  end
'''.replace('@P@', p).replace('@LABEL@', label).replace('@IF@', iface)


PAYLOAD = '\n' + BEGIN + '\n' + sv_probe('DS', 'ds_fdi_if') + sv_probe('US', 'us_fdi_if') + END + '\n'


def scrub_comments(source):
    # Preserve line breaks and positions for insertion and exact restoration.
    def blank(m):
        return ''.join('\n' if c == '\n' else ' ' for c in m.group())
    return re.sub(r'/\*[\s\S]*?\*/|//[^\n]*', blank, source)


def insertion_offset(source):
    clean = scrub_comments(source)
    for iface in ('ds_fdi_if', 'us_fdi_if'):
        if not re.search(r'\bsvt_ucie_d2d_if\s+' + iface + r'\s*\(', clean):
            raise ValueError('Required interface declaration missing: ' + iface)
    starts = list(re.finditer(r'^\s*`ifdef\s+SVT_UCIE_LCLK_PHASE_SHIFT\b[^\n]*\n', clean, re.M))
    if len(starts) != 1:
        raise ValueError('Expected exactly one US clock-selection branch')
    start = starts[0]
    depth = 1
    for m in re.finditer(r'^\s*`(ifdef|ifndef|endif)\b[^\n]*(?:\n|$)', clean[start.end():], re.M):
        depth += -1 if m.group(1) == 'endif' else 1
        if depth == 0:
            stop = start.end() + m.end()
            if not re.search(r'\bsvt_ucie_d2d_if\s+us_fdi_if\s*\(', clean[start.start():stop]):
                raise ValueError('US declaration is outside the selected branch')
            return stop
    raise ValueError('Unbalanced preprocessor branch')


def edited(source):
    if BEGIN in source or END in source:
        if source.count(PAYLOAD) == 1 and source.count(BEGIN) == 1 and source.count(END) == 1:
            return source, False
        raise ValueError('Existing probe was changed or incomplete; refusing to overwrite')
    if 'm1de_probe9_' in scrub_comments(source):
        raise ValueError('Probe identifier conflict')
    offset = insertion_offset(source)
    return source[:offset] + PAYLOAD + source[offset:], True


def locate(explicit):
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file() or p.name != NAME:
            raise ValueError('Target must be the expected existing topology file')
        return p
    roots = [Path.cwd()] + list(Path.cwd().parents)
    if os.environ.get('PROJ'):
        roots.insert(0, Path(os.environ['PROJ']))
    found = set()
    for root in roots:
        if root == Path('/'):
            continue
        for p in (root / NAME, root / REL):
            if p.is_file():
                found.add(p.resolve())
    if len(found) != 1:
        raise ValueError('Found %d targets; run from the testbench or supply --target PATH' % len(found))
    return found.pop()


def self_test():
    source = '''// fixture only
svt_ucie_d2d_if ds_fdi_if(SystemClock,reset);
`ifdef SVT_UCIE_LCLK_PHASE_SHIFT
svt_ucie_d2d_if us_fdi_if(~SystemClock,remote_die_reset);
`elsif SVT_UCIE_LCLK_FREQ_SHIFT
svt_ucie_d2d_if us_fdi_if(SystemClock_fast,remote_die_reset);
`else
svt_ucie_d2d_if us_fdi_if(SystemClock,remote_die_reset);
`endif // clock
initial begin end
'''
    result, changed = edited(source)
    assert changed and result.replace(PAYLOAD, '', 1) == source
    assert result.index(BEGIN) > result.index('`endif // clock')
    assert result.index(END) < result.index('initial begin end')
    assert edited(result) == (result, False)
    for broken in (source.replace('ds_fdi_if(', 'other('), source.replace('`endif // clock', ''), result.replace('tx_reset0=', 'changed=')):
        try:
            edited(broken)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe fixture was accepted')
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / NAME
        path.write_text(source)
        assert locate(str(path)) == path.resolve()
    assert 'force ' not in PAYLOAD and 'assign ' not in PAYLOAD
    print('SELF_TEST_PASS: insertion, idempotence, restoration and rejection checks')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--summary', metavar='LOG')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()
    if sum(bool(x) for x in (args.dry_run, args.undo, args.summary, args.self_test)) > 1:
        ap.error('Choose only one operation')
    if args.self_test:
        self_test()
        return
    if args.summary:
        count = 0
        with open(args.summary, errors='replace') as log:
            for line in log:
                if line.startswith('M1DE_FDI_PROBE '):
                    print(line.rstrip())
                    count += 1
        if not count:
            raise ValueError('No runtime probe records found; verify compilation and run')
        return
    target = locate(args.target)
    original = target.read_bytes()
    source = original.decode('utf-8')
    backup = target.with_name(target.name + '.probe9_backup')
    if args.undo:
        if not backup.is_file():
            raise ValueError('Backup is missing')
        before = backup.read_bytes()
        expected, _ = edited(before.decode('utf-8'))
        if original != expected.encode('utf-8'):
            raise ValueError('Topology changed after patch; automatic undo refused')
        target.write_bytes(before)
        print('RESTORED:', target)
        return
    result, changed = edited(source)
    if not changed:
        print('ALREADY_APPLIED:', target)
        return
    print('TARGET:', target)
    if args.dry_run:
        print('DRY_RUN_OK: passive probes for ds_fdi_if and us_fdi_if; no files changed')
        return
    if backup.exists() and backup.read_bytes() != original:
        raise ValueError('A different backup already exists; refusing to overwrite')
    if not backup.exists():
        with backup.open('xb') as out:
            out.write(original)
    target.write_bytes(result.encode('utf-8'))
    print('BACKUP:', backup)
    print('APPLIED: passive runtime probe only. Rebuild using the debug8 command and a new log.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
