"""A step-by-step script debugger built on the kernel's script trace hooks.

Bitcoin Core's script interpreter can emit a *trace frame* on evaluator
entry, once per opcode (after the opcode is decoded but before it runs),
and on exit. This module turns that raw stream into something a human can
read: it decodes opcodes, groups frames into the individual scripts that
run during one verification (scriptSig, then scriptPubkey, then any
witness script), and renders a ``btcdeb``-style trace.

The trace feature is compiled out by default. It is only available when
libbitcoinkernel was built with ``-DENABLE_SCRIPT_TRACE=ON``; otherwise
:func:`debug_script` and :func:`script_trace` raise
:class:`~pybitcoinkernel.KernelError`. Use :func:`trace_available` to
check at runtime.

Typical use::

    import pybitcoinkernel as pbk

    trace = pbk.debug_script(script_pubkey, amount, tx_to, input_index, flags)
    print(trace)              # human-readable, btcdeb-style dump
    print(trace.valid, trace.error)
    for execution in trace.executions:
        for step in execution.steps:
            print(step.opcode_name, [b.hex() for b in step.stack])

For streaming/advanced use (e.g. tracing scripts that run during block
validation) register your own callback with :func:`script_trace`::

    with pbk.script_trace(lambda frame: print(frame)):
        chainman.process_block(block)
"""

import contextlib as _contextlib
import enum as _enum

from pybitcoinkernel import _bitcoinkernel as _core
from pybitcoinkernel._bitcoinkernel import ScriptTraceFrame

__all__ = [
    "ScriptError",
    "ScriptExecution",
    "ScriptTrace",
    "ScriptTraceFrame",
    "ScriptTraceFrameKind",
    "SigVersion",
    "debug_script",
    "debug_transaction",
    "disassemble",
    "opcode_description",
    "opcode_name",
    "script_trace",
    "trace_available",
]


class ScriptTraceFrameKind(_enum.IntEnum):
    """Which point of script execution a trace frame was emitted at."""

    BEGIN = _core.SCRIPT_TRACE_FRAME_KIND_BEGIN
    STEP = _core.SCRIPT_TRACE_FRAME_KIND_STEP
    END = _core.SCRIPT_TRACE_FRAME_KIND_END


class SigVersion(_enum.IntEnum):
    """The signature-hashing regime a script is evaluated under."""

    BASE = _core.SIG_VERSION_BASE
    WITNESS_V0 = _core.SIG_VERSION_WITNESS_V0
    TAPROOT = _core.SIG_VERSION_TAPROOT
    TAPSCRIPT = _core.SIG_VERSION_TAPSCRIPT


class ScriptError(_enum.IntEnum):
    """Reason a script evaluation ended, mirroring ``script_error.h``.

    ``OK`` (0) means the script left a single true value on the stack.
    Any other value is why the interpreter stopped.
    """

    OK = 0
    UNKNOWN_ERROR = 1
    EVAL_FALSE = 2
    OP_RETURN = 3
    SCRIPTNUM = 4
    SCRIPT_SIZE = 5
    PUSH_SIZE = 6
    OP_COUNT = 7
    STACK_SIZE = 8
    SIG_COUNT = 9
    PUBKEY_COUNT = 10
    VERIFY = 11
    EQUALVERIFY = 12
    CHECKMULTISIGVERIFY = 13
    CHECKSIGVERIFY = 14
    NUMEQUALVERIFY = 15
    BAD_OPCODE = 16
    DISABLED_OPCODE = 17
    INVALID_STACK_OPERATION = 18
    INVALID_ALTSTACK_OPERATION = 19
    UNBALANCED_CONDITIONAL = 20
    NEGATIVE_LOCKTIME = 21
    UNSATISFIED_LOCKTIME = 22
    SIG_HASHTYPE = 23
    SIG_DER = 24
    MINIMALDATA = 25
    SIG_PUSHONLY = 26
    SIG_HIGH_S = 27
    SIG_NULLDUMMY = 28
    PUBKEYTYPE = 29
    CLEANSTACK = 30
    MINIMALIF = 31
    SIG_NULLFAIL = 32
    DISCOURAGE_UPGRADABLE_NOPS = 33
    DISCOURAGE_UPGRADABLE_WITNESS_PROGRAM = 34
    DISCOURAGE_UPGRADABLE_TAPROOT_VERSION = 35
    DISCOURAGE_OP_SUCCESS = 36
    DISCOURAGE_UPGRADABLE_PUBKEYTYPE = 37
    WITNESS_PROGRAM_WRONG_LENGTH = 38
    WITNESS_PROGRAM_WITNESS_EMPTY = 39
    WITNESS_PROGRAM_MISMATCH = 40
    WITNESS_MALLEATED = 41
    WITNESS_MALLEATED_P2SH = 42
    WITNESS_UNEXPECTED = 43
    WITNESS_PUBKEYTYPE = 44
    SCHNORR_SIG_SIZE = 45
    SCHNORR_SIG_HASHTYPE = 46
    SCHNORR_SIG = 47
    TAPROOT_WRONG_CONTROL_SIZE = 48
    TAPSCRIPT_VALIDATION_WEIGHT = 49
    TAPSCRIPT_CHECKMULTISIG = 50
    TAPSCRIPT_MINIMALIF = 51
    TAPSCRIPT_EMPTY_PUBKEY = 52
    OP_CODESEPARATOR = 53
    SIG_FINDANDDELETE = 54


