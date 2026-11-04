"""Create a bearer key for a loopback Provena gateway in local-store mode.

The key is bound to one tenant, one workspace and one person: the gateway
forwards that scope as verified claims and the store confines the key to it.

Two private files are written to a directory outside this repository and only
their paths are printed:

  <out-dir>/<name>.key        the raw bearer token, to paste into the client once
  <out-dir>/<name>.keys.json  the PROVENA_API_KEYS value: key id, tenant, role,
                              principal, bound scope and the SHA-256 of the
                              token (never the token)

Standard library only. Example (PowerShell):

  python scripts/local_gateway_key.py --out-dir "$env:LOCALAPPDATA\\Valcairn\\local-memory" `
      --tenant valcairn-local --principal person-1 --workspace <workspace id> --user <user id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES = ("viewer", "editor", "admin")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", required=True, help="private directory outside the repository")
    parser.add_argument("--tenant", required=True, help="tenant id the key is scoped to")
    parser.add_argument("--principal", required=True, help="principal id recorded as the author of every memory")
    parser.add_argument("--workspace", required=True, help="workspace id the key is bound to (the client's workspace)")
    parser.add_argument("--user", required=True, help="user id the key is bound to (the signed-in person)")
    parser.add_argument("--role", default="editor", choices=ROLES, help="gateway role (default: editor)")
    parser.add_argument("--name", default="valcairn-local-memory", help="file name stem and key id (default: valcairn-local-memory)")
    args = parser.parse_args()
    for label, value in (("--tenant", args.tenant), ("--principal", args.principal), ("--workspace", args.workspace), ("--user", args.user)):
        if not value.strip() or value != value.strip():
            print(f"{label} must be a non-empty value without surrounding spaces", file=sys.stderr)
            return 2

    out_dir = Path(args.out_dir).expanduser().resolve()
    if out_dir == REPO_ROOT or REPO_ROOT in out_dir.parents:
        print("refusing to write a key inside the repository; choose a private directory outside it", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    key_file = out_dir / f"{args.name}.key"
    keys_json = out_dir / f"{args.name}.keys.json"
    if key_file.exists() or keys_json.exists():
        print(f"refusing to overwrite an existing key in {out_dir}; remove it first or pick another --name", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32)
    record = {
        "key_id": args.name,
        "tenant_id": args.tenant,
        "role": args.role,
        "principal_id": args.principal,
        "scope": {"workspace_id": args.workspace, "user_id": args.user},
        "hashed_key": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "description": f"local-store gateway key created {datetime.now(UTC).isoformat(timespec='seconds')}",
    }
    for path, text in ((key_file, token + "\n"), (keys_json, json.dumps([record]) + "\n")):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(key_file)
    print(keys_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
