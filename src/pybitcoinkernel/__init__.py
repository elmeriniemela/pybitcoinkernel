"""Python bindings for Bitcoin Core's libbitcoinkernel.

This package wraps the bitcoin kernel C API (bitcoinkernel.h) with a
CPython extension module and adds Python-friendly enums and helpers.

Quick start::

    import pybitcoinkernel as pbk

    # Verify a script
    script = pbk.ScriptPubkey(bytes.fromhex("76a914..."))
    tx = pbk.Transaction(bytes.fromhex("0200..."))
    script.verify(amount=0, tx_to=tx, input_index=0)

    # Run a chainstate (regtest)
    chainman = pbk.load_chainstate(pbk.ChainType.REGTEST, "/tmp/datadir")
    with chainman:
        chain = chainman.get_active_chain()
        print(chain.height)
"""

import enum as _enum
import os as _os

from pybitcoinkernel import _bitcoinkernel as _core
from pybitcoinkernel._bitcoinkernel import (
    Block,
    BlockHash,
    BlockHeader,
    BlockSpentOutputs,
    BlockTreeEntry,
    BlockValidationState,
    Chain,
    ChainParameters,
    ChainstateManager,
    ChainstateManagerOptions,
    Coin,
    Context,
    ContextOptions,
    KernelError,
    LoggingConnection,
    PrecomputedTransactionData,
    ScriptPubkey,
    ScriptTraceFrame,
    Transaction,
    TransactionInput,
    TransactionOutPoint,
    TransactionOutput,
    TransactionSpentOutputs,
    Txid,
    logging_disable,
    logging_disable_category,
    logging_enable_category,
    logging_set_level_category,
    logging_set_options,
)
from pybitcoinkernel.debugger import (
    ScriptError,
    ScriptExecution,
    ScriptTrace,
    ScriptTraceFrameKind,
    SigVersion,
    debug_script,
    disassemble,
    opcode_description,
    opcode_name,
    script_trace,
    trace_available,
)

__version__ = "0.1.0"


class ChainType(_enum.IntEnum):
    """The supported chain networks."""

    MAINNET = _core.CHAIN_TYPE_MAINNET
    TESTNET = _core.CHAIN_TYPE_TESTNET
    TESTNET_4 = _core.CHAIN_TYPE_TESTNET_4
    SIGNET = _core.CHAIN_TYPE_SIGNET
    REGTEST = _core.CHAIN_TYPE_REGTEST


class ScriptVerificationFlags(_enum.IntFlag):
    """Script verification flags that may be combined with ``|``."""

    NONE = _core.SCRIPT_FLAGS_VERIFY_NONE
    P2SH = _core.SCRIPT_FLAGS_VERIFY_P2SH
    DERSIG = _core.SCRIPT_FLAGS_VERIFY_DERSIG
    NULLDUMMY = _core.SCRIPT_FLAGS_VERIFY_NULLDUMMY
    CHECKLOCKTIMEVERIFY = _core.SCRIPT_FLAGS_VERIFY_CHECKLOCKTIMEVERIFY
    CHECKSEQUENCEVERIFY = _core.SCRIPT_FLAGS_VERIFY_CHECKSEQUENCEVERIFY
    WITNESS = _core.SCRIPT_FLAGS_VERIFY_WITNESS
    TAPROOT = _core.SCRIPT_FLAGS_VERIFY_TAPROOT
    ALL = _core.SCRIPT_FLAGS_VERIFY_ALL


class ValidationMode(_enum.IntEnum):
    """Whether a validated structure is valid, invalid, or errored."""

    VALID = _core.VALIDATION_MODE_VALID
    INVALID = _core.VALIDATION_MODE_INVALID
    INTERNAL_ERROR = _core.VALIDATION_MODE_INTERNAL_ERROR


class BlockValidationResult(_enum.IntEnum):
    """A granular reason why a block was invalid."""

    UNSET = _core.BLOCK_VALIDATION_RESULT_UNSET
    CONSENSUS = _core.BLOCK_VALIDATION_RESULT_CONSENSUS
    CACHED_INVALID = _core.BLOCK_VALIDATION_RESULT_CACHED_INVALID
    INVALID_HEADER = _core.BLOCK_VALIDATION_RESULT_INVALID_HEADER
    MUTATED = _core.BLOCK_VALIDATION_RESULT_MUTATED
    MISSING_PREV = _core.BLOCK_VALIDATION_RESULT_MISSING_PREV
    INVALID_PREV = _core.BLOCK_VALIDATION_RESULT_INVALID_PREV
    TIME_FUTURE = _core.BLOCK_VALIDATION_RESULT_TIME_FUTURE
    HEADER_LOW_WORK = _core.BLOCK_VALIDATION_RESULT_HEADER_LOW_WORK


class SynchronizationState(_enum.IntEnum):
    """Current sync state passed to tip changed callbacks."""

    INIT_REINDEX = _core.SYNCHRONIZATION_STATE_INIT_REINDEX
    INIT_DOWNLOAD = _core.SYNCHRONIZATION_STATE_INIT_DOWNLOAD
    POST_INIT = _core.SYNCHRONIZATION_STATE_POST_INIT


