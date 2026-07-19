#!/usr/bin/env python3
"""Parsing and inspecting transactions.

A Transaction is created from consensus-serialized bytes (the format used
on the P2P network and returned by `getrawtransaction`). This example
dissects a real mainnet transaction with two inputs and two outputs.

Run:  python examples/01_transactions.py
"""

import pybitcoinkernel as pbk

# Mainnet transaction e37a5907ac519806758fff8137f8d988fba9101c9dc490a95fed4a230215e6ba
RAW_TX = bytes.fromhex(
    "020000000248c03e66fd371c7033196ce24298628e59ebefa00363026044e0f35e0325a6"
    "5d000000006a473044022004893432347f39beaa280e99da595681ddb20fc45010176897"
    "e6e055d716dbfa022040a9e46648a5d10c33ef7cee5e6cf4b56bd513eae3ae044f003982"
    "4b02d0f44c012102982331a52822fd9b62e9b5d120da1d248558fac3da3a3c51cd7d9c8a"
    "d3da760efeffffffb856678c6e4c3c84e39e2ca818807049d6fba274b42af3c6d3f9d4b6"
    "513212d2000000006a473044022068bcedc7fe39c9f21ad318df2c2da62c2dc9522a89c2"
    "8c8420ff9d03d2e6bf7b0220132afd752754e5cb1ea2fd0ed6a38ec666781e34b0e93dc9"
    "a08f2457842cf5660121033aeb9c079ea3e08ea03556182ab520ce5c22e6b0cb95cee643"
    "5ee17144d860cdfeffffff0260d50b00000000001976a914363cc8d55ea8d0500de728ef"
    "6d63804ddddbdc9888ac67040f00000000001976a914c303bdc5064bf9c9a8b507b5496b"
    "d0987285707988ac6acb0700"
)


def main() -> None:
    tx = pbk.Transaction(RAW_TX)

    # Txids compare by value and print in the display (byte-reversed)
    # order used by block explorers.
    print(f"txid:      {tx.txid.hex()}")
    print(f"inputs:    {tx.n_inputs}")
    print(f"outputs:   {tx.n_outputs}")

    # Each input references the output it spends through an out point.
    for i, tx_in in enumerate(tx.inputs):
        point = tx_in.out_point
        print(f"input {i}:   spends {point.txid.hex()}:{point.index}")

    # Outputs carry an amount (in satoshis) and a script pubkey.
    for i, tx_out in enumerate(tx.outputs):
        script = tx_out.script_pubkey.to_bytes()
        print(f"output {i}:  {tx_out.amount} sats  script={script.hex()}")

    # to_bytes() round-trips to the exact bytes we parsed.
    assert tx.to_bytes() == RAW_TX
    print("serialization round-trip: OK")

    # Malformed data raises KernelError.
    try:
        pbk.Transaction(b"definitely not a transaction")
    except pbk.KernelError as e:
        print(f"parsing garbage raises: KernelError({e})")


if __name__ == "__main__":
    main()