# Opcode value -> name, from src/script/script.h. Direct pushes (0x01..0x4b)
# have no OP_ name and are handled specially in opcode_name().
_OPCODE_NAMES = {
    0x00: "OP_0",
    0x4C: "OP_PUSHDATA1",
    0x4D: "OP_PUSHDATA2",
    0x4E: "OP_PUSHDATA4",
    0x4F: "OP_1NEGATE",
    0x50: "OP_RESERVED",
    0x51: "OP_1",
    0x52: "OP_2",
    0x53: "OP_3",
    0x54: "OP_4",
    0x55: "OP_5",
    0x56: "OP_6",
    0x57: "OP_7",
    0x58: "OP_8",
    0x59: "OP_9",
    0x5A: "OP_10",
    0x5B: "OP_11",
    0x5C: "OP_12",
    0x5D: "OP_13",
    0x5E: "OP_14",
    0x5F: "OP_15",
    0x60: "OP_16",
    0x61: "OP_NOP",
    0x62: "OP_VER",
    0x63: "OP_IF",
    0x64: "OP_NOTIF",
    0x65: "OP_VERIF",
    0x66: "OP_VERNOTIF",
    0x67: "OP_ELSE",
    0x68: "OP_ENDIF",
    0x69: "OP_VERIFY",
    0x6A: "OP_RETURN",
    0x6B: "OP_TOALTSTACK",
    0x6C: "OP_FROMALTSTACK",
    0x6D: "OP_2DROP",
    0x6E: "OP_2DUP",
    0x6F: "OP_3DUP",
    0x70: "OP_2OVER",
    0x71: "OP_2ROT",
    0x72: "OP_2SWAP",
    0x73: "OP_IFDUP",
    0x74: "OP_DEPTH",
    0x75: "OP_DROP",
    0x76: "OP_DUP",
    0x77: "OP_NIP",
    0x78: "OP_OVER",
    0x79: "OP_PICK",
    0x7A: "OP_ROLL",
    0x7B: "OP_ROT",
    0x7C: "OP_SWAP",
    0x7D: "OP_TUCK",
    0x7E: "OP_CAT",
    0x7F: "OP_SUBSTR",
    0x80: "OP_LEFT",
    0x81: "OP_RIGHT",
    0x82: "OP_SIZE",
    0x83: "OP_INVERT",
    0x84: "OP_AND",
    0x85: "OP_OR",
    0x86: "OP_XOR",
    0x87: "OP_EQUAL",
    0x88: "OP_EQUALVERIFY",
    0x89: "OP_RESERVED1",
    0x8A: "OP_RESERVED2",
    0x8B: "OP_1ADD",
    0x8C: "OP_1SUB",
    0x8D: "OP_2MUL",
    0x8E: "OP_2DIV",
    0x8F: "OP_NEGATE",
    0x90: "OP_ABS",
    0x91: "OP_NOT",
    0x92: "OP_0NOTEQUAL",
    0x93: "OP_ADD",
    0x94: "OP_SUB",
    0x95: "OP_MUL",
    0x96: "OP_DIV",
    0x97: "OP_MOD",
    0x98: "OP_LSHIFT",
    0x99: "OP_RSHIFT",
    0x9A: "OP_BOOLAND",
    0x9B: "OP_BOOLOR",
    0x9C: "OP_NUMEQUAL",
    0x9D: "OP_NUMEQUALVERIFY",
    0x9E: "OP_NUMNOTEQUAL",
    0x9F: "OP_LESSTHAN",
    0xA0: "OP_GREATERTHAN",
    0xA1: "OP_LESSTHANOREQUAL",
    0xA2: "OP_GREATERTHANOREQUAL",
    0xA3: "OP_MIN",
    0xA4: "OP_MAX",
    0xA5: "OP_WITHIN",
    0xA6: "OP_RIPEMD160",
    0xA7: "OP_SHA1",
    0xA8: "OP_SHA256",
    0xA9: "OP_HASH160",
    0xAA: "OP_HASH256",
    0xAB: "OP_CODESEPARATOR",
    0xAC: "OP_CHECKSIG",
    0xAD: "OP_CHECKSIGVERIFY",
    0xAE: "OP_CHECKMULTISIG",
    0xAF: "OP_CHECKMULTISIGVERIFY",
    0xB0: "OP_NOP1",
    0xB1: "OP_CHECKLOCKTIMEVERIFY",
    0xB2: "OP_CHECKSEQUENCEVERIFY",
    0xB3: "OP_NOP4",
    0xB4: "OP_NOP5",
    0xB5: "OP_NOP6",
    0xB6: "OP_NOP7",
    0xB7: "OP_NOP8",
    0xB8: "OP_NOP9",
    0xB9: "OP_NOP10",
    0xBA: "OP_CHECKSIGADD",
    0xFF: "OP_INVALIDOPCODE",
}


