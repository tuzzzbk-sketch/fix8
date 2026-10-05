#!/usr/bin/env python3
"""Connect external multi-stack FDI TX to the selected vendor scoreboard.

Run from tb_ucie_svt_uvm_basic_sys: python3 /path/to/fix8.py
Optional target: python3 fix8.py /path/to/env/ucie_ts_env.sv
Read-only preview: --check. Restore this patch only: --undo.
No external dependencies. No simulation is started.
"""
from __future__ import print_function

import argparse
import hashlib
import os
import re
import stat
import sys
import tempfile

BEGIN = '// M1DE_EXTERNAL_TX_FIX_BEGIN'
END = '// M1DE_EXTERNAL_TX_FIX_END'
REMOTE_TARGET = ('/remote/in01sgnfs00010/luantran/VIP101/m1de_integration/0p8p0/'
                 'build/m1de_snps_discovery_01/design_dir/examples/sverilog/'
                 'ucie_svt/tb_ucie_svt_uvm_basic_sys/env/ucie_ts_env.sv')
BLOCK = '''// M1DE_EXTERNAL_TX_FIX_BEGIN
`ifdef SVT_UCIE_VERIFY_MULTI_STACK_WITH_ONE_FDI_DISABLED
if (!env_cfg.disable_scoreboard && env_cfg.is_both_stack_streaming) begin
  if (env_cfg.include_ds_protocol) begin
    int stack_idx;
    stack_idx = env_cfg.include_ds_protocol_fdi_stack0_passive ? 0 : 1;
    ds_die.multi_stack_fdi_agent.fdi_mon.tx_fdi_xact_observed_port.connect(
      scoreboard[stack_idx][0].item_observed_fdi_ds_tx_export);
  end
  if (env_cfg.include_us_protocol) begin
    int stack_idx;
    stack_idx = env_cfg.include_us_protocol_fdi_stack0_passive ? 0 : 1;
    us_die.multi_stack_fdi_agent.fdi_mon.tx_fdi_xact_observed_port.connect(
      scoreboard[stack_idx][0].item_observed_fdi_us_tx_export);
  end
end
`endif
// M1DE_EXTERNAL_TX_FIX_END

'''
ANCHOR = re.compile(
    r'(?m)^[ \t]*if\s*\(\s*!\s*env_cfg\.disable_scoreboard\s*\)\s*begin[ \t]*\r?\n'
    r'[ \t]*//[ \t]*Get a pointer for configuration to be used in scoreboard[ \t]*\r?$')
FUNCTION = re.compile(
    r'(?m)^[ \t]*(?:virtual\s+)?function\b[^;]*\bconnect_phase\s*\([^;]*\)\s*;')


def fail(message):
    raise ValueError(message)


def read(path):
    with open(path, 'rb') as handle:
        return handle.read()


def patch(data):
    text = data.decode('utf-8')
    if BEGIN in text or END in text:
        fail('Patch markers already exist; refusing a duplicate insertion.')
    if re.search(r'multi_stack_fdi_agent\s*\.\s*fdi_mon\s*\.\s*'
                 r'tx_fdi_xact_observed_port\s*\.\s*connect\s*\(', text):
        fail('An external TX connection already exists; review it first.')
    functions = list(FUNCTION.finditer(text))
    if len(functions) != 1:
        fail('Expected exactly one connect_phase function; found %d.' % len(functions))
    start = functions[0].end()
    end = re.search(r'(?m)^[ \t]*endfunction\b', text[start:])
    if end is None:
        fail('Cannot find the end of connect_phase.')
    stop = start + end.start()
    anchors = list(ANCHOR.finditer(text))
    if len(anchors) != 1 or not (start <= anchors[0].start() < stop):
        fail('Expected one configuration-pointer anchor inside connect_phase.')
    newline = '\r\n' if '\r\n' in text else '\n'
    block = BLOCK.replace('\n', newline)
    position = anchors[0].start()
    return (text[:position] + block + text[position:]).encode('utf-8'), text[:position].count('\n') + 1


def atomic_write(path, data):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix='.m1de_tx_', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', nargs='?', default=None)
    options = parser.add_mutually_exclusive_group()
    options.add_argument('--check', action='store_true', help='Preview without writing')
    options.add_argument('--undo', action='store_true', help='Restore only an unchanged application of this patch')
    args = parser.parse_args()
    if args.target:
        path = os.path.abspath(args.target)
    elif os.path.isfile('env/ucie_ts_env.sv'):
        path = os.path.abspath('env/ucie_ts_env.sv')
    else:
        path = REMOTE_TARGET
    if os.path.islink(path) or not os.path.isfile(path):
        fail('Target must be a regular non-symlink file: ' + path)
    original = read(path)
    backup = path + '.m1de_tx_fix.bak'
    if args.undo:
        if os.path.islink(backup) or not os.path.isfile(backup):
            fail('Backup not found: ' + backup)
        saved = read(backup)
        expected, unused = patch(saved)
        if original != expected:
            fail('Target changed after patching; automatic undo refused.')
        atomic_write(path, saved)
        print('RESTORED:', path)
        return
    if BEGIN.encode('ascii') in original:
        if os.path.islink(backup) or not os.path.isfile(backup):
            fail('Markers exist but backup is missing; review manually.')
        expected, unused = patch(read(backup))
        if original != expected:
            fail('Patched file differs from the expected result; review manually.')
        print('ALREADY APPLIED; no changes.')
        return
    updated, line = patch(original)
    print('TARGET:', path)
    print('INSERT BEFORE LINE:', line)
    print('SCOPE: external DS/US TX only; stack selected by stack0_passive.')
    if args.check:
        print('CHECK OK; no files changed.')
        return
    if os.path.lexists(backup):
        if os.path.islink(backup) or not os.path.isfile(backup) or read(backup) != original:
            fail('Existing backup differs from current source; refusing overwrite.')
    else:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             stat.S_IMODE(os.stat(path).st_mode))
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    if read(path) != original:
        fail('Source changed during this operation; patch not written.')
    atomic_write(path, updated)
    print('BACKUP:', backup)
    print('SHA256:', hashlib.sha256(updated).hexdigest())
    print('APPLIED. Compile/run the F2 test into a NEW log (debug8).')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, UnicodeError) as error:
        print('STOP:', str(error), file=sys.stderr)
        sys.exit(1)
