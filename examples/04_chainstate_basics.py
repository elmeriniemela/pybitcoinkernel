#!/usr/bin/env python3
"""Running a chainstate: validating blocks and reading chain data.

A ChainstateManager is the central kernel object. It maintains the block
index and UTXO set in a data directory, fully validates blocks handed to
process_block(), and serves reads of previously stored blocks.

This example uses regtest and mines its own (trivial-PoW) blocks with the
pure-Python miner in _miner.py, so it runs offline in under a second.

Run:  python examples/04_chainstate_basics.py
"""

import tempfile

import pybitcoinkernel as pbk

from _miner import dsha256, mine_block, mine_chain


def main() -> None:
    # Kernel logging is chatty by default; keep the example output clean.
    pbk.logging_disable()

    with tempfile.TemporaryDirectory() as datadir:
        # load_chainstate() creates the context, options, and manager in
        # one call. It is a context manager: leaving the block flushes
        # state to disk and tears the manager down.
        with pbk.load_chainstate(pbk.ChainType.REGTEST, datadir) as chainman:
            # The active chain is a live view: chain[height] -> BlockTreeEntry.
            chain = chainman.get_active_chain()
            genesis = chain[0]
            print(f"fresh chainstate: height={chain.height}")
            print(f"genesis:          {genesis.block_hash.hex()}")

            # Mine and process 5 blocks. process_block() returns
            # (accepted, is_new) — it fully validates: PoW, merkle root,
            # coinbase rules, scripts, the lot.
            blocks = mine_chain(genesis.block_hash.to_bytes(), 5)
            for raw in blocks:
                accepted, is_new = chainman.process_block(pbk.Block(raw))
                assert accepted and is_new
            print(f"after mining:     height={chain.height}")

            # Feeding the same block again is fine, but it is not new.
            accepted, is_new = chainman.process_block(pbk.Block(blocks[-1]))
            print(f"duplicate block:  accepted={accepted} is_new={is_new}")

            # An invalid block (here: corrupted merkle root) is rejected.
            bad = bytearray(mine_block(chain.tip().block_hash.to_bytes(), 6))
            bad[36] ^= 0xFF
            accepted, _ = chainman.process_block(pbk.Block(bytes(bad)))
            print(f"corrupted block:  accepted={accepted}")

            # Iterate the chain and read blocks back from disk.
            for entry in chain:
                block = chainman.read_block(entry)
                print(
                    f"  height {entry.height}: {len(block)} tx,"
                    f" hash {entry.block_hash.hex()[:16]}..."
                )

            # Entries link backwards; walk from the tip to genesis.
            entry = chainman.get_best_entry()
            hops = 0
            while entry is not None:
                entry = entry.prev
                hops += 1
            print(f"tip -> genesis:   {hops} entries")

            # Look up an entry by block hash.
            wanted = pbk.BlockHash(dsha256(blocks[2][:80]))
            entry = chainman.get_block_tree_entry_by_hash(wanted)
            print(f"lookup by hash:   found height {entry.height}")

            tip_before = chain.tip().block_hash

        # The data directory persists: reopening resumes where we left off.
        with pbk.load_chainstate(pbk.ChainType.REGTEST, datadir) as chainman:
            chain = chainman.get_active_chain()
            print(f"reopened:         height={chain.height}")
            assert chain.tip().block_hash == tip_before


if __name__ == "__main__":
    main()