def opcode_name(opcode):
    """Return the mnemonic for a one-byte ``opcode`` (e.g. ``"OP_DUP"``).

    Direct data pushes (``0x01``..``0x4b``) become ``"OP_PUSHBYTES_<n>"``,
    matching Bitcoin Core's disassembly. Unassigned opcodes render as
    ``"OP_UNKNOWN_0x<hex>"``.
    """
    name = _OPCODE_NAMES.get(opcode)
    if name is not None:
        return name
    if 0x01 <= opcode <= 0x4B:
        return f"OP_PUSHBYTES_{opcode}"
    return f"OP_UNKNOWN_0x{opcode:02x}"


# Opcode value -> a one-line plain-English description of its effect. Data
# pushes (0x00..0x4e) and the OP_1..OP_16 range are described in
# opcode_description() so the count/number can be interpolated.
_OPCODE_DESCRIPTIONS = {
    0x4F: "Push the number -1.",
    0x50: "Reserved; makes the script invalid if executed.",
    0x61: "Do nothing.",
    0x62: "Reserved; makes the script invalid if executed.",
    0x63: "If the top stack value is true, run the following block (pops it).",
    0x64: "If the top stack value is false, run the following block (pops it).",
    0x65: "Reserved; makes the script invalid even when not executed.",
    0x66: "Reserved; makes the script invalid even when not executed.",
    0x67: "Run the following block if the matching OP_IF/OP_NOTIF did not.",
    0x68: "End an OP_IF / OP_NOTIF / OP_ELSE block.",
    0x69: "Fail the script unless the top stack value is true; then pop it.",
    0x6A: "Fail the script immediately (marks an output unspendable).",
    0x6B: "Move the top stack item to the alt stack.",
    0x6C: "Move the top alt-stack item back to the main stack.",
    0x6D: "Remove the top two stack items.",
    0x6E: "Duplicate the top two stack items.",
    0x6F: "Duplicate the top three stack items.",
    0x70: "Copy the second pair of items to the top.",
    0x71: "Move the third pair of items to the top.",
    0x72: "Swap the top two pairs of items.",
    0x73: "Duplicate the top stack item if it is non-zero.",
    0x74: "Push the current stack depth (number of items).",
    0x75: "Remove the top stack item.",
    0x76: "Duplicate the top stack item.",
    0x77: "Remove the second-from-top stack item.",
    0x78: "Copy the second-from-top item to the top.",
    0x79: "Copy the item n-deep (n taken from the top) to the top.",
    0x7A: "Move the item n-deep (n taken from the top) to the top.",
    0x7B: "Rotate the top three items (third item moves to the top).",
    0x7C: "Swap the top two items.",
    0x7D: "Copy the top item and insert it below the second item.",
    0x7E: "Disabled: concatenate two byte vectors.",
    0x7F: "Disabled: extract a substring.",
    0x80: "Disabled: keep the left part of a string.",
    0x81: "Disabled: keep the right part of a string.",
    0x82: "Push the byte length of the top item (without removing it).",
    0x83: "Disabled: bitwise NOT.",
    0x84: "Disabled: bitwise AND.",
    0x85: "Disabled: bitwise OR.",
    0x86: "Disabled: bitwise XOR.",
    0x87: "Push true if the top two items are equal, else false.",
    0x88: "Fail the script unless the top two items are equal.",
    0x89: "Reserved; makes the script invalid if executed.",
    0x8A: "Reserved; makes the script invalid if executed.",
    0x8B: "Add 1 to the top number.",
    0x8C: "Subtract 1 from the top number.",
    0x8D: "Disabled: multiply the top number by 2.",
    0x8E: "Disabled: divide the top number by 2.",
    0x8F: "Negate the top number.",
    0x90: "Replace the top number with its absolute value.",
    0x91: "Push true if the top number is 0, else false.",
    0x92: "Push true if the top number is not 0, else false.",
    0x93: "Add the top two numbers.",
    0x94: "Subtract the top number from the second-from-top number.",
    0x95: "Disabled: multiply the top two numbers.",
    0x96: "Disabled: divide the second number by the top.",
    0x97: "Disabled: remainder of the division.",
    0x98: "Disabled: left bit-shift.",
    0x99: "Disabled: right bit-shift.",
    0x9A: "Push true if both numbers are non-zero.",
    0x9B: "Push true if either number is non-zero.",
    0x9C: "Push true if the two numbers are equal.",
    0x9D: "Fail the script unless the two numbers are equal.",
    0x9E: "Push true if the two numbers are not equal.",
    0x9F: "Push true if the second number is less than the top.",
    0xA0: "Push true if the second number is greater than the top.",
    0xA1: "Push true if the second number is less than or equal to the top.",
    0xA2: "Push true if the second number is greater than or equal to the top.",
    0xA3: "Push the smaller of the top two numbers.",
    0xA4: "Push the larger of the top two numbers.",
    0xA5: "Push true if x is within the range [min, max).",
    0xA6: "Replace the top item with its RIPEMD-160 hash.",
    0xA7: "Replace the top item with its SHA-1 hash.",
    0xA8: "Replace the top item with its SHA-256 hash.",
    0xA9: "Replace the top item with RIPEMD160(SHA256(item)).",
    0xAA: "Replace the top item with SHA256(SHA256(item)).",
    0xAB: "Mark where signing of the script starts (for later signatures).",
    0xAC: "Check a signature against a pubkey; push true or false.",
    0xAD: "Check a signature against a pubkey; fail the script if invalid.",
    0xAE: "Check M-of-N signatures against N pubkeys; push true or false.",
    0xAF: "Check M-of-N signatures; fail the script if invalid.",
    0xB0: "Do nothing (reserved for future soft-fork upgrades).",
    0xB1: "Fail unless the tx locktime is at/after the top value (BIP65).",
    0xB2: "Fail unless the input's relative locktime is satisfied (BIP112).",
    0xB3: "Do nothing (reserved for future soft-fork upgrades).",
    0xB4: "Do nothing (reserved for future soft-fork upgrades).",
    0xB5: "Do nothing (reserved for future soft-fork upgrades).",
    0xB6: "Do nothing (reserved for future soft-fork upgrades).",
    0xB7: "Do nothing (reserved for future soft-fork upgrades).",
    0xB8: "Do nothing (reserved for future soft-fork upgrades).",
    0xB9: "Do nothing (reserved for future soft-fork upgrades).",
    0xBA: "Tapscript: add 1 to a counter if the signature is valid (BIP342).",
    0xFF: "Invalid opcode; always fails the script.",
}


