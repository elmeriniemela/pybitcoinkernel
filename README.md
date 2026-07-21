# PyBitcoinKernel

A library that provides CPython bindings for the bitcoin kernel.

* Bitcoin Kernel: https://thecharlatan.ch/kernel-docs/
* Python/C API reference manual: https://docs.python.org/3/c-api/index.html

The bindings are implemented as a CPython extension module
(`src/_bitcoinkernel.c`) written against the Python/C API, wrapping the
kernel C API declared in `bitcoinkernel.h`. The `pybitcoinkernel` package
adds enums and convenience helpers on top.

## Installing

### From source, one command

```sh
pip install git+https://github.com/elmeriniemela/pybitcoinkernel.git
```

pip checks out the pinned Bitcoin Core submodule (`external/bitcoin`,
fetched shallowly), compiles `libbitcoinkernel` from it with cmake, and
bundles the library into the installed package. This builds Bitcoin
Core's kernel from scratch, so it takes several minutes and requires:

* cmake >= 3.22
* a C++20 compiler
* Boost headers (`boost-devel` / `libboost-dev`)

Run pip with `-v` to watch the compilation progress.

### From prebuilt wheels

Wheels are built by CI (`.github/workflows/wheels.yml`) for Linux
x86_64 and aarch64 (CPython 3.12-3.14, glibc >= 2.34: Ubuntu 22.04+,
Debian 12+, Fedora 35+) and attached to [GitHub
releases](https://github.com/elmeriniemela/pybitcoinkernel/releases).
They are fully self-contained - `libbitcoinkernel` is bundled inside, so
the target machine needs nothing but Python. Pick the wheel matching
your Python version and architecture, e.g. for Python 3.12 on x86_64:

```sh
pip install https://github.com/elmeriniemela/pybitcoinkernel/releases/download/v0.1.0/pybitcoinkernel-0.1.0-cp312-cp312-manylinux_2_34_x86_64.whl
```

### Against an existing kernel build

If you already have `libbitcoinkernel`, skip the source build entirely:

```sh
BITCOINKERNEL_INCLUDE_DIR=/path/to/include \
BITCOINKERNEL_LIB_DIR=/path/to/lib \
    pip install git+https://github.com/elmeriniemela/pybitcoinkernel.git
```

`BITCOINKERNEL_INCLUDE_DIR` must contain `bitcoinkernel.h` and
`BITCOINKERNEL_LIB_DIR` must contain `libbitcoinkernel.so`. The library
path is baked into the extension as an rpath, so the `.so` must stay in
place after installation. Alternatively, set `BITCOINKERNEL_SOURCE_DIR`
to any Bitcoin Core source tree to compile and bundle the kernel from it
instead of the submodule.

## Developing

For iterating on the bindings, build the kernel once into the
project-local `vendor/build` dir so reinstalling the package is fast.
`setup.py` links against it directly and takes `bitcoinkernel.h` from
the source tree recorded in the build's cmake cache, so the header and
library always match. `vendor/` is gitignored, so `git clean -fdx`
removes the whole thing.

```sh
# 0. Fetch the pinned bitcoin sources
git submodule update --init

# 1. Build libbitcoinkernel as a shared library
cmake -S external/bitcoin -B vendor/build \
    -DBUILD_KERNEL_LIB=ON -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_DAEMON=OFF -DBUILD_CLI=OFF -DBUILD_TX=OFF -DBUILD_UTIL=OFF \
    -DBUILD_TESTS=OFF -DBUILD_BENCH=OFF -DENABLE_WALLET=OFF -DWITH_ZMQ=OFF \
    -DENABLE_IPC=OFF -DENABLE_SCRIPT_TRACE=ON
cmake --build vendor/build --target bitcoinkernel -j$(nproc)

# 2. Build and install the package (setup.py picks vendor/build up)
python -m venv .venv
.venv/bin/pip install -e ".[test]"

# 3. Run the tests
.venv/bin/python -m pytest
```

After moving the submodule to a different commit, re-run both cmake
commands (and reinstall the package) so the library, header, and
extension stay in sync.

## Releasing wheels

The `wheels` workflow builds wheels for all supported platforms
(compiling the kernel from the `external/bitcoin` submodule inside
manylinux containers) and runs the full test suite against each wheel
before accepting it.

To cut a release:

```sh
# 1. Bump `version` in pyproject.toml, commit, push.

# 2. Build the wheels
gh workflow run wheels.yml && sleep 5 && gh run watch

# 3. Download the wheels from the finished run
gh run download --name wheels-ubuntu-24.04 --name wheels-ubuntu-24.04-arm --dir wheelhouse

# 4. Attach them to a GitHub release (creates the tag)
gh release create v0.1.0 wheelhouse/**/*.whl --title v0.1.0 --notes "..."
```

The install URL in [From prebuilt wheels](#from-prebuilt-wheels) follows
from the tag and wheel filename.

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

### Script debugging

`debug_script()` takes the same arguments as `verify_script()` but also
captures a step-by-step trace of the script interpreter — the stack before
every opcode, for the scriptSig, scriptPubkey, and any witness script:

```python
trace = pbk.debug_script(script, amount, tx_to, input_index, flags)
print(trace)                       # btcdeb-style, opcode-by-opcode dump
print(trace.valid, trace.error)    # verdict + ScriptError
for execution in trace.executions: # one per script the interpreter ran
    for step in execution.steps:
        print(pbk.opcode_name(step.opcode), [b.hex() for b in step.stack])
```

`verify_script()` / `debug_script()` verify **one input** (one script
pubkey against one `input_index`) — that is the kernel's primitive. A
transaction's scripts pass only if every input does, so pass one
`TransactionOutput` per input (the coin it spends, in order) to the
whole-transaction helpers:

```python
spent = [pbk.TransactionOutput(script_i, amount_i) for ...]   # one per input
ok = pbk.verify_transaction(tx, spent)             # True iff every input verifies
traces = pbk.debug_transaction(tx, spent)          # one ScriptTrace per input
```

Note this is per-input *script* verification looped over the inputs, not
full consensus validation — amounts, double-spends, weight and coinbase
maturity are enforced only by `ChainstateManager.process_block()` against a
chainstate.

To trace scripts as they run elsewhere (e.g. inside `process_block`),
install your own frame callback with the `script_trace` context manager:

```python
with pbk.script_trace(lambda frame: print(repr(frame))):
    pbk.verify_script(script, amount, tx_to, input_index, flags)
```

The formatted trace annotates each script block with its role (input /
output / witness or redeem script) and recognised type (`P2PKH`, `P2WPKH`,
`P2TR`, …), and notes when a witness script's stack is seeded from the
input witness. `pbk.disassemble()` decodes raw script bytes to opcodes and
`pbk.classify_script()` returns a script's standard type; both are pure
decoders that need no special build.

The trace hooks are compiled in only when libbitcoinkernel is built with
`-DENABLE_SCRIPT_TRACE=ON` (the bundled build does this automatically);
`pbk.trace_available()` reports whether they are present.

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
