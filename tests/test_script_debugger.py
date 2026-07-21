"""Tests for the opcode-level script debugger (kernel script trace hooks).

The trace hooks are only compiled into libbitcoinkernel when it is built
with -DENABLE_SCRIPT_TRACE=ON, so every test that actually traces is
gated on pbk.trace_available(). The pure decoders (disassemble,
opcode_name) and the enums are always exercised.
"""

import pytest

import pybitcoinkernel as pbk

# Same vectors as test_script_verify.py / example 02.
LEGACY_SPENT_SCRIPT = "76a9144bfbaf6afb76cc5771bc6404810d1cc041a6933988ac"
LEGACY_SPENDING_TX = (
    "02000000013f7cebd65c27431a90bba7f796914fe8cc2ddfc3f2cbd6f7e5f2fc854534da"
    "95000000006b483045022100de1ac3bcdfb0332207c4a91f3832bd2c2915840165f876ab"
    "47c5f8996b971c3602201c6c053d750fadde599e6f5c4e1963df0f01fc0d97815e8157e3"
    "d59fe09ca30d012103699b464d1d8bc9e47d4fb1cdaa89a1c5783d68363c4dbc4b524ed3"
    "d857148617feffffff02836d3c01000000001976a914fc25d6d5c94003bf5b0c7b640a24"
    "8e2c637fcfb088ac7ada8202000000001976a914fbed3d9b11183209a57999d54d59f67c"
    "019e756c88ac6acb0700"
)

SEGWIT_SPENT_SCRIPT = "0020701a8d401c84fb13e6baf169d59684e17abd9fa216c8cc5b9fc63d622ff8c58d"
SEGWIT_SPENDING_TX = (
    "010000000001011f97548fbbe7a0db7588a66e18d803d0089315aa7d4cc28360b6ec50ef"
    "36718a0100000000ffffffff02df1776000000000017a9146c002a686959067f4866b8fb"
    "493ad7970290ab728757d29f0000000000220020701a8d401c84fb13e6baf169d59684e1"
    "7abd9fa216c8cc5b9fc63d622ff8c58d04004730440220565d170eed95ff95027a69b313"
    "758450ba84a01224e1f7f130dda46e94d13f8602207bdd20e307f062594022f12ed5017b"
    "bf4a055a06aea91c10110a0e3bb23117fc014730440220647d2dc5b15f60bc37dc42618a"
    "370b2a1490293f9e5c8464f53ec4fe1dfe067302203598773895b4b16d37485cbe21b337"
    "f4e4b650739880098c592553add7dd4355016952210375e00eb72e29da82b89367947f29"
    "ef34afb75e8654f6ea368e0acdfd92976b7c2103a1b26313f430c4b15bb1fdce66320765"
    "9d8cac749a0e53d70eff01874496feff2103c96d495bfdd5ba4145e3e046fee45e84a8a4"
    "8ad05bd8dbb395c011a32cf9f88053ae00000000"
)
SEGWIT_AMOUNT = 18393430

TAPROOT_SPENT_SCRIPT = "5120339ce7e165e67d93adb3fef88a6d4beed33f01fa876f05a225242b82a631abc0"
TAPROOT_SPENDING_TX = (
    "01000000000101d1f1c1f8cdf6759167b90f52c9ad358a369f95284e841d7a2536cef31c"
    "0549580100000000fdffffff020000000000000000316a2f49206c696b65205363686e6f"
    "7272207369677320616e6420492063616e6e6f74206c69652e204062697462756734329e"
    "06010000000000225120a37c3903c8d0db6512e2b40b0dffa05e5a3ab73603ce8c9c4b77"
    "71e5412328f90140a60c383f71bac0ec919b1d7dbc3eb72dd56e7aa99583615564f9f99b"
    "8ae4e837b758773a5b2e4c51348854c8389f008e05029db7f464a5ff2e01d5e6e626174a"
    "ffd30a00"
)
TAPROOT_AMOUNT = 88480

PRE_TAPROOT = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT

requires_trace = pytest.mark.skipif(
    not pbk.trace_available(),
    reason="libbitcoinkernel built without -DENABLE_SCRIPT_TRACE=ON",
)


# --- Pure decoders (no tracing required) -------------------------------


def test_opcode_name_known():
    assert pbk.opcode_name(0x76) == "OP_DUP"
    assert pbk.opcode_name(0xA9) == "OP_HASH160"
    assert pbk.opcode_name(0x00) == "OP_0"
    assert pbk.opcode_name(0xAC) == "OP_CHECKSIG"