def opcode_description(opcode):
    """Return a short plain-English description of what ``opcode`` does.

    Returns an empty string for opcodes with no known description.
    """
    if opcode == 0x00:
        return "Push an empty byte vector (represents false / zero)."
    if 0x01 <= opcode <= 0x4B:
        return f"Push the next {opcode} bytes onto the stack."
    if opcode == 0x4C:
        return "Push bytes counted by the next 1-byte length."
    if opcode == 0x4D:
        return "Push bytes counted by the next 2-byte little-endian length."
    if opcode == 0x4E:
        return "Push bytes counted by the next 4-byte little-endian length."
    if 0x51 <= opcode <= 0x60:
        return f"Push the number {opcode - 0x50}."
    return _OPCODE_DESCRIPTIONS.get(opcode, "")


def disassemble(script):
    """Disassemble ``script`` (``bytes`` or :class:`~pybitcoinkernel.ScriptPubkey`)
    into a list of ``(opcode_pos, mnemonic, data)`` tuples.

    ``data`` is the pushed bytes for push operations, otherwise ``b""``.
    Truncated pushes at the end of the script are reported with whatever
    bytes remain, so malformed scripts still disassemble rather than raise.
    This is a pure decoder that does not need the trace feature.
    """
    if hasattr(script, "to_bytes"):
        script = script.to_bytes()
    script = bytes(script)
    out = []
    i = 0
    pos = 0
    n = len(script)
    while i < n:
        op = script[i]
        i += 1
        data = b""
        if 1 <= op <= 0x4B:
            data = script[i : i + op]
            i += op
        elif op == 0x4C:  # OP_PUSHDATA1
            if i < n:
                ln = script[i]
                i += 1
                data = script[i : i + ln]
                i += ln
        elif op == 0x4D:  # OP_PUSHDATA2
            if i + 1 < n:
                ln = int.from_bytes(script[i : i + 2], "little")
                i += 2
                data = script[i : i + ln]
                i += ln
        elif op == 0x4E:  # OP_PUSHDATA4
            if i + 3 < n:
                ln = int.from_bytes(script[i : i + 4], "little")
                i += 4
                data = script[i : i + ln]
                i += ln
        out.append((pos, opcode_name(op), data))
        pos += 1
    return out


