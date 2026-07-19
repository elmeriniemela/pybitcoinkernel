#!/usr/bin/env python3
"""Spending coins and reading undo data (spent outputs).

When a block is connected, the kernel stores "undo data": every coin the
block's transactions consumed. read_block_spent_outputs() exposes it as
BlockSpentOutputs -> TransactionSpentOutputs -> Coin. Together with the
block itself this reconstructs the full input/output flow — the basis of
address indexers and chain analytics.

The example mines 100 blocks to mature a coinbase, spends it in block
101, and inspects that block's undo data.

Run:  python examples/05_spent_outputs.py
"""

import tempfile

import pybitcoinkernel as pbk

from _miner import COIN, OP_TRUE_SCRIPT, SUBSIDY, mine_block, mine_chain, ser_tx

COINBASE_MATURITY = 100


def main() -> None:
    pbk.logging_disable()

    with tempfile.TemporaryDirectory() as datadir:
        with pbk.load_chainstate(pbk.ChainType.REGTEST, datadir) as chainman:
            chain = chainman.get_active_chain()

            # Mine 100 blocks; block 1's coinbase becomes spendable at 101.
            blocks = mine_chain(chain[0].block_hash.to_bytes(), COINBASE_MATURITY)
            for raw in blocks:
                accepted, _ = chainman.process_block(pbk.Block(raw))
                assert accepted

            # Build a transaction spending block 1's coinbase. Its output
            # is a bare OP_TRUE, so an empty scriptSig satisfies it — no
            # signing needed for this demo.
            coinbase_1 = chainman.read_block(chain[1])[0]
            fee = 10_000
            spend_tx = ser_tx(
                inputs=[(coinbase_1.txid.to_bytes(), 0, b"")],
                outputs=[(SUBSIDY - fee, OP_TRUE_SCRIPT)],
            )
            print(f"spending coinbase {coinbase_1.txid.hex()[:16]}... from height 1")

            # Mine it into block 101.
            block_101 = mine_block(
                chain.tip().block_hash.to_bytes(),
                COINBASE_MATURITY + 1,
                extra_txs=[spend_tx],
            )
            accepted, _ = chainman.process_block(pbk.Block(block_101))
            assert accepted
            print(f"chain height now: {chain.height}")

            # Read the undo data of the tip block. There is one entry per
            # non-coinbase transaction, in block order.
            entry = chain.tip()
            spent = chainman.read_block_spent_outputs(entry)
            block = chainman.read_block(entry)
            print(f"non-coinbase txs with undo data: {len(spent)}")

            # Pair each transaction with the coins it consumed. Coins are
            # ordered like the transaction's inputs.
            for tx_index, tx_spent in enumerate(spent, start=1):
                tx = block[tx_index]
                print(f"tx {tx.txid.hex()[:16]}... consumed {len(tx_spent)} coin(s):")
                total_in = sum(coin.output.amount for coin in tx_spent)
                total_out = sum(out.amount for out in tx.outputs)
                for coin, tx_in in zip(tx_spent, tx.inputs):
                    point = tx_in.out_point
                    print(
                        f"  {point.txid.hex()[:16]}...:{point.index}"
                        f"  {coin.output.amount / COIN:.8f} BTC"
                        f"  created at height {coin.confirmation_height}"
                        f"  coinbase={coin.is_coinbase}"
                    )
                print(f"  fee paid: {total_in - total_out} sats")

            # Blocks with only a coinbase have no undo entries.
            assert len(chainman.read_block_spent_outputs(chain[1])) == 0
            print("empty blocks have no undo data: OK")


if __name__ == "__main__":
    main()
