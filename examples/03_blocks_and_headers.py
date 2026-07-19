#!/usr/bin/env python3
"""Parsing blocks, headers and block hashes.

A Block is created from consensus-serialized bytes and behaves like a
sequence of transactions. This example dissects the mainnet genesis
block.

Run:  python examples/03_blocks_and_headers.py
"""

import datetime

import pybitcoinkernel as pbk

GENESIS_BLOCK = bytes.fromhex(
    "0100000000000000000000000000000000000000000000000000000000000000"
    "000000003ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa"
    "4b1e5e4a29ab5f49ffff001d1dac2b7c01010000000100000000000000000000"
    "00000000000000000000000000000000000000000000ffffffff4d04ffff001d"
    "0104455468652054696d65732030332f4a616e2f32303039204368616e63656c"
    "6c6f72206f6e206272696e6b206f66207365636f6e64206261696c6f75742066"
    "6f722062616e6b73ffffffff0100f2052a01000000434104678afdb0fe554827"
    "1967f1a67130b7105cd6a828e03909a67962e0ea1f61deb649f6bc3f4cef38c4"
    "f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5fac00000000"
)


def main() -> None:
    block = pbk.Block(GENESIS_BLOCK)

    print(f"block hash:  {block.hash.hex()}")

    # Blocks act as sequences of transactions.
    print(f"tx count:    {len(block)}")
    coinbase = block[0]
    print(f"coinbase:    {coinbase.txid.hex()}")

    # The famous newspaper headline is embedded in the coinbase input.
    # (The scriptSig of a coinbase input is arbitrary data.)
    raw_coinbase = coinbase.to_bytes()
    print(f"message:     {raw_coinbase[50:119].decode('ascii')!r}")

    # An 80-byte header can also be parsed standalone.
    header = pbk.BlockHeader(GENESIS_BLOCK[:80])
    when = datetime.datetime.fromtimestamp(header.timestamp, datetime.timezone.utc)
    print(f"version:     {header.version}")
    print(f"timestamp:   {header.timestamp} ({when:%Y-%m-%d %H:%M:%S} UTC)")
    print(f"bits:        {header.bits:#010x}")
    print(f"nonce:       {header.nonce}")
    print(f"prev hash:   {header.prev_hash.hex()}")
    assert header.hash == block.hash

    # BlockHash values are constructed from 32 bytes in *internal* byte
    # order; .hex() shows the display (reversed) order.
    same_hash = pbk.BlockHash(block.hash.to_bytes())
    print(f"hash equal:  {same_hash == block.hash}")


if __name__ == "__main__":
    main()
