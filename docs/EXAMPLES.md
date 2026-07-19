# PyBitcoinKernel by example

The [`examples/`](../examples) directory contains self-contained, runnable
scripts that walk through the library — and through the bitcoin kernel's
concepts — one topic at a time. Every snippet in this document comes from
one of those files, and every output shown is real.

## Prerequisites

Build and install the package first (see the [README](../README.md)),
then run any example from the project root:

```sh
.venv/bin/python examples/01_transactions.py
```

The chainstate examples (04–06) use regtest and mine their own blocks
with a tiny pure-Python miner ([`examples/_miner.py`](../examples/_miner.py))
— regtest proof-of-work is trivial, so they run offline in about a
second, in a temporary directory that is cleaned up afterwards.

| Example | Topic |
| --- | --- |
| [`01_transactions.py`](../examples/01_transactions.py) | Parsing and inspecting transactions |
| [`02_script_verification.py`](../examples/02_script_verification.py) | Consensus script verification (legacy, segwit, taproot) |
| [`03_blocks_and_headers.py`](../examples/03_blocks_and_headers.py) | Blocks, headers, and block hashes |
| [`04_chainstate_basics.py`](../examples/04_chainstate_basics.py) | Running a chainstate: validating blocks, reading chain data |
| [`05_spent_outputs.py`](../examples/05_spent_outputs.py) | Spending coins and reading undo data |
| [`06_callbacks_and_logging.py`](../examples/06_callbacks_and_logging.py) | Validation events, notifications, and log routing |

---

## 01 — Transactions

`Transaction` parses consensus-serialized bytes (the format used on the
P2P network and returned by `getrawtransaction`). Parsed transactions
expose their txid, inputs, and outputs; malformed bytes raise
`KernelError`.

```python
import pybitcoinkernel as pbk

tx = pbk.Transaction(raw_tx_bytes)

print(tx.txid.hex())            # display order, as block explorers show it
for tx_in in tx.inputs:         # or tx.input(i) / tx.n_inputs
    point = tx_in.out_point     # the output this input spends
    print(point.txid.hex(), point.index)
for tx_out in tx.outputs:       # or tx.output(i) / tx.n_outputs
    print(tx_out.amount, tx_out.script_pubkey.to_bytes().hex())

assert tx.to_bytes() == raw_tx_bytes   # lossless round-trip
```

```console
$ .venv/bin/python examples/01_transactions.py
txid:      e37a5907ac519806758fff8137f8d988fba9101c9dc490a95fed4a230215e6ba
inputs:    2
outputs:   2
input 0:   spends 5da625035ef3e04460026303a0efeb598e629842e26c1933701c37fd663ec048:0
input 1:   spends d2123251b6d4f9d3c6f32ab474a2fbd649708018a82c9ee3843c4c6e8c6756b8:0
output 0:  775520 sats  script=76a914363cc8d55ea8d0500de728ef6d63804ddddbdc9888ac
output 1:  984167 sats  script=76a914c303bdc5064bf9c9a8b507b5496bd0987285707988ac
serialization round-trip: OK
parsing garbage raises: KernelError(failed to deserialize transaction)
```

Hashes (`Txid`, `BlockHash`) compare by value, are hashable, and expose
both byte orders: `.to_bytes()` returns the internal order used in the
wire format, `.hex()` returns the reversed display order.

## 02 — Script verification

`verify_script()` runs Bitcoin Core's actual script interpreter to answer:
*does input N of this transaction validly spend this script pubkey?*
The rules differ by script era:

- **Legacy** — the spent amount is not signed; pass `0`.
- **Segwit** — signatures commit to the spent output's amount; pass the
  real amount or verification fails.
- **Taproot** — signatures commit to *all* outputs spent by the
  transaction, so you must supply `PrecomputedTransactionData` built
  with them. Forgetting it raises `ValueError` rather than returning
  `False`, because the answer isn't "invalid" — the question is
  malformed.

```python
flags = pbk.ScriptVerificationFlags

# Legacy / segwit
ok = pbk.verify_script(script, amount, tx, input_index, flags.ALL ^ flags.TAPROOT)

# Taproot
spent_outputs = [pbk.TransactionOutput(script, amount)]   # one per tx input
precomputed = pbk.PrecomputedTransactionData(tx, spent_outputs)
ok = pbk.verify_script(script, amount, tx, 0, flags.ALL, precomputed)
```

