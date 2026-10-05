#!/usr/bin/env python3
"""Connect M1 SVA to the two observed Synopsys Stack0 FDI interfaces.

Run from the testbench or incoming/fix8: python3 fix10.py
Then: python3 fix10.py --build
Options: --target PATH, --dry-run, --undo, --self-test.
The copied topology alone is edited. Core and vendor sources are untouched.
Requires probe9. Undo fix10 before undoing probe9.
debug9 evidence: both interfaces have 512-bit data; traffic at reset=0;
first traffic has vendor protocol=7, flitfmt=2 and protocol_valid=1.
No unconditional metadata constants: unsupported vendor codes become X.
Raw stream/state/handshake signals remain observable by the core assertions.
Successful patching is not proof of successful compilation or qualification.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

NAME = 'topology_snps_ucie_both_streaming_multi_stack_d2d_dut_with_shim.svi'
TB_REL = Path('build/m1de_snps_discovery_01/design_dir/examples/sverilog/ucie_svt/tb_ucie_svt_uvm_basic_sys')
REL = TB_REL / NAME
CORE_REL = Path('src/ucie_operation_format_vip_m1d/src')
SVA_REL = CORE_REL / 'frontend/fdi/ucie_fdi_m1_sva.sv'
BEGIN = '// BEGIN M1DE_SVA_FIX10_V1'
END = '// END M1DE_SVA_FIX10_V1'
TEST = 'ucie_protocol_mainband_transfer_both_stack_streaming_multi_stack_test'
FILELIST = 'compile_snps_ucie_both_streaming_multi_stack_d2d_dut_with_shim.f'
PORTS = ('lclk tb_domain_reset_n checks_enable coverage_enable lp_data '
         'lp_valid lp_irdy lp_stream pl_trdy pl_data pl_valid pl_stream '
         'pl_protocol pl_protocol_flitfmt pl_protocol_vld pl_inband_pres '
         'pl_state_sts pl_rx_active_req lp_rx_active_sts pl_flit_cancel').split()


def sv_instance(label, iface):
    prefix = 'm1de_fix10_' + label.lower()
    return '''
  // Adapter encoding conversion for the observed Streaming + F2 smoke.
  // Four-state equality keeps unknown/unsupported vendor codes unknown.
  wire [3:0] @P@_protocol = (@IF@.pl_protocol == 3'd7)
    ? ucie_flit_format_types_pkg::UCIE_PROTOCOL_STREAMING_NO_MTP : 4'bxxxx;
  wire [3:0] @P@_fmt = (@IF@.pl_protocol_flitfmt == 4'd2)
    ? ucie_flit_format_types_pkg::UCIE_FORMAT_2_68B : 4'bxxxx;

  ucie_fdi_m1_sva #(.NBYTES(64)) m1de_sva_@LOW@ (
    .lclk(@IF@.lclk),
    .tb_domain_reset_n(~@IF@.reset),
    .checks_enable(@IF@.reset === 1'b0),
    .coverage_enable(1'b1),
    .lp_data(@IF@.lp_data),
    .lp_valid(@IF@.lp_valid),
    .lp_irdy(@IF@.lp_irdy),
    .lp_stream(@IF@.lp_stream),
    .pl_trdy(@IF@.pl_trdy),
    .pl_data(@IF@.pl_data),
    .pl_valid(@IF@.pl_valid),
    .pl_stream(@IF@.pl_stream),
    .pl_protocol(@P@_protocol),
    .pl_protocol_flitfmt(@P@_fmt),
    .pl_protocol_vld(@IF@.pl_protocol_valid),
    .pl_inband_pres(@IF@.pl_inband_pres),
    .pl_state_sts(@IF@.pl_state_sts),
    .pl_rx_active_req(@IF@.pl_rx_active_req),
    .lp_rx_active_sts(@IF@.lp_rx_active_sts),
    .pl_flit_cancel(@IF@.pl_flit_cancel)
  );

  initial begin
    if (($bits(@IF@.lp_data) != 512) || ($bits(@IF@.pl_data) != 512))
      $fatal(1, "M1DE_FDI_SVA @LABEL@ requires 512-bit TX and RX data");
    $display("M1DE_FDI_SVA CONNECT @LABEL@ %m nb=64 core_protocol=%h core_fmt=%h core_reset_state=%h core_stream=%h",
      ucie_flit_format_types_pkg::UCIE_PROTOCOL_STREAMING_NO_MTP,
      ucie_flit_format_types_pkg::UCIE_FORMAT_2_68B,
      ucie_flit_format_types_pkg::UCIE_STATE_STS_RESET,
      ucie_flit_format_types_pkg::UCIE_STREAM_STACK0_STREAMING);
  end
'''.replace('@P@', prefix).replace('@IF@', iface).replace('@LABEL@', label).replace('@LOW@', label.lower())


PAYLOAD = '\n' + BEGIN + '\n' + sv_instance('DS', 'ds_fdi_if') + sv_instance('US', 'us_fdi_if') + END + '\n'


def scrub(source):
    def blank(match):
        return ''.join('\n' if c == '\n' else ' ' for c in match.group())
    return re.sub(r'/\*[\s\S]*?\*/|//[^\n]*', blank, source)


def anchor(source):
    clean = scrub(source)
    for iface in ('ds_fdi_if', 'us_fdi_if'):
        if not re.search(r'\bsvt_ucie_d2d_if\s+' + iface + r'\s*\(', clean):
            raise ValueError('Missing interface: ' + iface)
    starts = list(re.finditer(r'^\s*`ifdef\s+SVT_UCIE_LCLK_PHASE_SHIFT\b[^\n]*\n', clean, re.M))
    if len(starts) != 1:
        raise ValueError('Expected exactly one US clock-selection branch')
    start, depth = starts[0], 1
    for match in re.finditer(r'^\s*`(ifdef|ifndef|endif)\b[^\n]*(?:\n|$)', clean[start.end():], re.M):
        depth += -1 if match.group(1) == 'endif' else 1
        if depth == 0:
            stop = start.end() + match.end()
            if not re.search(r'\bsvt_ucie_d2d_if\s+us_fdi_if\s*\(', clean[start.start():stop]):
                raise ValueError('US interface is outside clock-selection branch')
            return stop
    raise ValueError('Unbalanced US clock-selection branch')


def edited(source):
    if BEGIN in source or END in source:
        if source.count(PAYLOAD) == 1 and source.count(BEGIN) == source.count(END) == 1:
            return source, False
        raise ValueError('Existing fix10 block changed/incomplete; refusing to overwrite')
    if re.search(r'\bm1de_(?:fix10_|sva_(?:ds|us)\b)', scrub(source)):
        raise ValueError('SVA instance or adapter identifier conflict')
    if source.count('// BEGIN M1DE_PROBE9_V1') != 1 or source.count('// END M1DE_PROBE9_V1') != 1:
        raise ValueError('Expected probe9 markers are missing or duplicated')
    pos = anchor(source)
    return source[:pos] + PAYLOAD + source[pos:], True


def locate(explicit):
    if explicit:
        target = Path(explicit).expanduser().resolve()
        if not target.is_file() or target.name != NAME:
            raise ValueError('Target must be the expected existing topology')
        return target
    roots = [Path.cwd()] + list(Path.cwd().parents)
    if os.environ.get('PROJ'):
        roots.insert(0, Path(os.environ['PROJ']))
    found = set()
    for root in roots:
        if root == Path('/'):
            continue
        for candidate in (root / NAME, root / REL):
            if candidate.is_file():
                found.add(candidate.resolve())
    if len(found) != 1:
        raise ValueError('Found %d targets; run from testbench/incoming or use --target' % len(found))
    return found.pop()


def project_for(target):
    found = [p for p in target.parents if (p / SVA_REL).is_file()]
    if len(found) != 1:
        raise ValueError('Cannot uniquely locate project containing the core SVA')
    project = found[0]
    if target != (project / REL).resolve():
        raise ValueError('Target is outside the expected copied build topology')
    return project


def preflight(project):
    source = scrub((project / SVA_REL).read_text())
    headers = re.findall(r'\bmodule\s+ucie_fdi_m1_sva\b([\s\S]*?);', source)
    if len(headers) != 1:
        raise ValueError('Unexpected core SVA module declaration')
    header = headers[0]
    for port in PORTS:
        if not re.search(r'\binput\b[^,;]*\b' + port + r'\b', header):
            raise ValueError('Expected input port missing: ' + port)
    if not re.search(r'\bNBYTES\b', header):
        raise ValueError('Core SVA lacks NBYTES parameter')
    packages = []
    for path in (project / CORE_REL).rglob('*types_pkg.sv*'):
        if path.is_file() and re.search(r'\bpackage\s+ucie_flit_format_types_pkg\b', scrub(path.read_text())):
            packages.append(path)
    if len(packages) != 1:
        raise ValueError('Expected one canonical types package; found %d' % len(packages))
    types = scrub(packages[0].read_text())
    for name in ('UCIE_PROTOCOL_STREAMING_NO_MTP', 'UCIE_FORMAT_2_68B',
                 'UCIE_STATE_STS_RESET', 'UCIE_STREAM_STACK0_STREAMING'):
        if not re.search(r'\b' + name + r'\s*=', types):
            raise ValueError('Canonical symbol definition missing: ' + name)
    filelist = project / TB_REL / FILELIST
    if not filelist.is_file():
        raise ValueError('Expected compile filelist missing')
    print('CORE:', project / SVA_REL)
    print('TYPES:', packages[0])


def build(target, project):
    if edited(target.read_text())[1]:
        raise ValueError('Apply fix10 before --build')
    if not shutil.which('gmake'):
        raise ValueError('gmake is unavailable in this shell')
    logs = project / 'logs'
    if not logs.is_dir():
        raise ValueError('Expected logs directory missing')
    log = logs / 'compile_pipe_debug10.log'
    command = ['gmake', '-B', TEST, 'USE_SIMULATOR=vcsvlog',
               'SVT_UCIE_COMPILE_FILE=' + FILELIST,
               'SVT_UCIE_TOPOLOGY_FILE=' + NAME]
    # Exclusive creation protects an existing run. No shell interpolation.
    with log.open('xb') as output:
        proc = subprocess.Popen(command, cwd=str(target.parent), stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
    print('BUILD_STARTED: pid=%d' % proc.pid)
    print('LOG:', log)
    print('Use tail on this log; BUILD_STARTED does not mean PASS.')


def self_test():
    fixture = '''svt_ucie_d2d_if ds_fdi_if(SystemClock,reset);
`ifdef SVT_UCIE_LCLK_PHASE_SHIFT
svt_ucie_d2d_if us_fdi_if(~SystemClock,remote_die_reset);
`elsif SVT_UCIE_LCLK_FREQ_SHIFT
svt_ucie_d2d_if us_fdi_if(SystemClock_fast,remote_die_reset);
`else
svt_ucie_d2d_if us_fdi_if(SystemClock,remote_die_reset);
`endif // clock
// BEGIN M1DE_PROBE9_V1
initial begin end
// END M1DE_PROBE9_V1
'''
    patched, changed = edited(fixture)
    assert changed and patched.replace(PAYLOAD, '', 1) == fixture
    assert edited(patched) == (patched, False)
    assert patched.index(BEGIN) > patched.index('`endif // clock')
    for broken in (fixture.replace('ds_fdi_if(', 'other('),
                   fixture.replace('`endif // clock', ''),
                   fixture.replace('// BEGIN M1DE_PROBE9_V1', ''),
                   patched.replace('.coverage_enable(1\'b1)', '.coverage_enable(1\'b0)', 1)):
        try:
            edited(broken)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe fixture accepted')
    assert PAYLOAD.count('ucie_fdi_m1_sva #(.NBYTES(64))') == 2
    for iface in ('ds_fdi_if', 'us_fdi_if'):
        assert '.lclk(' + iface + '.lclk)' in PAYLOAD
        assert '.tb_domain_reset_n(~' + iface + '.reset)' in PAYLOAD
        for signal in ('lp_data', 'pl_data', 'lp_stream', 'pl_stream', 'pl_state_sts'):
            assert '.' + signal + '(' + iface + '.' + signal + ')' in PAYLOAD
    assert 'force ' not in scrub(PAYLOAD)
    assert PAYLOAD.count(': 4\'bxxxx') == 4
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        topology = root / REL
        topology.parent.mkdir(parents=True)
        topology.write_text(fixture)
        sva = root / SVA_REL
        sva.parent.mkdir(parents=True)
        sva.write_text('module ucie_fdi_m1_sva #(parameter NBYTES=64)(\n' +
                       ',\n'.join('input logic ' + name for name in PORTS) + '\n); endmodule')
        types = root / CORE_REL / 'ucie_flit_format_types_pkg.sv'
        types.write_text('package ucie_flit_format_types_pkg;\n' +
                         '\n'.join('localparam ' + name + '=0;' for name in
                         ('UCIE_PROTOCOL_STREAMING_NO_MTP', 'UCIE_FORMAT_2_68B',
                          'UCIE_STATE_STS_RESET', 'UCIE_STREAM_STACK0_STREAMING')) + '\nendpackage')
        (topology.parent / FILELIST).write_text('fixture only\n')
        assert locate(str(topology)) == topology.resolve()
        assert project_for(topology) == root
        preflight(root)
        # Exercise the real CLI and backup/undo paths, not only string edits.
        cli = [sys.executable, str(Path(__file__).resolve()), '--target', str(topology)]
        def run(*options):
            return subprocess.run(cli + list(options), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True)
        result = run('--dry-run')
        assert result.returncode == 0, result.stderr
        assert topology.read_text() == fixture
        result = run()
        assert result.returncode == 0 and 'APPLIED:' in result.stdout, result.stderr
        assert topology.read_text() == patched
        backup = topology.with_name(topology.name + '.fix10_backup')
        assert backup.read_text() == fixture
        assert 'ALREADY_APPLIED' in run().stdout
        topology.write_text(patched + '// later user edit\n')
        result = run('--undo')
        assert result.returncode == 1 and 'automatic undo refused' in result.stderr
        assert topology.read_text().endswith('// later user edit\n')
        topology.write_text(patched)
        result = run('--undo')
        assert result.returncode == 0 and topology.read_text() == fixture, result.stderr
        types.write_text('package ucie_flit_format_types_pkg; endpackage')
        try:
            preflight(root)
        except ValueError:
            pass
        else:
            raise AssertionError('Missing canonical constants accepted')
    print('SELF_TEST_PASS: insertion, idempotence, restoration, ports and source guards')
    print('NOTE: synthetic fixtures only; no SystemVerilog compiler was run.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target')
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--dry-run', action='store_true')
    operation.add_argument('--undo', action='store_true')
    operation.add_argument('--build', action='store_true')
    operation.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    target = locate(args.target)
    original = target.read_bytes()
    backup = target.with_name(target.name + '.fix10_backup')
    if args.undo:
        if not backup.is_file():
            raise ValueError('fix10 backup is missing')
        before = backup.read_bytes()
        expected, _ = edited(before.decode('utf-8'))
        if original != expected.encode('utf-8'):
            raise ValueError('Topology changed after fix10; automatic undo refused')
        target.write_bytes(before)
        print('RESTORED:', target)
        return
    project = project_for(target)
    preflight(project)
    if args.build:
        build(target, project)
        return
    result, changed = edited(original.decode('utf-8'))
    print('TARGET:', target)
    if not changed:
        print('ALREADY_APPLIED: DS and US SVA connections')
        return
    if args.dry_run:
        print('DRY_RUN_OK: two SVA instances; files unchanged')
        return
    if backup.exists() and backup.read_bytes() != original:
        raise ValueError('Different fix10 backup exists; refusing overwrite')
    if not backup.exists():
        with backup.open('xb') as output:
            output.write(original)
    target.write_bytes(result.encode('utf-8'))
    print('BACKUP:', backup)
    print('APPLIED: DS/US SVA; 64-byte bus, inverted reset, checks/coverage enabled')
    print('NEXT: run this script again with --build for debug10')


if __name__ == '__main__':
    try:
        main()
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
