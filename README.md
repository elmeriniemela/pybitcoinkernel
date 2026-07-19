# PyBitcoinKernel

A library that provides CPython bindings for the bitcoin kernel.

* Bitcoin Kernel: https://thecharlatan.ch/kernel-docs/
* Python/C API reference manual: https://docs.python.org/3/c-api/index.html

The bindings are implemented as a CPython extension module
(`src/_bitcoinkernel.c`) written against the Python/C API, wrapping the
kernel C API declared in `bitcoinkernel.h`. The `pybitcoinkernel` package
adds enums and convenience helpers on top.

## Building

The extension links against `libbitcoinkernel`, which is not yet shipped
in Bitcoin Core releases and must be built from source:

```sh
# 1. Build libbitcoinkernel as a shared library from a bitcoin checkout
cmake -S ../bitcoin -B /tmp/btck-build \
    -DBUILD_KERNEL_LIB=ON -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_DAEMON=OFF -DBUILD_CLI=OFF -DBUILD_TX=OFF -DBUILD_UTIL=OFF \
    -DBUILD_TESTS=OFF -DBUILD_BENCH=OFF -DENABLE_WALLET=OFF -DWITH_ZMQ=OFF
cmake --build /tmp/btck-build --target bitcoinkernel -j$(nproc)

# 2. Drop the artifacts into the project-local vendor prefix
mkdir -p vendor/lib vendor/include
cp /tmp/btck-build/lib/libbitcoinkernel.so vendor/lib/
cp ../bitcoin/src/kernel/bitcoinkernel.h vendor/include/

# 3. Build and install the package
python -m venv .venv
.venv/bin/pip install -e ".[test]"

# 4. Run the tests
.venv/bin/python -m pytest
```

To link against a kernel library installed elsewhere, set
`BITCOINKERNEL_INCLUDE_DIR` and `BITCOINKERNEL_LIB_DIR` when installing.

## Usage

For a guided tour with runnable scripts, see
[docs/EXAMPLES.md](docs/EXAMPLES.md) and the [examples/](examples)
directory.

### Script verification

```python
import pybitcoinkernel as pbk

script = pbk.ScriptPubkey(bytes.fromhex("76a9144bfbaf6afb76cc5771bc6404810d1cc041a6933988ac"))
tx = pbk.Transaction(bytes.fromhex("0200000001..."))
flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
ok = pbk.verify_script(script, amount=0, tx_to=tx, input_index=0, flags=flags)
```

Taproot verification requires precomputed transaction data carrying the
spent outputs:

```python
spent = pbk.TransactionOutput(script, amount)
precomputed = pbk.PrecomputedTransactionData(tx, [spent])
ok = pbk.verify_script(script, amount, tx, 0, pbk.ScriptVerificationFlags.ALL, precomputed)
```

### Running a chainstate

```python
import pybitcoinkernel as pbk

with pbk.load_chainstate(pbk.ChainType.REGTEST, "/path/to/datadir") as chainman:
    chain = chainman.get_active_chain()
    print("height:", chain.height)

    accepted, is_new = chainman.process_block(pbk.Block(raw_block_bytes))

    for entry in chain:                      # BlockTreeEntry per height
        block = chainman.read_block(entry)   # full block from disk
        for tx in block:
            print(tx.txid.hex())

    # Undo data: the coins spent by each transaction of a block
    spent = chainman.read_block_spent_outputs(chain.tip())
```

### Validation and notification callbacks

Handlers are plain objects; implement only the methods you care about:

```python
class Handler:
    def block_checked(self, block, state):
        print(block.hash.hex(), pbk.ValidationMode(state.mode))

    def block_connected(self, block, entry):
        print("connected at height", entry.height)

options = pbk.ContextOptions()
options.set_chainparams(pbk.ChainParameters(pbk.ChainType.REGTEST))
options.set_validation_interface(Handler())
context = pbk.Context(options)
chainman = pbk.load_chainstate(pbk.ChainType.REGTEST, datadir, context=context)
```

### Logging

```python
conn = pbk.LoggingConnection(print)
pbk.logging_enable_category(pbk.LogCategory.VALIDATION)
pbk.logging_set_level_category(pbk.LogCategory.ALL, pbk.LogLevel.DEBUG)
```

## Notes

* Every wrapper object owns its kernel handle; memory is managed
  automatically. `Chain` and `BlockTreeEntry` are views tied to their
  `ChainstateManager` — they keep it alive, and raise `ValueError` if it
  was explicitly `close()`d.
* Long-running calls (block processing, imports, disk reads, chainstate
  construction/teardown) release the GIL; kernel callbacks may arrive
  from kernel threads and are safe to handle in Python.
* The kernel API is unversioned and not yet stable; these bindings track
  the header in `vendor/include/bitcoinkernel.h`.
