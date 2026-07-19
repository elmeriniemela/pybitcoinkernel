"""A minimal pure-Python regtest block miner used by the examples.

Regtest's proof-of-work target (nBits 0x207fffff) is trivial, so grinding
a valid nonce takes only a couple of hash attempts per block. Mined
blocks contain a coinbase paying 50 BTC to an OP_TRUE (anyone-can-spend)
output, plus any extra raw transactions supplied by the caller.

This is test/demo tooling: it builds consensus-valid blocks for a chain
where nobody else is mining. It is not a real miner.
"""

import hashlib
import struct

REGTEST_BITS = 0x207FFFFF
REGTEST_TARGET = 0x7FFFFF << (8 * (0x20 - 3))
REGTEST_GENESIS_TIME = 1296688602
COIN = 100_000_000
SUBSIDY = 50 * COIN
OP_TRUE_SCRIPT = b"\x51"


def dsha256(data: bytes) -> bytes:
    """Bitcoin's double-SHA256 hash."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ser_varint(n: int) -> bytes:
    if n < 0xFD:
        return struct.pack("<B", n)
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def ser_tx(inputs, outputs, version=1, locktime=0) -> bytes:
    """Serialize a transaction (no witness data).

    inputs:  list of (prevout_txid: bytes32, prevout_index: int, script_sig: bytes)
    outputs: list of (amount: int, script_pubkey: bytes)
    """
    tx = struct.pack("<i", version)
    tx += ser_varint(len(inputs))
    for txid, index, script_sig in inputs:
        tx += txid + struct.pack("<I", index)
        tx += ser_varint(len(script_sig)) + script_sig
        tx += struct.pack("<I", 0xFFFFFFFF)  # sequence
    tx += ser_varint(len(outputs))
    for amount, script_pubkey in outputs:
        tx += struct.pack("<q", amount)
        tx += ser_varint(len(script_pubkey)) + script_pubkey
    tx += struct.pack("<I", locktime)
    return tx


def bip34_height_push(height: int) -> bytes:
    """The canonical script push of the block height (CScript() << height)
    that BIP34 requires the coinbase scriptSig to start with."""
    if 1 <= height <= 16:
        return bytes([0x50 + height])  # OP_1 .. OP_16
    data = b""
    n = height
    while n:
        data += bytes([n & 0xFF])
        n >>= 8
    if data[-1] & 0x80:
        data += b"\x00"
    return bytes([len(data)]) + data


def make_coinbase(height: int, amount: int = SUBSIDY) -> bytes:
    # Pad with OP_0 to satisfy the 2-byte scriptSig minimum for low heights.
    script_sig = bip34_height_push(height) + b"\x00"
    return ser_tx(
        inputs=[(b"\x00" * 32, 0xFFFFFFFF, script_sig)],
        outputs=[(amount, OP_TRUE_SCRIPT)],
    )


def merkle_root(txids: list[bytes]) -> bytes:
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [dsha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def mine_block(prev_hash: bytes, height: int, extra_txs: list[bytes] | None = None) -> bytes:
    """Mine a regtest block on top of prev_hash (internal byte order).

    Returns the serialized block. extra_txs are raw serialized
    transactions included after the coinbase.
    """
    txs = [make_coinbase(height)] + list(extra_txs or [])
    txids = [dsha256(tx) for tx in txs]
    root = merkle_root(txids)
    timestamp = REGTEST_GENESIS_TIME + height
    nonce = 0
    while True:
        header = (
            struct.pack("<i", 4)
            + prev_hash
            + root
            + struct.pack("<I", timestamp)
            + struct.pack("<I", REGTEST_BITS)
            + struct.pack("<I", nonce)
        )
        block_hash = dsha256(header)
        if int.from_bytes(block_hash, "little") <= REGTEST_TARGET:
            break
        nonce += 1
    return header + ser_varint(len(txs)) + b"".join(txs)


def mine_chain(start_hash: bytes, num_blocks: int, start_height: int = 1) -> list[bytes]:
    """Mine num_blocks empty blocks on top of start_hash."""
    blocks = []
    prev = start_hash
    for height in range(start_height, start_height + num_blocks):
        block = mine_block(prev, height)
        blocks.append(block)
        prev = dsha256(block[:80])
    return blocks