```console
$ .venv/bin/python examples/02_script_verification.py
legacy P2PKH spend valid:   True
spend of the wrong script:  False
segwit multisig valid:      True
...with a wrong amount:     False
taproot key-path valid:     True
taproot without precomputed data raises: ValueError(taproot verification requires precomputed transaction data with spent outputs)
```

All vectors are real mainnet transactions, the same ones Bitcoin Core's
kernel test suite uses.

## 03 — Blocks and headers

`Block` parses a serialized block and behaves like a sequence of
transactions (`len(block)`, `block[i]`, iteration). An 80-byte header can
be parsed standalone with `BlockHeader`. The example dissects the mainnet
genesis block, newspaper headline included:

```python
block = pbk.Block(raw_block_bytes)
print(block.hash.hex(), len(block))
coinbase = block[0]

header = pbk.BlockHeader(raw_block_bytes[:80])
header.version, header.timestamp, header.bits, header.nonce, header.prev_hash
```

```console
$ .venv/bin/python examples/03_blocks_and_headers.py
block hash:  000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f
tx count:    1
coinbase:    4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b
message:     'The Times 03/Jan/2009 Chancellor on brink of second bailout for banks'
version:     1
timestamp:   1231006505 (2009-01-03 18:15:05 UTC)
bits:        0x1d00ffff
nonce:       2083236893
prev hash:   0000000000000000000000000000000000000000000000000000000000000000
hash equal:  True
```

## 04 — Running a chainstate

`ChainstateManager` is the heart of the kernel: it maintains the block
index and UTXO set in a data directory, fully validates blocks handed to
`process_block()` (proof of work, merkle root, coinbase rules, every
script), and serves reads of stored blocks. `load_chainstate()` bundles
the context/options/manager setup into one call, and the manager works
as a context manager — leaving the `with` block flushes to disk.

```python
with pbk.load_chainstate(pbk.ChainType.REGTEST, datadir) as chainman:
    chain = chainman.get_active_chain()     # live view of the best chain
    genesis = chain[0]                      # BlockTreeEntry by height
    tip = chain.tip()                       # == chain[-1]

    accepted, is_new = chainman.process_block(pbk.Block(raw))

    for entry in chain:                     # iterate genesis -> tip
        block = chainman.read_block(entry)  # full block from disk

    entry = chainman.get_block_tree_entry_by_hash(some_block_hash)
    parent = entry.prev                     # walk toward genesis; None at height 0
```

`process_block()` returns `(accepted, is_new)`: a duplicate of a known
block is `accepted=True, is_new=False`; an invalid block is
`accepted=False` (example: corrupted merkle root). The data directory
persists — reopening it resumes at the same height:

```console
$ .venv/bin/python examples/04_chainstate_basics.py
fresh chainstate: height=0
genesis:          0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206
after mining:     height=5
duplicate block:  accepted=True is_new=False
corrupted block:  accepted=False
  height 0: 1 tx, hash 0f9188f13cb7b2c7...
  height 1: 1 tx, hash 62c7c507e775676e...
  height 2: 1 tx, hash 58a3253bdd340d8a...
  height 3: 1 tx, hash 62f51a9b37e00b99...
  height 4: 1 tx, hash 46a9c836e09b1d12...
  height 5: 1 tx, hash 18afe84438c8cb6a...
tip -> genesis:   6 entries
lookup by hash:   found height 3
reopened:         height=5
```

`Chain` and `BlockTreeEntry` are live views into the manager; they keep
it alive, and raise `ValueError` if you `close()` it explicitly.

## 05 — Spent outputs (undo data)

When a block connects, the kernel stores every coin the block consumed —
the "undo data" needed for reorgs. `read_block_spent_outputs()` exposes
it as nested sequences: one `TransactionSpentOutputs` per non-coinbase
transaction (in block order), containing one `Coin` per input (in input
order). Combined with the block itself, this reconstructs the complete
money flow — the foundation of address indexers and analytics tools.

