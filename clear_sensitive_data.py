#!/usr/bin/env python3
"""Clear sensitive API credentials from btc_bot.db."""
import argparse
from config import config
from database import Database

ap = argparse.ArgumentParser()
ap.add_argument("--yes", action="store_true", help="confirm deletion")
args = ap.parse_args()
if not args.yes:
    print("This deletes rows from api_credentials only. Re-run with --yes to confirm.")
    raise SystemExit(1)

db = Database(config.DB_PATH)
count = db.clear_api_creds()
print(f"Deleted {count} row(s) from api_credentials in {config.DB_PATH}")
