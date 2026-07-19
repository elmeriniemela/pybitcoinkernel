"""Integration tests running a regtest chainstate manager and processing
locally-mined blocks through full validation."""

import pytest

import pybitcoinkernel as pbk

from util import (
    OP_TRUE_SCRIPT,
    SUBSIDY,
    dsha256,
    mine_block,
    mine_chain,
    ser_tx,
)

# Coinbase outputs need 100 confirmations before they can be spent.
COINBASE_MATURITY = 100

REGTEST_GENESIS_HASH_HEX = (
    "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
)


@pytest.fixture
def chainman(tmp_path):
    manager = pbk.load_chainstate(pbk.ChainType.REGTEST, tmp_path / "data")
    yield manager
    manager.close()


def test_genesis_only(chainman):
    chain = chainman.get_active_chain()
    assert chain.height == 0
    assert len(chain) == 1
    genesis = chain[0]
    assert genesis.height == 0
    assert genesis.block_hash.hex() == REGTEST_GENESIS_HASH_HEX
    assert genesis.prev is None
    assert chainman.get_best_entry() == genesis


def test_process_blocks(chainman):
    chain = chainman.get_active_chain()
    genesis_hash = chain[0].block_hash.to_bytes()
    blocks = mine_chain(genesis_hash, 5)
    for raw in blocks:
        accepted, new = chainman.process_block(pbk.Block(raw))
        assert accepted
        assert new

    assert chain.height == 5
    tip = chain.tip()
    assert tip.height == 5
    assert tip.block_hash.to_bytes() == dsha256(blocks[-1][:80])

    # Duplicate block: accepted but not new.
    accepted, new = chainman.process_block(pbk.Block(blocks[-1]))
    assert accepted
    assert not new

    # Walk the chain backwards via prev.
    entry = tip
    for expected_height in range(5, -1, -1):
        assert entry.height == expected_height
        assert entry in chain
        entry = entry.prev
    assert entry is None


def test_invalid_block_rejected(chainman):
    chain = chainman.get_active_chain()
    genesis_hash = chain[0].block_hash.to_bytes()
    raw = bytearray(mine_block(genesis_hash, 1))
    raw[36] ^= 0xFF  # corrupt the merkle root
    accepted, _new = chainman.process_block(pbk.Block(bytes(raw)))
    assert not accepted
    assert chain.height == 0


def test_lookup_and_read_block(chainman):
    chain = chainman.get_active_chain()
    genesis_hash = chain[0].block_hash.to_bytes()
    blocks = mine_chain(genesis_hash, 3)
    for raw in blocks:
        accepted, _ = chainman.process_block(pbk.Block(raw))
        assert accepted

    block_hash = pbk.BlockHash(dsha256(blocks[1][:80]))
    entry = chainman.get_block_tree_entry_by_hash(block_hash)
    assert entry is not None
    assert entry.height == 2
    assert entry.header.prev_hash.to_bytes() == dsha256(blocks[0][:80])

    block = chainman.read_block(entry)
    assert block.to_bytes() == blocks[1]

    unknown = chainman.get_block_tree_entry_by_hash(pbk.BlockHash(b"\x42" * 32))
    assert unknown is None


def test_spend_coinbase_and_read_spent_outputs(chainman):
    chain = chainman.get_active_chain()
    genesis_hash = chain[0].block_hash.to_bytes()

    blocks = mine_chain(genesis_hash, COINBASE_MATURITY)
    for raw in blocks:
        accepted, _ = chainman.process_block(pbk.Block(raw))
        assert accepted

    # Spend the coinbase of block 1 (an OP_TRUE output, so an empty
    # scriptSig satisfies it) in a block at height 101.
    coinbase_1 = pbk.Block(blocks[0])[0]
    fee = 10_000
    spend_tx = ser_tx(
        inputs=[(coinbase_1.txid.to_bytes(), 0, b"")],
        outputs=[(SUBSIDY - fee, OP_TRUE_SCRIPT)],
    )
    tip_hash = dsha256(blocks[-1][:80])
    spend_block = mine_block(tip_hash, COINBASE_MATURITY + 1, extra_txs=[spend_tx])
    accepted, new = chainman.process_block(pbk.Block(spend_block))
    assert accepted and new
    assert chain.height == COINBASE_MATURITY + 1

    entry = chain.tip()
    spent_outputs = chainman.read_block_spent_outputs(entry)
    # One non-coinbase transaction in the block...
    assert len(spent_outputs) == 1
    tx_spent = spent_outputs[0]
    # ...which consumed exactly one coin:
    assert len(tx_spent) == 1
    coin = tx_spent[0]
    assert coin.is_coinbase
    assert coin.confirmation_height == 1
    assert coin.output.amount == SUBSIDY
    assert coin.output.script_pubkey.to_bytes() == OP_TRUE_SCRIPT

    # Empty blocks have no spent outputs.
    assert len(chainman.read_block_spent_outputs(chain[1])) == 0