def trace_available():
    """Return ``True`` if libbitcoinkernel exposes the script trace hooks.

    They are only compiled in when the kernel is built with
    ``-DENABLE_SCRIPT_TRACE=ON``.
    """
    try:
        _core.script_trace_register_callback(lambda _frame: None)
    except _core.KernelError:
        return False
    _core.script_trace_unregister_callback()
    return True


@_contextlib.contextmanager
def script_trace(callback):
    """Context manager that installs ``callback`` as the global script trace
    callback for its duration and removes it on exit.

    ``callback`` is invoked with a :class:`ScriptTraceFrame` on evaluator
    entry (``BEGIN``), once per opcode (``STEP``), and on exit (``END``),
    for *every* script evaluated while the context is active - including
    those run deep inside block validation. Raises
    :class:`~pybitcoinkernel.KernelError` if tracing is unavailable.
    """
    _core.script_trace_register_callback(callback)
    try:
        yield
    finally:
        _core.script_trace_unregister_callback()


class ScriptExecution:
    """One contiguous script evaluation: a ``BEGIN``, its ``STEP`` frames,
    and the terminating ``END`` frame.

    A single :func:`debug_script` call produces several of these - the
    input's scriptSig, then the output's scriptPubkey, then any witness
    or P2SH-redeem scripts, each run by the interpreter in turn.
    """

    def __init__(self, frames):
        self.frames = list(frames)

    @property
    def begin(self):
        """The ``BEGIN`` frame, or ``None`` if this group has none."""
        for f in self.frames:
            if f.kind == ScriptTraceFrameKind.BEGIN:
                return f
        return None

    @property
    def end(self):
        """The ``END`` frame, or ``None`` if this group has none."""
        for f in reversed(self.frames):
            if f.kind == ScriptTraceFrameKind.END:
                return f
        return None

    @property
    def steps(self):
        """The per-opcode ``STEP`` frames, in execution order."""
        return [f for f in self.frames if f.kind == ScriptTraceFrameKind.STEP]

    @property
    def script(self):
        """The raw script bytes being evaluated."""
        ref = self.begin or (self.frames[0] if self.frames else None)
        return ref.script if ref is not None else b""

    @property
    def sig_version(self):
        """The :class:`SigVersion` this script ran under."""
        ref = self.begin or (self.frames[0] if self.frames else None)
        return SigVersion(ref.sig_version) if ref is not None else SigVersion.BASE

    @property
    def error(self):
        """The :class:`ScriptError` from the ``END`` frame (``OK`` if none)."""
        end = self.end
        return ScriptError(end.script_error) if end is not None else ScriptError.OK

    @property
    def final_stack(self):
        """The stack as it stood at the ``END`` frame."""
        end = self.end
        return list(end.stack) if end is not None else []

    def __repr__(self):
        return (
            f"<ScriptExecution {self.sig_version.name} "
            f"steps={len(self.steps)} error={self.error.name}>"
        )


