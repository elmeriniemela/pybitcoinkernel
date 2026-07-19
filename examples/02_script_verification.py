#!/usr/bin/env python3
"""Verifying scripts with Bitcoin Core's script interpreter.

verify_script() answers: "does input N of this transaction validly spend
this script pubkey?" — using the exact consensus code Bitcoin Core uses.
The vectors below are real mainnet transactions (they are also used in
Bitcoin Core's own kernel test suite).

Run:  python examples/02_script_verification.py
"""

import pybitcoinkernel as pbk

# Legacy P2PKH spend:
# tx aca326a724eda9a461c10a876534ecd5ae7b27f10f26c3862fb996f80ea2d45d
LEGACY_SCRIPT = bytes.fromhex("76a9144bfbaf6afb76cc5771bc6404810d1cc041a6933988ac")
LEGACY_TX = bytes.fromhex(
    "02000000013f7cebd65c27431a90bba7f796914fe8cc2ddfc3f2cbd6f7e5f2fc854534da"
    "95000000006b483045022100de1ac3bcdfb0332207c4a91f3832bd2c2915840165f876ab"
    "47c5f8996b971c3602201c6c053d750fadde599e6f5c4e1963df0f01fc0d97815e8157e3"
    "d59fe09ca30d012103699b464d1d8bc9e47d4fb1cdaa89a1c5783d68363c4dbc4b524ed3"
    "d857148617feffffff02836d3c01000000001976a914fc25d6d5c94003bf5b0c7b640a24"
    "8e2c637fcfb088ac7ada8202000000001976a914fbed3d9b11183209a57999d54d59f67c"
    "019e756c88ac6acb0700"
)

# Segwit P2WSH spend (2-of-3 multisig):
# tx 1a3e89644985fbbb41e0dcfe176739813542b5937003c46a07de1e3ee7a4a7f3
SEGWIT_SCRIPT = bytes.fromhex(
    "0020701a8d401c84fb13e6baf169d59684e17abd9fa216c8cc5b9fc63d622ff8c58d"
)
SEGWIT_TX = bytes.fromhex(
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
SEGWIT_AMOUNT = 18_393_430  # the amount of the spent output, in satoshis

# Taproot key-path spend:
# tx 33e794d097969002ee05d336686fc03c9e15a597c1b9827669460fac98799036
TAPROOT_SCRIPT = bytes.fromhex(
    "5120339ce7e165e67d93adb3fef88a6d4beed33f01fa876f05a225242b82a631abc0"
)
TAPROOT_TX = bytes.fromhex(
    "01000000000101d1f1c1f8cdf6759167b90f52c9ad358a369f95284e841d7a2536cef31c"
    "0549580100000000fdffffff020000000000000000316a2f49206c696b65205363686e6f"
    "7272207369677320616e6420492063616e6e6f74206c69652e204062697462756734329e"
    "06010000000000225120a37c3903c8d0db6512e2b40b0dffa05e5a3ab73603ce8c9c4b77"
    "71e5412328f90140a60c383f71bac0ec919b1d7dbc3eb72dd56e7aa99583615564f9f99b"
    "8ae4e837b758773a5b2e4c51348854c8389f008e05029db7f464a5ff2e01d5e6e626174a"
    "ffd30a00"
)
TAPROOT_AMOUNT = 88_480


def main() -> None:
    flags = pbk.ScriptVerificationFlags

    # --- Legacy (pre-segwit): the amount is not signed, pass 0. ---------
    script = pbk.ScriptPubkey(LEGACY_SCRIPT)
    tx = pbk.Transaction(LEGACY_TX)
    ok = pbk.verify_script(script, 0, tx, 0, flags.ALL ^ flags.TAPROOT)
    print(f"legacy P2PKH spend valid:   {ok}")

    # Verifying against a script the input does not spend fails cleanly.
    wrong = pbk.ScriptPubkey(SEGWIT_SCRIPT)
    ok = pbk.verify_script(wrong, 0, tx, 0, flags.ALL ^ flags.TAPROOT)
    print(f"spend of the wrong script:  {ok}")

    # --- Segwit: the spent output's amount is committed to by the
    # signature, so it must be passed in. ---------------------------------
    script = pbk.ScriptPubkey(SEGWIT_SCRIPT)
    tx = pbk.Transaction(SEGWIT_TX)
    ok = pbk.verify_script(script, SEGWIT_AMOUNT, tx, 0, flags.ALL ^ flags.TAPROOT)
    print(f"segwit multisig valid:      {ok}")
    ok = pbk.verify_script(script, SEGWIT_AMOUNT + 1, tx, 0, flags.ALL ^ flags.TAPROOT)
    print(f"...with a wrong amount:     {ok}")

    # --- Taproot: signatures commit to *all* spent outputs, so
    # verification needs PrecomputedTransactionData built with them. ------
    script = pbk.ScriptPubkey(TAPROOT_SCRIPT)
    tx = pbk.Transaction(TAPROOT_TX)
    spent_outputs = [pbk.TransactionOutput(script, TAPROOT_AMOUNT)]
    precomputed = pbk.PrecomputedTransactionData(tx, spent_outputs)
    ok = pbk.verify_script(script, TAPROOT_AMOUNT, tx, 0, flags.ALL, precomputed)
    print(f"taproot key-path valid:     {ok}")

    # Forgetting the precomputed data with the TAPROOT flag set is an
    # error (not merely "invalid"):
    try:
        pbk.verify_script(script, TAPROOT_AMOUNT, tx, 0, flags.ALL)
    except ValueError as e:
        print(f"taproot without precomputed data raises: ValueError({e})")


if __name__ == "__main__":
    main()
