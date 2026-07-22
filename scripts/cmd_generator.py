#!/usr/bin/env python3
"""Turn a mempool.space transaction URL into a manual_debug.py command.

Fetches the transaction and its inputs' prevouts from mempool.space, then
prints a ready-to-run `manual_debug.py` command (raw tx + one --spent per
input). This is the only step that touches the network; the printed command
runs fully offline.

Usage:
    python scripts/cmd_generator.py <mempool-tx-url-or-txid>

Examples:
    python scripts/cmd_generator.py https://mempool.space/tx/04b6e433...75dc
    python scripts/cmd_generator.py 04b6e4338c7d555bd77741d5b9ee38432747cd40c4d41637ca3d51e138df75dc
    python scripts/cmd_generator.py https://mempool.space/signet/tx/<txid>

The command is printed to stdout; a short summary goes to stderr, so you can
capture just the command with:  python scripts/cmd_generator.py <url> > cmd.sh
"""

import json
import re
import sys
import urllib.parse
import urllib.request

PYTHON = "python"  # interpreter shown in the generated command (e.g. .venv/bin/python)
MANUAL_DEBUG = "scripts/manual_debug.py"


def parse_target(arg):
    """Resolve a tx URL or bare txid to (api_base, txid).

    Handles network-prefixed mempool paths (/testnet/tx/..., /signet/tx/...)
    and self-hosted instances by reusing the URL's host and path prefix.
    """
    if re.fullmatch(r"[0-9a-fA-F]{64}", arg):
        return "https://mempool.space/api", arg.lower()

    url = urllib.parse.urlparse(arg if "//" in arg else "https://" + arg)
    parts = [p for p in url.path.split("/") if p]
    if "tx" not in parts or parts.index("tx") + 1 >= len(parts):
        raise SystemExit(f"could not find a txid in: {arg}")
    idx = parts.index("tx")
    txid = parts[idx + 1]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        raise SystemExit(f"not a valid txid: {txid}")

    base = f"{url.scheme}://{url.netloc}"
    prefix = parts[:idx]  # e.g. ['testnet'] or ['signet'] or []
    if prefix:
        base += "/" + "/".join(prefix)
    return base + "/api", txid.lower()


def get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/cmd_generator.py <mempool-tx-url-or-txid>")

    api_base, txid = parse_target(sys.argv[1])

    info = json.loads(get(f"{api_base}/tx/{txid}"))
    raw_hex = get(f"{api_base}/tx/{txid}/hex").decode().strip()

    vin = info["vin"]
    if vin and vin[0].get("is_coinbase"):
        raise SystemExit("coinbase transactions have no input scripts to debug")

    spent = []
    for i, v in enumerate(vin):
        prevout = v.get("prevout")
        if not prevout:
            raise SystemExit(f"input {i} has no prevout data on mempool.space")
        spent.append((prevout["scriptpubkey"], prevout["value"], prevout["scriptpubkey_type"]))

    # Summary -> stderr, so stdout stays a clean, copy-pasteable command.
    print(f"tx {txid}: {len(vin)} input(s)", file=sys.stderr)
    for i, (script, amount, kind) in enumerate(spent):
        print(f"  input {i}: {kind} worth {amount} sat -> {script}", file=sys.stderr)
    print(file=sys.stderr)

    lines = [f"{PYTHON} {MANUAL_DEBUG}", f"  {raw_hex}"]
    lines += [f"  --spent {script} {amount}" for script, amount, _kind in spent]
    print(" \\\n".join(lines))


if __name__ == "__main__":
    main()