class ScriptTrace:
    """The full result of a traced verification: every frame, split into
    :class:`ScriptExecution` groups, plus the overall verdict.
    """

    def __init__(self, valid, frames):
        self.valid = bool(valid)
        self.frames = list(frames)

    @property
    def executions(self):
        """The frames split into per-script :class:`ScriptExecution` groups."""
        groups = []
        current = None
        for f in self.frames:
            if f.kind == ScriptTraceFrameKind.BEGIN:
                current = [f]
                groups.append(current)
            elif current is None:
                current = [f]
                groups.append(current)
            else:
                current.append(f)
            if f.kind == ScriptTraceFrameKind.END:
                current = None
        return [ScriptExecution(g) for g in groups]

    @property
    def error(self):
        """The first non-``OK`` :class:`ScriptError` any script reported.

        Note that a ``False`` :attr:`valid` can still pair with an ``OK``
        error: the interpreter (``EvalScript``) can run every script to
        completion yet leave a *false* value on top of the stack, and that
        final ``EVAL_FALSE`` / clean-stack verdict is decided by
        ``VerifyScript`` *after* the last trace frame is emitted. In that
        case inspect :attr:`valid` and the final stack.
        """
        for execution in self.executions:
            if execution.error != ScriptError.OK:
                return execution.error
        return ScriptError.OK

    def format(self, max_item_bytes=40):
        """Render a ``btcdeb``-style, multi-line trace of the evaluation.

        ``max_item_bytes`` truncates long stack items (signatures, pubkeys)
        in the display; pass ``None`` to show them in full.
        """
        return _format_trace(self, max_item_bytes)

    def __str__(self):
        return self.format()

    def __repr__(self):
        return (
            f"<ScriptTrace valid={self.valid} error={self.error.name} "
            f"executions={len(self.executions)} frames={len(self.frames)}>"
        )


def debug_script(
    script_pubkey,
    amount,
    tx_to,
    input_index,
    flags=None,
    precomputed_transaction_data=None,
):
    """Verify a script while capturing a full execution trace.

    Arguments mirror :func:`pybitcoinkernel.verify_script`. Returns a
    :class:`ScriptTrace` holding the verdict and every trace frame the
    interpreter emitted. Raises :class:`~pybitcoinkernel.KernelError` if the
    kernel was not built with script tracing enabled (see
    :func:`trace_available`).
    """
    # Imported lazily to avoid a circular import at package import time.
    import pybitcoinkernel as _pbk

    if flags is None:
        flags = _pbk.ScriptVerificationFlags.ALL

    frames = []
    with script_trace(frames.append):
        valid = _pbk.verify_script(
            script_pubkey,
            amount,
            tx_to,
            input_index,
            flags,
            precomputed_transaction_data,
        )
    return ScriptTrace(valid, frames)