```python
spent = chainman.read_block_spent_outputs(entry)   # BlockSpentOutputs
block = chainman.read_block(entry)

for tx_index, tx_spent in enumerate(spent, start=1):   # skips the coinbase
    tx = block[tx_index]
    for coin, tx_in in zip(tx_spent, tx.inputs):
        coin.output.amount          # value of the consumed output
        coin.output.script_pubkey   # ...and its script
        coin.confirmation_height    # block that created the coin
        coin.is_coinbase            # was it a coinbase output?
```

The example mines 100 blocks to mature a coinbase, spends it in block
101, and inspects that block's undo data (fee = inputs − outputs):

```console
$ .venv/bin/python examples/05_spent_outputs.py
spending coinbase b2060669568c6f48... from height 1
chain height now: 101
non-coinbase txs with undo data: 1
tx 97161db1b8b7031c... consumed 1 coin(s):
  b2060669568c6f48...:0  50.00000000 BTC  created at height 1  coinbase=True
  fee paid: 10000 sats
empty blocks have no undo data: OK
```

## 06 — Callbacks and logging

The kernel reports validation progress through two callback interfaces
registered on the `Context`. Handlers are plain Python objects —
implement only the methods you care about:

```python
class ValidationEvents:
    def block_checked(self, block, state): ...      # every validation verdict
    def block_connected(self, block, entry): ...    # block joined the best chain
    def block_disconnected(self, block, entry): ... # reorg
    def pow_valid_block(self, block, entry): ...

class NodeNotifications:
    def block_tip(self, state, entry, verification_progress): ...
    def header_tip(self, state, height, timestamp, presync): ...
    def progress(self, title, percent, resume_possible): ...
    def warning_set(self, warning, message): ...
    def warning_unset(self, warning): ...
    def flush_error(self, message): ...
    def fatal_error(self, message): ...

options = pbk.ContextOptions()
options.set_chainparams(pbk.ChainParameters(pbk.ChainType.REGTEST))
options.set_validation_interface(ValidationEvents())
options.set_notifications(NodeNotifications())
context = pbk.Context(options)
chainman = pbk.load_chainstate(pbk.ChainType.REGTEST, datadir, context=context)
```

Callbacks may arrive from kernel worker threads; the bindings handle
Python thread-state, so ordinary Python code just works. Exceptions
raised inside a handler are reported (like `sys.unraisablehook`) but do
not abort validation.

Separately, `LoggingConnection` routes the kernel's internal log through
any Python callable, with global category/level/format controls:

```python
pbk.logging_set_options(log_timestamps=False)
pbk.logging_enable_category(pbk.LogCategory.VALIDATION)
pbk.logging_set_level_category(pbk.LogCategory.VALIDATION, pbk.LogLevel.DEBUG)
connection = pbk.LoggingConnection(my_logger.info)   # keep a reference!
```

```console
$ .venv/bin/python examples/06_callbacks_and_logging.py
  block_checked:   0f9188f13cb7b2c7... VALID
  block_connected: height 0
  block_tip:       height 0 (INIT_DOWNLOAD)
processing 2 valid blocks:
  block_checked:   62c7c507e775676e... VALID
  block_connected: height 1
  block_tip:       height 1 (INIT_DOWNLOAD)
  block_checked:   58a3253bdd340d8a... VALID
  block_connected: height 2
  block_tip:       height 2 (INIT_DOWNLOAD)
processing 1 corrupted block:
  block_checked:   6786ade4af78d865... INVALID (MUTATED)
kernel produced 48 log lines, e.g.:
  Using the 'x86_shani(1way;2way)' SHA256 implementation
  Using RdSeed as an additional entropy source
  Using RdRand as an additional entropy source
```

Note the first three lines: they fire while the chainstate manager is
being created, when the kernel loads (here: creates) the genesis block —
before the script processes anything itself. The invalid block's verdict
carries a granular reason (`BlockValidationResult.MUTATED`: the block's
contents don't match the merkle root committed to by the header).

Two logging gotchas, straight from the kernel's own documentation:

- Keep the `LoggingConnection` object alive; letting it be
  garbage-collected disconnects the callback.
- `pbk.logging_disable()` permanently disables kernel logging for the
  process (examples 04/05 use it to keep their output clean). Don't call
  it while a connection exists.