def test_process_block_header(chainman):
    chain = chainman.get_active_chain()
    genesis_hash = chain[0].block_hash.to_bytes()
    raw = mine_block(genesis_hash, 1)
    header = pbk.BlockHeader(raw[:80])
    accepted, state = chainman.process_block_header(header)
    assert accepted
    assert state.mode == pbk.ValidationMode.VALID
    # Header-only processing does not extend the chain.
    assert chain.height == 0
    entry = chainman.get_block_tree_entry_by_hash(pbk.BlockHash(dsha256(raw[:80])))
    assert entry is not None
    assert entry.height == 1


def test_validation_interface_callbacks(tmp_path):
    events = []

    class Handler:
        def block_checked(self, block, state):
            events.append(("checked", block.hash.hex(), state.mode))

        def block_connected(self, block, entry):
            events.append(("connected", block.hash.hex(), entry.height))

    class Notifications:
        def block_tip(self, state, entry, progress):
            events.append(("tip", entry.height))

    options = pbk.ContextOptions()
    options.set_chainparams(pbk.ChainParameters(pbk.ChainType.REGTEST))
    options.set_validation_interface(Handler())
    options.set_notifications(Notifications())
    context = pbk.Context(options)

    with pbk.load_chainstate(
        pbk.ChainType.REGTEST, tmp_path / "data", context=context
    ) as chainman:
        chain = chainman.get_active_chain()
        genesis_hash = chain[0].block_hash.to_bytes()
        raw = mine_block(genesis_hash, 1)
        block_hash_hex = pbk.Block(raw).hash.hex()
        accepted, _ = chainman.process_block(pbk.Block(raw))
        assert accepted

    checked = [e for e in events if e[0] == "checked"]
    connected = [e for e in events if e[0] == "connected"]
    tips = [e for e in events if e[0] == "tip"]
    assert ("checked", block_hash_hex, int(pbk.ValidationMode.VALID)) in checked
    assert ("connected", block_hash_hex, 1) in connected
    assert ("tip", 1) in tips


def test_reopen_persists_chain(tmp_path):
    data_dir = tmp_path / "data"
    with pbk.load_chainstate(pbk.ChainType.REGTEST, data_dir) as chainman:
        chain = chainman.get_active_chain()
        genesis_hash = chain[0].block_hash.to_bytes()
        for raw in mine_chain(genesis_hash, 3):
            accepted, _ = chainman.process_block(pbk.Block(raw))
            assert accepted
        tip_hash = chain.tip().block_hash

    with pbk.load_chainstate(pbk.ChainType.REGTEST, data_dir) as chainman:
        chain = chainman.get_active_chain()
        assert chain.height == 3
        assert chain.tip().block_hash == tip_hash


def test_closed_chainman_raises(tmp_path):
    chainman = pbk.load_chainstate(pbk.ChainType.REGTEST, tmp_path / "data")
    chainman.close()
    with pytest.raises(ValueError):
        chainman.get_active_chain()
    # close() is idempotent
    chainman.close()


def test_stale_views_raise_after_close(tmp_path):
    chainman = pbk.load_chainstate(pbk.ChainType.REGTEST, tmp_path / "data")
    chain = chainman.get_active_chain()
    entry = chain.tip()
    chainman.close()
    with pytest.raises(ValueError):
        chain.height
    with pytest.raises(ValueError):
        len(chain)
    with pytest.raises(ValueError):
        chain[0]
    with pytest.raises(ValueError):
        entry.height
    with pytest.raises(ValueError):
        entry.block_hash
