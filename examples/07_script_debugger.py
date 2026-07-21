#!/usr/bin/env python3
"""Stepping through Bitcoin's script interpreter opcode by opcode.

verify_script() answers *whether* a spend is valid; the script debugger
shows *how* the interpreter reached that answer. It hooks Bitcoin Core's
script trace callback (PR #35641) to capture a snapshot of the stack on
evaluator entry, before every opcode, and on exit — for the scriptSig,
the scriptPubkey, and any witness script that runs.

This needs a kernel built with -DENABLE_SCRIPT_TRACE=ON. On a kernel
without it, debug_script() raises KernelError; trace_available() tells
you up front.

Run:  python examples/07_script_debugger.py
"""

import pybitcoinkernel as pbk

# Legacy P2PKH spend (also used in example 02 and Bitcoin Core's tests):
# tx aca326a724eda9a461c10a876534ecd5ae7b27f10f26c3862fb996f80ea2d45d
LEGACY_SCRIPT = bytes.fromhex("76a9144bfbaf6afb76cc5771bc6404810d1cc041a6933988ac")
LEGACY_TX = bytes.fromhex(
    "02000000013f7cebd65c27431a90bba7f796914fe8cc2ddfc3f2cbd6f7e5f2fc854534da"
    "95000000006b483045022100de1ac3bcdfb0332207c4a91f3832bd2c2915840165f876ab"
    "47c5f8996b971c3602201c6c053d750fadde599e6f5c4e1963df0f01fc0d97815e8157e3"
    "d59fe09ca30d012103699b464d1d8bc9e47d4fb1cdaa89a1c5783d68363c4dbc4b524ed3"
    "d857148617feffffff02836d3c01000000001976a914fc25d6d5c94003bf5b0c7b640a24"
    "8e2c637fcfb088ac7ada8202000000001976a914fbed3d9b11183209a57999d54d59f67c"
    "019e756c88ac6acb0700"
)


def main() -> None:
    flags = pbk.ScriptVerificationFlags

    if not pbk.trace_available():
        print("script tracing is unavailable: this libbitcoinkernel was built")
        print("without -DENABLE_SCRIPT_TRACE=ON. Nothing to demo.")
        return

    # --- Disassembly needs no tracing: a pure decoder of script bytes. ---
    print("scriptPubkey disassembly:")
    for pos, name, data in pbk.disassemble(LEGACY_SCRIPT):
        suffix = f"  {data.hex()}" if data else ""
        print(f"  {pos:>2}  {name}{suffix}")

    # --- Trace a full verification and print the btcdeb-style dump. ------
    script = pbk.ScriptPubkey(LEGACY_SCRIPT)
    tx = pbk.Transaction(LEGACY_TX)
    trace = pbk.debug_script(script, 0, tx, 0, flags.ALL ^ flags.TAPROOT)

    print()
    print(trace.format(max_item_bytes=12))

    # --- The trace is also structured data you can walk yourself. --------
    # One ScriptExecution per script the interpreter ran (scriptSig first,
    # then scriptPubkey), each with its per-opcode STEP frames.
    print()
    print(f"verdict: valid={trace.valid}, error={trace.error.name}")
    for i, execution in enumerate(trace.executions):
        depths = [len(step.stack) for step in execution.steps]
        print(
            f"  script #{i} ({execution.sig_version.name}): "
            f"{len(execution.steps)} opcodes, stack depth over time {depths}"
        )

    # --- A failing spend: verify the transaction against a script it does
    # not actually spend. The interpreter still runs to completion, so the
    # trace explains that the failure is a false top-of-stack. ------------
    wrong = pbk.ScriptPubkey(
        bytes.fromhex("0020701a8d401c84fb13e6baf169d59684e17abd9fa216c8cc5b9fc63d622ff8c58d")
    )
    bad = pbk.debug_script(wrong, 0, tx, 0, flags.ALL ^ flags.TAPROOT)
    print()
    print(f"spending the wrong script: valid={bad.valid}")

    # --- Advanced: register your own frame callback for the duration of a
    # block. This is the same hook debug_script() uses; it fires for every
    # script evaluated while the context is open, including during
    # process_block(). Here we just count opcodes across both scripts. ----
    steps = 0

    def on_frame(frame):
        nonlocal steps
        if frame.kind == pbk.ScriptTraceFrameKind.STEP:
            steps += 1

    with pbk.script_trace(on_frame):
        pbk.verify_script(script, 0, tx, 0, flags.ALL ^ flags.TAPROOT)
    print(f"streaming callback saw {steps} opcode steps")


if __name__ == "__main__":
    main()
