#!/usr/bin/env python3
"""Validation events, kernel notifications, and log routing.

The kernel reports what happens during validation through two callback
interfaces registered on the Context:

  * validation interface — per-block validation results and chain events
    (block_checked, block_connected, block_disconnected, pow_valid_block)
  * notifications — node-level events (block_tip, header_tip, progress,
    warnings, fatal errors)

Handlers are plain Python objects; implement only the methods you need.
Callbacks may arrive from kernel worker threads — the bindings take care
of Python thread-state, so ordinary Python code just works.

Independently, LoggingConnection routes the kernel's internal log through
a Python callable.

Run:  python examples/06_callbacks_and_logging.py
"""

import tempfile

import pybitcoinkernel as pbk

from _miner import mine_block, mine_chain


class ValidationEvents:
    """Receives per-block validation outcomes."""

    def block_checked(self, block, state):
        mode = pbk.ValidationMode(state.mode)
        result = pbk.BlockValidationResult(state.result)
        line = f"  block_checked:   {block.hash.hex()[:16]}... {mode.name}"
        if mode != pbk.ValidationMode.VALID:
            line += f" ({result.name})"
        print(line)

    def block_connected(self, block, entry):
        print(f"  block_connected: height {entry.height}")


class NodeNotifications:
    """Receives node-level notifications."""

    def block_tip(self, state, entry, verification_progress):
        sync = pbk.SynchronizationState(state)
        print(f"  block_tip:       height {entry.height} ({sync.name})")

    def warning_set(self, warning, message):
        print(f"  warning:         {message}")

    def fatal_error(self, message):
        print(f"  FATAL:           {message}")


def main() -> None:
    # --- Log routing ----------------------------------------------------
    # Collect kernel log lines instead of printing them all; real
    # applications would forward these into the `logging` module.
    kernel_log: list[str] = []
    pbk.logging_set_options(log_timestamps=False)
    pbk.logging_enable_category(pbk.LogCategory.VALIDATION)
    pbk.logging_set_level_category(pbk.LogCategory.VALIDATION, pbk.LogLevel.DEBUG)
    connection = pbk.LoggingConnection(kernel_log.append)

    # --- Wire the handlers into a context --------------------------------
    options = pbk.ContextOptions()
    options.set_chainparams(pbk.ChainParameters(pbk.ChainType.REGTEST))
    options.set_validation_interface(ValidationEvents())
    options.set_notifications(NodeNotifications())
    context = pbk.Context(options)

    with tempfile.TemporaryDirectory() as datadir:
        with pbk.load_chainstate(
            pbk.ChainType.REGTEST, datadir, context=context
        ) as chainman:
            chain = chainman.get_active_chain()
            genesis_hash = chain[0].block_hash.to_bytes()

            print("processing 2 valid blocks:")
            for raw in mine_chain(genesis_hash, 2):
                chainman.process_block(pbk.Block(raw))

            print("processing 1 corrupted block:")
            bad = bytearray(mine_block(chain.tip().block_hash.to_bytes(), 3))
            bad[36] ^= 0xFF  # break the merkle root
            chainman.process_block(pbk.Block(bytes(bad)))

    connection.close()
    print(f"kernel produced {len(kernel_log)} log lines, e.g.:")
    for line in kernel_log[:3]:
        print(f"  {line.rstrip()}")


if __name__ == "__main__":
    main()