def test_opcode_name_pushbytes():
    assert pbk.opcode_name(0x01) == "OP_PUSHBYTES_1"
    assert pbk.opcode_name(0x14) == "OP_PUSHBYTES_20"
    assert pbk.opcode_name(0x4B) == "OP_PUSHBYTES_75"


def test_opcode_name_unknown():
    assert pbk.opcode_name(0xBB) == "OP_UNKNOWN_0xbb"


def test_disassemble_p2pkh():
    dis = pbk.disassemble(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    names = [name for _pos, name, _data in dis]
    assert names == [
        "OP_DUP",
        "OP_HASH160",
        "OP_PUSHBYTES_20",
        "OP_EQUALVERIFY",
        "OP_CHECKSIG",
    ]
    # positions are opcode indices, and the push carries its 20-byte payload
    assert [pos for pos, _n, _d in dis] == [0, 1, 2, 3, 4]
    assert dis[2][2] == bytes.fromhex("4bfbaf6afb76cc5771bc6404810d1cc041a69339")
    assert all(data == b"" for _pos, _name, data in (dis[0], dis[1], dis[3], dis[4]))


def test_disassemble_accepts_scriptpubkey():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    assert pbk.disassemble(script) == pbk.disassemble(bytes.fromhex(LEGACY_SPENT_SCRIPT))


def test_disassemble_truncated_push_does_not_raise():
    # A push claiming 5 bytes but only 2 present: decode what's there.
    dis = pbk.disassemble(bytes([0x05, 0xAA, 0xBB]))
    assert dis == [(0, "OP_PUSHBYTES_5", b"\xaa\xbb")]


def test_disassemble_pushdata1():
    dis = pbk.disassemble(bytes([0x4C, 0x02, 0xDE, 0xAD]))
    assert dis == [(0, "OP_PUSHDATA1", b"\xde\xad")]


def test_scripterror_values():
    # A couple of anchors so the enum stays pinned to script_error.h.
    assert pbk.ScriptError.OK == 0
    assert pbk.ScriptError.EVAL_FALSE == 2
    assert pbk.ScriptError.EQUALVERIFY == 12


def test_trace_available_is_bool():
    assert isinstance(pbk.trace_available(), bool)


# --- Tracing (needs ENABLE_SCRIPT_TRACE) -------------------------------


@requires_trace
def test_debug_legacy_frame_structure():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    trace = pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT)

    assert trace.valid is True
    assert trace.error == pbk.ScriptError.OK
    # scriptSig then scriptPubkey.
    assert len(trace.executions) == 2
    # Matches Bitcoin Core's own kernel test (btck_script_trace_tests): 11 frames.
    assert len(trace.frames) == 11

    kinds = [pbk.ScriptTraceFrameKind(f.kind) for f in trace.frames]
    K = pbk.ScriptTraceFrameKind
    assert kinds[0] == K.BEGIN
    assert kinds[3] == K.END
    assert kinds[4] == K.BEGIN
    assert kinds[10] == K.END
    for i in (1, 2, 5, 6, 7, 8, 9):
        assert kinds[i] == K.STEP


@requires_trace
def test_debug_legacy_execution_details():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    trace = pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT)

    script_sig, script_pubkey = trace.executions

    # scriptSig pushes the signature then the pubkey (two pushes).
    assert [pbk.opcode_name(s.opcode) for s in script_sig.steps] == [
        "OP_PUSHBYTES_72",
        "OP_PUSHBYTES_33",
    ]

    # scriptPubkey is the standard P2PKH template.
    assert [pbk.opcode_name(s.opcode) for s in script_pubkey.steps] == [
        "OP_DUP",
        "OP_HASH160",
        "OP_PUSHBYTES_20",
        "OP_EQUALVERIFY",
        "OP_CHECKSIG",
    ]
    assert script_pubkey.sig_version == pbk.SigVersion.BASE
    assert script_pubkey.error == pbk.ScriptError.OK
    # A single truthy item is left on the stack at the end.
    assert script_pubkey.final_stack == [b"\x01"]


