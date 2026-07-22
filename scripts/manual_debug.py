#!/usr/bin/env python3
"""Debug a whole transaction's input scripts from values you pass on the CLI.

No network calls: you copy the values off mempool.space (or any explorer)
in your browser and pass them as arguments.

Usage:
    python scripts/manual_debug.py RAW_TX \\
        --spent SCRIPT_HEX AMOUNT_SATS [--spent SCRIPT_HEX AMOUNT_SATS ...]

Pass one --spent per input, in input order -- the coin each input spends.
Every input's prevout is required: all inputs are verified, and taproot
signatures commit to all spent outputs.

Arguments, and where to find them on https://mempool.space/tx/<txid>:

  RAW_TX        The whole spending transaction, serialized as hex. mempool
                doesn't show this in its UI; open it in your browser at
                https://mempool.space/api/tx/<txid>/hex and copy the text.

  --spent       One (SCRIPT_HEX AMOUNT_SATS) pair per input, in the same
                order as the tx's inputs. Expand an input on the tx page:
                "Previous output script" (switch it to hex) is the
                scriptpubkey; the input's value is the amount (toggle the
                display to sats, or BTC * 100_000_000).

Example (a real mainnet P2PKH spend, one input):

    python scripts/manual_debug.py \\
        02000000013f7cebd65c27431a90bba7f796914fe8cc2ddfc3f2cbd6f7e5f2fc854534da95000000006b483045022100de1ac3bcdfb0332207c4a91f3832bd2c2915840165f876ab47c5f8996b971c3602201c6c053d750fadde599e6f5c4e1963df0f01fc0d97815e8157e3d59fe09ca30d012103699b464d1d8bc9e47d4fb1cdaa89a1c5783d68363c4dbc4b524ed3d857148617feffffff02836d3c01000000001976a914fc25d6d5c94003bf5b0c7b640a248e2c637fcfb088ac7ada8202000000001976a914fbed3d9b11183209a57999d54d59f67c019e756c88ac6acb0700 \\
        --spent 76a9144bfbaf6afb76cc5771bc6404810d1cc041a6933988ac 0
"""

import argparse

import pybitcoinkernel as pbk


def parse_args():
    parser = argparse.ArgumentParser(
        description="Step through Bitcoin's script interpreter for a whole tx.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("raw_tx", help="raw spending transaction, hex")
    parser.add_argument(
        "--spent",
        action="append",
        nargs=2,
        required=True,
        metavar=("SCRIPT_HEX", "AMOUNT_SATS"),
        help="prevout of one input, in order; repeat once per input",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not pbk.trace_available():
        raise SystemExit(
            "script tracing is unavailable; rebuild libbitcoinkernel with "
            "-DENABLE_SCRIPT_TRACE=ON."
        )

    tx = pbk.Transaction(bytes.fromhex(args.raw_tx))

    if len(args.spent) != tx.n_inputs:
        raise SystemExit(
            f"this tx has {tx.n_inputs} input(s), but you passed "
            f"{len(args.spent)} --spent value(s).\nProvide one "
            f"--spent SCRIPT_HEX AMOUNT_SATS per input, in order."
        )

    try:
        spent_outputs = [
            pbk.TransactionOutput(pbk.ScriptPubkey(bytes.fromhex(s)), int(a))
            for s, a in args.spent
        ]
    except ValueError:
        raise SystemExit(
            "invalid --spent value: SCRIPT_HEX must be hex and AMOUNT_SATS "
            "an integer number of satoshis."
        )

    # debug_transaction traces every input (building the shared precomputed
    # transaction data for taproot itself) and returns one ScriptTrace each.
    traces = pbk.debug_transaction(tx, spent_outputs)
    overall = all(t.valid for t in traces)

    print(f"transaction script verification: {'VALID' if overall else 'INVALID'} "
          f"({tx.n_inputs} input(s))")
    for i, trace in enumerate(traces):
        print()
        print(f"########## input {i} ##########")
        print(trace.format(max_item_bytes=16))


if __name__ == "__main__":
    main()
