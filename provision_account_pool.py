import argparse
import json
import os
import secrets
from pathlib import Path
import sys


def safe_token(length=10):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def default_accounts(count):
    accounts = []
    for index in range(count):
        slot = index + 1
        label = f"r{slot}"
        user = f"wagent{safe_token(8)}{slot}"
        password = f"wg{safe_token(12)}"
        accounts.append({"label": label, "user": user, "password": password})
    return accounts


def detect_game_dir(base_dir):
    candidates = [base_dir / "mygame", base_dir]
    for candidate in candidates:
        if (candidate / "server" / "conf" / "settings.py").exists():
            return candidate
    raise RuntimeError("Could not locate Evennia game directory with server/conf/settings.py")


def bootstrap_evennia(game_dir):
    game_dir_str = str(game_dir)
    os.chdir(game_dir_str)
    if game_dir_str not in sys.path:
        sys.path.insert(0, game_dir_str)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.conf.settings")
    import django
    import evennia

    django.setup()
    evennia._init()


def ensure_account(user, password):
    from evennia.accounts.models import AccountDB
    from evennia.utils import create

    existing = AccountDB.objects.filter(username__iexact=user).first()
    if existing:
        existing.set_password(password)
        existing.save(update_fields=["password"])
        return {"created": False, "ok": True}

    account = create.create_account(user, None, password)
    return {"created": bool(account), "ok": bool(account)}


def write_pool_file(path, accounts, host, port):
    payload = {
        "format": "wagent-account-pool-v1",
        "host": host,
        "port": port,
        "accounts": accounts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def parse_args():
    parser = argparse.ArgumentParser(description="Create Evennia accounts and write a reusable Wagent account pool file.")
    parser.add_argument("--pool-file", default="wagent_account_pool.local.json")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--game-dir", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    pool_path = Path(args.pool_file).resolve()
    game_dir = Path(args.game_dir).resolve() if args.game_dir else detect_game_dir(Path(__file__).resolve().parent)
    bootstrap_evennia(game_dir)
    accounts = default_accounts(args.count)

    results = []
    for account in accounts:
        result = ensure_account(account["user"], account["password"])
        results.append((account, result))

    failed = [item for item in results if not item[1].get("ok")]
    if failed:
        for account, result in failed:
            print(f"FAILED {account['label']} {account['user']}")
        raise SystemExit(1)

    write_pool_file(pool_path, [account for account, _ in results], args.host, args.port)
    print(f"Wrote {pool_path}")
    for account, result in results:
        state = "created" if result.get("created") else "verified"
        print(f"{account['label']}: {account['user']} ({state})")


if __name__ == "__main__":
    main()