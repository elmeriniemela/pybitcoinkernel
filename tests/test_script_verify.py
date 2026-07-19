"""Script verification tests using vectors from Bitcoin Core's kernel
test suite (src/test/kernel/test_kernel.cpp)."""

import pytest

import pybitcoinkernel as pbk

# Legacy transaction aca326a724eda9a461c10a876534ecd5ae7b27f10f26c3862fb996f80ea2d45d
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

# Segwit transaction 1a3e89644985fbbb41e0dcfe176739813542b5937003c46a07de1e3ee7a4a7f3
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

# Taproot transaction 33e794d097969002ee05d336686fc03c9e15a597c1b9827669460fac98799036
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


def test_legacy_verify():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    assert pbk.verify_script(
        script, 0, tx, 0, pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    )


def test_legacy_verify_with_precomputed():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    precomputed = pbk.PrecomputedTransactionData(tx)
    assert pbk.verify_script(
        script,
        0,
        tx,
        0,
        pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT,
        precomputed,
    )


def test_legacy_verify_method():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    assert script.verify(0, tx, 0, int(flags))


def test_out_of_range_input_index_raises():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    with pytest.raises(IndexError):
        pbk.verify_script(script, 0, tx, tx.n_inputs, flags)


def test_unknown_flags_raise():
    script = pbk.ScriptPubkey(bytes.fromhex(LEGACY_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    with pytest.raises(ValueError):
        script.verify(0, tx, 0, 1 << 30)


def test_wrong_script_fails():
    # Verifying against a script the transaction does not spend must fail.
    wrong_script = pbk.ScriptPubkey(bytes.fromhex(SEGWIT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(LEGACY_SPENDING_TX))
    flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    assert not pbk.verify_script(wrong_script, 0, tx, 0, flags)


def test_segwit_verify():
    script = pbk.ScriptPubkey(bytes.fromhex(SEGWIT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(SEGWIT_SPENDING_TX))
    flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    assert pbk.verify_script(script, SEGWIT_AMOUNT, tx, 0, flags)


def test_segwit_wrong_amount_fails():
    script = pbk.ScriptPubkey(bytes.fromhex(SEGWIT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(SEGWIT_SPENDING_TX))
    flags = pbk.ScriptVerificationFlags.ALL ^ pbk.ScriptVerificationFlags.TAPROOT
    assert not pbk.verify_script(script, SEGWIT_AMOUNT + 1, tx, 0, flags)


def test_taproot_verify():
    script = pbk.ScriptPubkey(bytes.fromhex(TAPROOT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(TAPROOT_SPENDING_TX))
    spent_output = pbk.TransactionOutput(script, TAPROOT_AMOUNT)
    precomputed = pbk.PrecomputedTransactionData(tx, [spent_output])
    assert pbk.verify_script(
        script, TAPROOT_AMOUNT, tx, 0, pbk.ScriptVerificationFlags.ALL, precomputed
    )


def test_precomputed_spent_outputs_count_mismatch_raises():
    script = pbk.ScriptPubkey(bytes.fromhex(TAPROOT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(TAPROOT_SPENDING_TX))  # 1 input
    spent = pbk.TransactionOutput(script, TAPROOT_AMOUNT)
    with pytest.raises(ValueError):
        pbk.PrecomputedTransactionData(tx, [spent, spent])


def test_taproot_without_precomputed_raises():
    script = pbk.ScriptPubkey(bytes.fromhex(TAPROOT_SPENT_SCRIPT))
    tx = pbk.Transaction(bytes.fromhex(TAPROOT_SPENDING_TX))
    with pytest.raises(ValueError):
        pbk.verify_script(script, TAPROOT_AMOUNT, tx, 0, pbk.ScriptVerificationFlags.ALL)
