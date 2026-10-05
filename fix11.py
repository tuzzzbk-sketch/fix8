#!/usr/bin/env python3
"""Fix the invalid byte-7 diagnostic slice, then optionally run debug11.

Keep this file beside fix10.py in incoming/fix8.
python3 fix11.py             # backup + diagnostic-only edit
python3 fix11.py --build     # foreground build; progress + exit status
Options: --dry-run, --undo, --self-test.
Payload extraction and SVA connections are not changed.
No VCS validation has been performed locally; run debug11 on the server.
"""
import argparse
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import fix10
except ImportError:
    print('STOP: put fix11.py beside fix10.py', file=sys.stderr)
    sys.exit(1)

WRONG = re.compile(r'\btr\s*\.\s*data\s*\[\s*beat\s*\]\s*\[\s*63\s*:\s*56\s*\]')
OLD_LABEL = 'raw63_56='
NEW_LABEL = 'raw_byte7='
NEW_EXPR = 'tr.data[beat][7]'


def edited(source):
    matches = list(re.finditer(r'^.*M1DE_SNPS_BYTE7.*$', source, re.M))
    if len(matches) != 1:
        raise ValueError('Expected exactly one M1DE_SNPS_BYTE7 diagnostic line')
    match = matches[0]
    line = match.group()
    if OLD_LABEL not in line:
        if (line.count(NEW_LABEL) == 1 and line.count(NEW_EXPR) == 1
                and not WRONG.search(source)):
            return source, False
        raise ValueError('Unexpected existing byte-7 diagnostic; no edit made')
    if (line.count(OLD_LABEL) != 1 or NEW_LABEL in line
            or len(WRONG.findall(line)) != 1 or len(WRONG.findall(source)) != 1):
        raise ValueError('Unexpected diagnostic slice or additional occurrences')
    replacement = WRONG.sub(NEW_EXPR, line).replace(OLD_LABEL, NEW_LABEL)
    return source[:match.start()] + replacement + source[match.end():], True


def adapter_for(project):
    paths = list((project / 'adapter/synopsys').glob('*/adapter/ucie_opfmt_snps_adapter.sv'))
    if len(paths) != 1:
        raise ValueError('Expected one live adapter; found %d' % len(paths))
    return paths[0].resolve()


def patch(adapter, dry_run=False, undo=False):
    before = adapter.read_bytes()
    backup = adapter.with_name(adapter.name + '.fix11_backup')
    if undo:
        if not backup.is_file():
            raise ValueError('fix11 backup missing')
        original = backup.read_bytes()
        expected, _ = edited(original.decode('utf-8'))
        if before != expected.encode('utf-8'):
            raise ValueError('Adapter changed after fix11; undo refused')
        adapter.write_bytes(original)
        print('RESTORED:', adapter)
        return
    after, changed = edited(before.decode('utf-8'))
    print('ADAPTER:', adapter)
    if not changed:
        print('ALREADY_APPLIED: byte-7 diagnostic is corrected')
        return
    if dry_run:
        print('DRY_RUN_OK: diagnostic expression + label only')
        return
    if backup.exists() and backup.read_bytes() != before:
        raise ValueError('Different fix11 backup exists; refusing overwrite')
    if not backup.exists():
        with backup.open('xb') as output:
            output.write(before)
    adapter.write_bytes(after.encode('utf-8'))
    print('BACKUP:', backup)
    print('APPLIED: raw63_56 / [63:56] -> raw_byte7 / [7] in debug output only')
    print('NEXT: python3 fix11.py --build')


def build(project, topology, adapter):
    if edited(adapter.read_text())[1]:
        raise ValueError('Apply fix11 before --build')
    if fix10.edited(topology.read_text())[1]:
        raise ValueError('The required fix10 SVA block is missing')
    if not shutil.which('gmake'):
        raise ValueError('gmake is unavailable in this shell')
    log = project / 'logs/compile_pipe_debug11.log'
    command = ['gmake', '-B', fix10.TEST, 'USE_SIMULATOR=vcsvlog',
               'SVT_UCIE_COMPILE_FILE=' + fix10.FILELIST,
               'SVT_UCIE_TOPOLOGY_FILE=' + fix10.NAME]
    print('BUILD_CWD:', topology.parent, flush=True)
    print('LOG:', log, flush=True)
    # Keep Python alive until gmake exits. No detached session or log overwrite.
    with log.open('xb') as output:
        proc = subprocess.Popen(command, cwd=str(topology.parent), stdout=output,
                                stderr=subprocess.STDOUT)
        print('BUILD_STARTED: pid=%d; wait for BUILD_EXIT' % proc.pid, flush=True)
        started = time.monotonic()
        while True:
            try:
                code = proc.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                print('BUILD_RUNNING: elapsed=%ds' % (time.monotonic() - started), flush=True)
        output.write(('\nM1DE_BUILD_EXIT rc=%d\n' % code).encode('utf-8'))
    print('BUILD_EXIT: rc=%d (rc=0 still requires checking simulation/SVA results)' % code, flush=True)
    return code if code >= 0 else 128 - code


