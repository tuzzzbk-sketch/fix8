#!/usr/bin/env python3
"""Read-only metadata collection for the existing fix15 checkpoint.

Does not run a simulator, modify source, copy source content, or qualify a release.
Writes one new JSON report exclusively; an existing output is never overwritten.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROJ = '/remote/in01sgnfs00010/luantran/VIP101/m1de_integration/0p8p0'
TB_REL = 'build/m1de_snps_discovery_01/design_dir/examples/sverilog/ucie_svt/tb_ucie_svt_uvm_basic_sys'
SKIP_DIRS = {'.git', '__pycache__', 'work', 'csrc', 'simv.daidir', 'DVEfiles'}
SKIP_SUFFIXES = {'.wlf', '.vcd', '.fsdb', '.ucdb', '.o', '.so', '.a', '.pyc', '.log'}
SUMMARY = re.compile(
    r'(?:M1DE_BUILD_EXIT|SMOKE_GATE|BASELINE_READY_GATE|SEED_GATE|'
    r'BACKPRESSURE_GATE|STALL_COVER_[DU]S|M1DE_BP1[35]_|'
    r'UCIE_E2E_SUMMARY|UCIE_INT_SHADOW_SUMMARY|SvtTestEpilog|'
    r'Number of (?:caught|demoted) UVM_(?:ERROR|FATAL)|'
    r'^\s*#?\s*UVM_(?:WARNING|ERROR|FATAL)\s*:|'
    r'\b(?:VCS|UVM|Version)\b.*(?:20\d\d|1\.1|1\.2)|'
    r'^\s*(?:UVM_ERROR|UVM_FATAL|Error-|Fatal-|Warning-))'
)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def metadata(path, with_hash=True):
    result = {'path': str(path), 'exists': path.exists(), 'is_symlink': path.is_symlink()}
    if path.is_symlink():
        result['link_target'] = os.readlink(str(path))
        result['resolved_target'] = str(path.resolve())
        result['target_content_not_copied'] = True
    if not path.exists():
        return result
    try:
        if path.is_file():
            result['bytes'] = path.stat().st_size
            # Link identity is listed, not silently converted to a copied target.
            if with_hash and not path.is_symlink():
                result['sha256'] = digest(path)
        else:
            result['is_directory'] = path.is_dir()
    except OSError as exc:
        result['read_error'] = str(exc)
    return result

def log_metadata(path):
    result = metadata(path)
    result['selected_lines'] = []
    if not path.is_file():
        return result
    try:
        with path.open('r', encoding='utf-8', errors='replace') as stream:
            for number, line in enumerate(stream, 1):
                if SUMMARY.search(line):
                    result['selected_lines'].append({'line': number, 'text': line.rstrip()})
    except OSError as exc:
        result['read_error'] = str(exc)
    # This is evidence extraction, not an exact-count log validator.
    result['full_log_review_required'] = True
    return result

def inventory(directory):
    rows = []
    if not directory.is_dir():
        return [metadata(directory)]
    for current, dirs, files in os.walk(str(directory), followlinks=False):
        for name in sorted(dirs):
            child = Path(current) / name
            if child.is_symlink():
                rows.append(metadata(child, False))
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not (Path(current) / name).is_symlink())
        for name in sorted(files):
            child = Path(current) / name
            if child.suffix.lower() in SKIP_SUFFIXES:
                continue
            rows.append(metadata(child))
    return rows

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proj', type=Path, default=Path(DEFAULT_PROJ))
    parser.add_argument('--out', type=Path, default=Path('M1DE_VCS_fix15_evidence_inventory.json'))
    args = parser.parse_args()
    proj = args.proj.expanduser().resolve()
    if not proj.is_dir():
        parser.error('Project directory does not exist: ' + str(proj))
    tb = proj / TB_REL
    result = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'decision': 'EVIDENCE_INVENTORY_ONLY_NOT_QUALIFICATION',
        'simulation_run': False,
        'source_content_copied': False,
        'qualified': False,
        'proj': str(proj),
        'tb': str(tb),
        'tools_on_path_not_verified_versions': {name: shutil.which(name) for name in ['vcs', 'verdi', 'urg', 'gmake', 'python3']},
        'installation_environment': {name: os.environ.get(name) for name in ['DESIGNWARE_HOME', 'DIR_VIP', 'VCS_HOME', 'UVM_HOME']},
        'logs': [log_metadata(proj / 'logs' / ('compile_pipe_debug15_seed%d.log' % seed)) for seed in [50, 51, 52]],
        'debug11_preflight': log_metadata(proj / 'logs' / 'compile_pipe_debug11.log'),
        'scripts': [metadata(proj / 'incoming/fix8' / name) for name in ['fix8.py', 'probe9.py', 'fix10.py', 'fix11.py', 'fix13.py', 'fix14.py', 'fix15.py']],
        'tb_root_inputs': [],
        'source_inventory': [],
        'limits': [
            'Missing exact log names are reported; suffix retries are not automatically substituted.',
            'Selected lines do not replace full raw log validation, seed/route byte oracle or per-port discovery evidence.',
            'Tool paths do not establish actual tool/UVM versions; obtain the run-specific banner/config manifest.',
            'Hashes do not establish complete include dependency closure or capture symlink target content.',
            'No archive restore/replay, vendor scoreboard coexistence review or release qualification was performed.'
        ]
    }
    required = ['Makefile', 'sim_run_options', 'compile_snps_ucie_both_streaming_multi_stack_d2d_dut_with_shim.f', 'topology_snps_ucie_both_streaming_multi_stack_d2d_dut_with_shim.svi']
    candidates = {tb / name for name in required}
    if tb.is_dir():
        for pattern in ['*.f', '*.svi', '*run_options*']:
            candidates.update(tb.glob(pattern))
    result['tb_root_inputs'] = [metadata(path) for path in sorted(candidates)]
    for directory in [proj / 'src', proj / 'adapter', tb / 'tests', tb / 'env']:
        result['source_inventory'].extend(inventory(directory))
    # Serialize before creating output; failure cannot leave a partial validation claim.
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    with args.out.expanduser().open('x', encoding='utf-8') as stream:
        stream.write(payload)
    print('Saved inventory: ' + str(args.out.expanduser().resolve()))
    print('Simulation NOT RUN; full log review/replay still required; qualified=false')
    missing = [row['path'] for row in result['logs'] + result['scripts'] + result['tb_root_inputs'] if not row['exists']]
    print('Missing named inputs: %d' % len(missing))
    for name in missing:
        print('  ' + name)

if __name__ == '__main__':
    main()
