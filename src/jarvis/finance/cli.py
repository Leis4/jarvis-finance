"""Local aggregate audit and safe backup/restore commands."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from jarvis.finance.audit_report import build_audit_report
from jarvis.finance.backup import create_backup, restore_backup
from jarvis.finance.db import FinanceDB
from jarvis.finance.production_intake import write_folder_intake
from jarvis.finance.statement_review import conflict_queue, statement_dry_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--db", required=True, type=Path)
    report.add_argument("--start", required=True, type=date.fromisoformat)
    report.add_argument("--end", required=True, type=date.fromisoformat)
    report.add_argument("--currency", default="RUB")
    report.add_argument("--account")
    report.add_argument("--category")
    report.add_argument("--output", type=Path)
    backup = sub.add_parser("backup")
    backup.add_argument("--db", required=True, type=Path)
    backup.add_argument("--output-dir", required=True, type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--archive", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    review = sub.add_parser("conflict-queue")
    review.add_argument("--db", required=True, type=Path)
    review.add_argument("--output", required=True, type=Path)
    dry_run = sub.add_parser("statement-dry-run")
    dry_run.add_argument("--db", required=True, type=Path)
    dry_run.add_argument("--file", required=True, type=Path)
    dry_run.add_argument("--metadata", required=True, type=Path)
    dry_run.add_argument("--output", required=True, type=Path)
    intake = sub.add_parser("statement-intake")
    intake.add_argument("--folder", required=True, type=Path)
    intake.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "statement-intake":
        if not args.folder.is_dir():
            parser.error("Statement folder does not exist")
        payload = write_folder_intake(args.folder, args.output_dir)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "restore":
        restore_backup(args.archive, args.destination)
        print("Restore verified")
        return 0
    if not args.db.is_file():
        parser.error("Database does not exist; audit never creates an empty database")
    if args.command == "backup":
        result = create_backup(args.db, args.output_dir)
        print(result.path)
        return 0
    if args.command in {"conflict-queue", "statement-dry-run"}:
        payload = (conflict_queue(args.db) if args.command == "conflict-queue" else
                   statement_dry_run(args.db, args.file, json.loads(args.metadata.read_text(encoding="utf-8"))))
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
        print(payload["status"])
        return 0
    payload = build_audit_report(FinanceDB(args.db), args.start, args.end, args.currency, args.account, args.category)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
