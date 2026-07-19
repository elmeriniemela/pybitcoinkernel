"""Tests for transaction, block, and hash primitives."""

import pytest

import pybitcoinkernel as pbk

# The mainnet genesis block.
GENESIS_BLOCK_HEX = (
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
GENESIS_HASH_HEX = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
GENESIS_TXID_HEX = "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b"

# A mainnet transaction with 2 inputs and 2 outputs (from Bitcoin Core's
# kernel test suite).
TWO_IN_TWO_OUT_TX_HEX = (
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


class TestTransaction:
    def test_roundtrip(self):
        data = bytes.fromhex(TWO_IN_TWO_OUT_TX_HEX)
        tx = pbk.Transaction(data)
        assert tx.to_bytes() == data

    def test_invalid_data_raises(self):
        with pytest.raises(pbk.KernelError):
            pbk.Transaction(b"not a transaction")

    def test_counts(self):
        tx = pbk.Transaction(bytes.fromhex(TWO_IN_TWO_OUT_TX_HEX))
        assert tx.n_inputs == 2
        assert tx.n_outputs == 2
        assert len(tx.inputs) == 2
        assert len(tx.outputs) == 2

    def test_outputs(self):
        tx = pbk.Transaction(bytes.fromhex(TWO_IN_TWO_OUT_TX_HEX))
        out0 = tx.output(0)
        assert out0.amount == 775520
        script = out0.script_pubkey.to_bytes()
        assert script.hex() == "76a914363cc8d55ea8d0500de728ef6d63804ddddbdc9888ac"
        with pytest.raises(IndexError):
            tx.output(2)

    def test_inputs_and_out_points(self):
        tx = pbk.Transaction(bytes.fromhex(TWO_IN_TWO_OUT_TX_HEX))
        point = tx.input(0).out_point
        assert point.index == 0
        assert point.txid.hex() == (
            "5da625035ef3e04460026303a0efeb598e629842e26c1933701c37fd663ec048"
        )
        with pytest.raises(IndexError):
            tx.input(2)

    def test_txid_equality(self):
        tx = pbk.Transaction(bytes.fromhex(TWO_IN_TWO_OUT_TX_HEX))
        assert tx.txid == tx.txid
        assert tx.txid != tx.input(0).out_point.txid
        assert len({tx.txid, tx.txid}) == 1


class TestScriptPubkey:
    def test_roundtrip(self):
        data = bytes.fromhex("76a914363cc8d55ea8d0500de728ef6d63804ddddbdc9888ac")
        script = pbk.ScriptPubkey(data)
        assert script.to_bytes() == data


class TestTransactionOutput:
    def test_create(self):
        script = pbk.ScriptPubkey(b"\x51")
        out = pbk.TransactionOutput(script, 123456)
        assert out.amount == 123456
        assert out.script_pubkey.to_bytes() == b"\x51"


class TestBlock:
    def test_genesis_roundtrip(self):
        data = bytes.fromhex(GENESIS_BLOCK_HEX)
        block = pbk.Block(data)
        assert block.to_bytes() == data

    def test_genesis_hash(self):
        block = pbk.Block(bytes.fromhex(GENESIS_BLOCK_HEX))
        assert block.hash.hex() == GENESIS_HASH_HEX

    def test_transactions(self):
        block = pbk.Block(bytes.fromhex(GENESIS_BLOCK_HEX))
        assert len(block) == 1
        assert block[0].txid.hex() == GENESIS_TXID_HEX
        assert [tx.txid.hex() for tx in block] == [GENESIS_TXID_HEX]
        with pytest.raises(IndexError):
            block[1]

    def test_invalid_data_raises(self):
        with pytest.raises(pbk.KernelError):
            pbk.Block(b"junk")


class TestBlockHeader:
    def test_fields(self):
        header_data = bytes.fromhex(GENESIS_BLOCK_HEX)[:80]
        header = pbk.BlockHeader(header_data)
        assert header.version == 1
        assert header.timestamp == 1231006505
        assert header.bits == 0x1D00FFFF
        assert header.nonce == 2083236893
        assert header.hash.hex() == GENESIS_HASH_HEX
        assert header.prev_hash.to_bytes() == b"\x00" * 32

    def test_wrong_size_raises(self):
        with pytest.raises(pbk.KernelError):
            pbk.BlockHeader(b"\x00" * 79)


class TestBlockHash:
    def test_create_and_compare(self):
        raw = bytes.fromhex(GENESIS_HASH_HEX)[::-1]
        h1 = pbk.BlockHash(raw)
        h2 = pbk.BlockHash(raw)
        h3 = pbk.BlockHash(b"\x11" * 32)
        assert h1 == h2
        assert h1 != h3
        assert h1.to_bytes() == raw
        assert h1.hex() == GENESIS_HASH_HEX
        assert len({h1, h2}) == 1

    def test_wrong_size_raises(self):
        with pytest.raises(ValueError):
            pbk.BlockHash(b"\x00" * 31)
