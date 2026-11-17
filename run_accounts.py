"""Run the Barchart posting batch sequentially for accounts.json."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACCOUNTS = ROOT / "accounts.json"
LOGIN = ROOT / "login.py"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def state_name(email: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", email).strip("._")
    if not value:
        raise RuntimeError(f"invalid email/state name: {email!r}")
    return value


def link_count(name: str) -> int:
    path = ROOT / "state" / name / "posted-links.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    return len(data)


def main() -> int:
    if not ACCOUNTS.exists():
        raise RuntimeError(
            "missing accounts.json; copy accounts.example.json and enter your accounts"
        )
    accounts = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("accounts.json must contain a non-empty JSON array")

    posts = int(os.environ.get("BARCHART_POSTS_PER_ACCOUNT", "5"))
    defaults = load_env_file(ROOT / ".env")

    for index, account in enumerate(accounts, start=1):
        email = str(account.get("email", "")).strip()
        if not email:
            raise RuntimeError(f"account #{index} is missing email")
        name = str(account.get("state_name") or state_name(email))
        saved = link_count(name)
        if saved >= posts:
            print(
                f"\n=== Account {index}/{len(accounts)}: {email} "
                f"(already {saved}/{posts}, skipped) ===",
                flush=True,
            )
            continue
        env = os.environ.copy()
        env.update(defaults)
        env["BARCHART_EMAIL"] = email
        env["BARCHART_STATE_NAME"] = name
        env["BARCHART_NO_WAIT"] = "1"
        if account.get("password"):
            env["BARCHART_PASSWORD"] = str(account["password"])
        if account.get("totp_secret"):
            env["BARCHART_TOTP_SECRET"] = str(account["totp_secret"])

        print(f"\n=== Account {index}/{len(accounts)}: {email} ===", flush=True)
        result = subprocess.run(
            [sys.executable, str(LOGIN), "--batch", str(posts), "--fresh"],
            cwd=ROOT,
            env=env,
        )
        saved = link_count(name)
        if result.returncode != 0 or saved < posts:
            print(
                f"STOP: {email} completed {saved}/{posts} links "
                f"(exit code {result.returncode}).",
                flush=True,
            )
            return 1
        print(f"DONE: {email} completed {saved}/{posts} links.", flush=True)

    print(f"\nAll {len(accounts)} accounts completed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        raise SystemExit(1)