class Warning_(_enum.IntEnum):
    """Warning types issued by validation."""

    UNKNOWN_NEW_RULES_ACTIVATED = _core.WARNING_UNKNOWN_NEW_RULES_ACTIVATED
    LARGE_WORK_INVALID_CHAIN = _core.WARNING_LARGE_WORK_INVALID_CHAIN


class LogCategory(_enum.IntEnum):
    """Logging categories used by the kernel."""

    ALL = _core.LOG_CATEGORY_ALL
    BENCH = _core.LOG_CATEGORY_BENCH
    BLOCKSTORAGE = _core.LOG_CATEGORY_BLOCKSTORAGE
    COINDB = _core.LOG_CATEGORY_COINDB
    LEVELDB = _core.LOG_CATEGORY_LEVELDB
    MEMPOOL = _core.LOG_CATEGORY_MEMPOOL
    PRUNE = _core.LOG_CATEGORY_PRUNE
    RAND = _core.LOG_CATEGORY_RAND
    REINDEX = _core.LOG_CATEGORY_REINDEX
    VALIDATION = _core.LOG_CATEGORY_VALIDATION
    KERNEL = _core.LOG_CATEGORY_KERNEL


class LogLevel(_enum.IntEnum):
    """The level at which logs should be produced."""

    TRACE = _core.LOG_LEVEL_TRACE
    DEBUG = _core.LOG_LEVEL_DEBUG
    INFO = _core.LOG_LEVEL_INFO


def verify_script(
    script_pubkey,
    amount,
    tx_to,
    input_index,
    flags=ScriptVerificationFlags.ALL,
    precomputed_transaction_data=None,
):
    """Verify that ``tx_to``'s input at ``input_index`` validly spends
    ``script_pubkey`` under the constraints in ``flags``.

    ``amount`` is required when the WITNESS flag is set. Taproot
    verification additionally requires ``precomputed_transaction_data``
    created with the spent outputs.
    """
    return script_pubkey.verify(
        amount,
        tx_to,
        input_index,
        int(flags),
        precomputed_transaction_data,
    )


def load_chainstate(
    chain_type,
    data_dir,
    blocks_dir=None,
    *,
    context=None,
    worker_threads=None,
    wipe_block_tree_db=False,
    wipe_chainstate_db=False,
    block_tree_db_in_memory=False,
    chainstate_db_in_memory=False,
):
    """Create a :class:`ChainstateManager` for ``chain_type`` rooted at
    ``data_dir``, creating directories as needed.

    ``blocks_dir`` defaults to ``<data_dir>/blocks``. Pass ``context`` to
    supply a preconfigured :class:`Context` (e.g. with notification or
    validation interface callbacks); otherwise a plain context for
    ``chain_type`` is created.
    """
    if context is None:
        options = ContextOptions()
        options.set_chainparams(ChainParameters(int(chain_type)))
        context = Context(options)
    data_dir = _os.fspath(data_dir)
    if blocks_dir is None:
        blocks_dir = _os.path.join(data_dir, "blocks")
    chainman_opts = ChainstateManagerOptions(context, data_dir, blocks_dir)
    if worker_threads is not None:
        chainman_opts.set_worker_threads(worker_threads)
    if wipe_block_tree_db or wipe_chainstate_db:
        chainman_opts.set_wipe_dbs(wipe_block_tree_db, wipe_chainstate_db)
    if block_tree_db_in_memory:
        chainman_opts.set_block_tree_db_in_memory(True)
    if chainstate_db_in_memory:
        chainman_opts.set_chainstate_db_in_memory(True)
    return ChainstateManager(chainman_opts)


__all__ = [
    "Block",
    "BlockHash",
    "BlockHeader",
    "BlockSpentOutputs",
    "BlockTreeEntry",
    "BlockValidationResult",
    "BlockValidationState",
    "Chain",
    "ChainParameters",
    "ChainType",
    "ChainstateManager",
    "ChainstateManagerOptions",
    "Coin",
    "Context",
    "ContextOptions",
    "KernelError",
    "LogCategory",
    "LogLevel",
    "LoggingConnection",
    "PrecomputedTransactionData",
    "ScriptError",
    "ScriptExecution",
    "ScriptPubkey",
    "ScriptTrace",
    "ScriptTraceFrame",
    "ScriptTraceFrameKind",
    "ScriptVerificationFlags",
    "SigVersion",
    "SynchronizationState",
    "Transaction",
    "TransactionInput",
    "TransactionOutPoint",
    "TransactionOutput",
    "TransactionSpentOutputs",
    "Txid",
    "ValidationMode",
    "Warning_",
    "debug_script",
    "disassemble",
    "load_chainstate",
    "logging_disable",
    "logging_disable_category",
    "logging_enable_category",
    "logging_set_level_category",
    "logging_set_options",
    "opcode_description",
    "opcode_name",
    "script_trace",
    "trace_available",
    "verify_script",
]