def self_test():
    line = ('if(tr.data.size() == 0) return; foreach (tr.data[beat]) begin '
            'bytes = new[64]; foreach (bytes[i]) bytes[i] = tr.data[beat][i]; '
            '`uvm_info("M1DE_SNPS_BYTE7", $sformatf("raw63_56=%02h bytes7=%02h", '
            'tr.data[beat][63:56], bytes[7]), UVM_NONE); '
            'publish_observation(role, direction, sequence_number, beat, origin); end\n')
    fixture = 'class adapter;\n' + line + 'endclass\n'
    expected = fixture.replace(OLD_LABEL, NEW_LABEL).replace('tr.data[beat][63:56]', NEW_EXPR)
    assert edited(fixture) == (expected, True)
    assert edited(expected) == (expected, False)
    for broken in (fixture + line, fixture.replace(OLD_LABEL, 'other='),
                   fixture + 'logic x = tr.data[beat][63:56];\n'):
        try:
            edited(broken)
        except ValueError:
            pass
        else:
            raise AssertionError('Unsafe source accepted')
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        adapter = project / 'adapter/synopsys/fixture/adapter/ucie_opfmt_snps_adapter.sv'
        adapter.parent.mkdir(parents=True)
        adapter.write_text(fixture)
        assert adapter_for(project) == adapter
        patch(adapter, dry_run=True)
        assert adapter.read_text() == fixture
        patch(adapter)
        assert adapter.read_text() == expected
        patch(adapter)
        adapter.write_text(expected + '// later edit\n')
        try:
            patch(adapter, undo=True)
        except ValueError:
            pass
        else:
            raise AssertionError('Undo overwrote a later change')
        adapter.write_text(expected)
        topology = project / fix10.REL
        topology.parent.mkdir(parents=True)
        topology.write_text(fix10.PAYLOAD)
        (project / 'logs').mkdir()
        bin_dir = project / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'gmake'
        fake.write_text('#!' + sys.executable + '\nimport os, sys\n'
                        'print("FIX11_STUB_CWD=" + os.getcwd())\n'
                        'print("FIX11_STUB_ARGS=" + repr(sys.argv[1:]))\n'
                        'sys.exit(3)\n')
        fake.chmod(0o755)
        old_path = os.environ.get('PATH', '')
        try:
            os.environ['PATH'] = str(bin_dir) + os.pathsep + old_path
            assert build(project, topology, adapter) == 3
            log = project / 'logs/compile_pipe_debug11.log'
            saved = log.read_bytes()
            assert str(topology.parent).encode() in saved
            for arg in (fix10.TEST, fix10.FILELIST, fix10.NAME, 'USE_SIMULATOR=vcsvlog'):
                assert arg.encode() in saved
            assert b'M1DE_BUILD_EXIT rc=3' in saved
            try:
                build(project, topology, adapter)
            except FileExistsError:
                pass
            else:
                raise AssertionError('Build overwrote an existing log')
            assert log.read_bytes() == saved
        finally:
            os.environ['PATH'] = old_path
        patch(adapter, undo=True)
        assert adapter.read_text() == fixture
    print('SELF_TEST_PASS: diagnostic-only patch, backup, idempotence, undo, build cwd/args/exit and log protection')
    print('NOTE: fake build only; no VCS/SystemVerilog compiler was run.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--build', action='store_true')
    operation.add_argument('--dry-run', action='store_true')
    operation.add_argument('--undo', action='store_true')
    operation.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    topology = fix10.locate(None)
    project = fix10.project_for(topology)
    adapter = adapter_for(project)
    if args.build:
        return build(project, topology, adapter)
    patch(adapter, args.dry_run, args.undo)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, UnicodeError, ValueError) as exc:
        print('STOP:', exc, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nINTERRUPTED: inspect the debug11 log before starting another build', file=sys.stderr)
        sys.exit(130)