@requires_trace
def test_step_stack_snapshots_are_bytes():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    trace = pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT)

    # OP_HASH160 is the second step of the scriptPubkey; before it runs the
    # stack top is the pubkey, and afterwards (next step) its hash appears.
    pubkey_script = trace.executions[1]
    dup, hash160, pushhash = pubkey_script.steps[0], pubkey_script.steps[1], pubkey_script.steps[2]
    assert all(isinstance(item, bytes) for item in hash160.stack)
    # OP_DUP duplicated the pubkey: depth grew from 2 to 3.
    assert len(dup.stack) == 2
    assert len(hash160.stack) == 3
    assert hash160.stack[-1] == hash160.stack[-2]  # the duplicate
    # OP_HASH160 replaced the top with its 20-byte hash.
    assert len(pushhash.stack[-1]) == 20


@requires_trace
def test_frame_is_immutable():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    frame = pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT).frames[0]
    with pytest.raises((AttributeError, TypeError)):
        frame.opcode = 5
    with pytest.raises(TypeError):
        pbk.ScriptTraceFrame()


@requires_trace
def test_debug_segwit_has_witness_execution():
    script = pbk.ScriptPubkey(bytes.fromhex(SEGWIT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(SEGWIT_SPENDING_TX))
    trace = pbk.debug_script(script, SEGWIT_AMOUNT, tx, 0, PRE_TAPROOT)

    assert trace.valid is True
    versions = [e.sig_version for e in trace.executions]
    # scriptSig (empty), scriptPubkey (the witness program), then the
    # witness script itself under the segwit v0 sighash regime.
    assert pbk.SigVersion.WITNESS_V0 in versions
    witness_exec = trace.executions[-1]
    assert witness_exec.sig_version == pbk.SigVersion.WITNESS_V0
    assert pbk.opcode_name(witness_exec.steps[-1].opcode) == "OP_CHECKMULTISIG"


@requires_trace
def test_debug_taproot_keypath_runs_no_script():
    script = pbk.ScriptPubkey(bytes.fromhex(TAPROOT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(TAPROOT_SPENDING_TX))
    spent = pbk.TransactionOutput(script, TAPROOT_AMOUNT)
    precomputed = pbk.PrecomputedTransactionData(tx, [spent])
    trace = pbk.debug_script(
        script, TAPROOT_AMOUNT, tx, 0, pbk.ScriptVerificationFlags.ALL, precomputed
    )

    assert trace.valid is True
    # Key-path spends verify the signature outside the script interpreter,
    # so only the (empty) scriptSig and the witness-program scriptPubkey are
    # traced -- no TAPSCRIPT execution.
    assert all(e.sig_version == pbk.SigVersion.BASE for e in trace.executions)


@requires_trace
def test_debug_failing_spend_reports_invalid():
    # Verify the legacy tx against a script it does not spend.
    wrong = pbk.ScriptPubkey(bytes.fromhex(SEGWIT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    trace = pbk.debug_script(wrong, 0, tx, 0, PRE_TAPROOT)

    assert trace.valid is False
    # The scripts still ran; the failure is the overall verdict.
    text = trace.format()
    assert "INVALID" in text
    assert "verification failed" in text


@requires_trace
def test_format_returns_readable_text():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    text = pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT).format()

    assert "VALID" in text
    assert "OP_CHECKSIG" in text
    assert "OP_DUP" in text
    assert str(text) == pbk.debug_script(script, 0, tx, 0, PRE_TAPROOT).format()


@requires_trace
def test_script_trace_context_manager_scopes_the_callback():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))

    frames = []
    with pbk.script_trace(frames.append):
        assert pbk.verify_script(script, 0, tx, 0, PRE_TAPROOT)
    assert len(frames) == 11

    # After the context exits the callback is gone: no new frames captured.
    count_after = len(frames)
    assert pbk.verify_script(script, 0, tx, 0, PRE_TAPROOT)
    assert len(frames) == count_after


@requires_trace
def test_registering_replaces_previous_callback():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))

    first, second = [], []
    with pbk.script_trace(first.append):
        with pbk.script_trace(second.append):
            assert pbk.verify_script(script, 0, tx, 0, PRE_TAPROOT)
    # Only the innermost (most recently registered) callback fires.
    assert len(second) == 11
    assert first == []


@requires_trace
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_callback_exception_does_not_abort_verification():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))

    def boom(_frame):
        raise RuntimeError("handler blew up")

    # Exceptions in the callback are reported unraisably (like the other
    # kernel callbacks), not propagated, and verification still returns the
    # correct verdict.
    with pbk.script_trace(boom):
        assert pbk.verify_script(script, 0, tx, 0, PRE_TAPROOT) is True
