"""Command-line entry point.

    python run.py sync        # pull the lead sheet + WebEngage exports, assign, check follow-ups
    python run.py morning     # send today's morning notes now
    python run.py eod         # send today's end-of-day report now
    python run.py pull-clients  # no-deposit clients + ageing of every lead from the back-office SQL Server, then a full sync
    python run.py export-sheet  # publish the prioritised customer list (CSV + Google Sheet if configured)
    python run.py market      # refresh live market notes now (NSE, Yahoo, ET / Google News) with sanity checks
    python run.py loop        # run continuously (sync every few minutes, morning note and EOD on schedule)
"""
from __future__ import annotations

import argparse
import json
import logging

from wepipeline import jobs
from wepipeline.db import open_ctx


def main() -> None:
    parser = argparse.ArgumentParser(description="Growth team follow-up pipeline")
    parser.add_argument("command", choices=["sync", "morning", "eod", "tick", "loop", "pull-clients", "export-sheet", "market"])
    parser.add_argument("--config", help="path to config.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ctx = open_ctx(args.config)
    if args.command == "loop":
        jobs.loop(ctx)
        return
    if args.command == "pull-clients":
        from wepipeline.clientdb import refresh_from_client_db

        print(json.dumps({"client_db": refresh_from_client_db(ctx), "sync": jobs.sync(ctx)}, indent=2, default=str))
        return
    if args.command == "market":
        from wepipeline.market import live_notes, refresh

        res = refresh(ctx)
        data = live_notes(ctx)
        print(json.dumps({**res, "notes": [n["text"] for n in data["notes"]],
                          "checks": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in data.get("checks", [])]},
                         indent=2, ensure_ascii=False))
        return
    if args.command == "export-sheet":
        from wepipeline.sheet_export import export

        print(json.dumps(export(ctx), indent=2, default=str))
        return
    action = {"sync": jobs.sync, "morning": jobs.send_morning_notes, "eod": jobs.send_eod_reports, "tick": jobs.tick}[args.command]
    print(json.dumps(action(ctx), indent=2, default=str))


if __name__ == "__main__":
    main()