def debug_transaction(tx, spent_outputs, flags=None):
    """Trace the script verification of *every* input of ``tx``.

    ``spent_outputs`` is one :class:`~pybitcoinkernel.TransactionOutput` per
    input, in input order -- the coin each input spends. Returns a list of
    :class:`ScriptTrace`, one per input in the same order; the overall
    script-level verdict is ``all(t.valid for t in traces)``.

    This is only per-input *script* verification (looped), not full consensus
    validation -- amounts, double-spends, weight and maturity are enforced by
    the chainstate, not here. Raises :class:`~pybitcoinkernel.KernelError` if
    the kernel was not built with script tracing enabled (see
    :func:`trace_available`), and ``ValueError`` if the number of spent
    outputs does not match the number of inputs.
    """
    import pybitcoinkernel as _pbk

    if flags is None:
        flags = _pbk.ScriptVerificationFlags.ALL
    spent = list(spent_outputs)
    if len(spent) != tx.n_inputs:
        raise ValueError(
            f"expected one spent output per input ({tx.n_inputs}), got {len(spent)}"
        )
    # Built once and shared across inputs so taproot inputs (whose sighash
    # commits to all spent outputs) verify under the default ALL flags.
    precomputed = _pbk.PrecomputedTransactionData(tx, spent)
    return [
        debug_script(o.script_pubkey, o.amount, tx, i, flags, precomputed)
        for i, o in enumerate(spent)
    ]


def _cast_to_bool(item):
    """Mirror Bitcoin Core's ``CastToBool``: a byte vector is true unless
    every byte is zero, allowing a trailing ``0x80`` sign bit (negative
    zero is still false)."""
    for i, b in enumerate(item):
        if b != 0:
            if i == len(item) - 1 and b == 0x80:
                return False
            return True
    return False


def _render_item(item, max_item_bytes):
    if not item:
        return "0x"  # empty byte vector (also the "false" value)
    h = item.hex()
    if max_item_bytes is not None and len(item) > max_item_bytes:
        keep = max_item_bytes * 2
        return f"{h[:keep]}...({len(item)} bytes)"
    return h


def _render_stack(stack, max_item_bytes):
    if not stack:
        return "[]"
    return "[" + ", ".join(_render_item(i, max_item_bytes) for i in stack) + "]"


def _format_trace(trace, max_item_bytes):
    lines = []
    verdict = "VALID" if trace.valid else "INVALID"
    lines.append(f"script verification: {verdict}  (error: {trace.error.name})")
    for idx, execution in enumerate(trace.executions):
        script_hex = execution.script.hex()
        lines.append("")
        lines.append(
            f"=== script #{idx} : {execution.sig_version.name} "
            f"({len(execution.script)} bytes) ==="
        )
        lines.append(f"    {script_hex or '(empty)'}")
        # Show the opcode about to run (name + what it does), then the stack
        # as it stands *before* the opcode executes.
        for step in execution.steps:
            note = opcode_description(step.opcode)
            if not step.executed:
                note = (note + "  " if note else "") + "(skipped in this branch)"
            lines.append(f"  #{step.opcode_pos:04d}  {opcode_name(step.opcode):<22} {note}")
            lines.append(
                f"         stack: {_render_stack(step.stack, max_item_bytes)}"
            )
        end = execution.end
        if end is not None:
            lines.append(
                f"  result: {_render_stack(end.stack, max_item_bytes)}"
                f"  -> {execution.error.name}"
            )
    # The interpreter can run clean yet still fail overall (a false top-of-stack
    # or unclean stack that VerifyScript rejects after tracing ends). Say so.
    if not trace.valid and trace.error == ScriptError.OK:
        last = trace.executions[-1] if trace.executions else None
        top = last.final_stack[-1] if (last and last.final_stack) else b""
        reason = "top of stack is false" if not _cast_to_bool(top) else (
            "stack not clean / policy check failed after evaluation"
        )
        lines.append("")
        lines.append(
            f"  note: scripts evaluated without error, but verification "
            f"failed ({reason})."
        )
    return "\n".join(lines)
