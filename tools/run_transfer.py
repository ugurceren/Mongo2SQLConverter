"""Headless transfer for Windows Task Scheduler."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.logutil import configure_logging, get_logger  # noqa: E402
from core.run_job import run_transfer_job  # noqa: E402
from core.settings import SCHEDULE_MODES, parse_table_renames  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a Mongo collection to SQL using config.local.yaml prefs."
    )
    parser.add_argument("--collection", required=True, help="MongoDB collection name")
    parser.add_argument(
        "--mode",
        choices=SCHEDULE_MODES,
        default=None,
        help="auto: full if the root table is empty, incremental otherwise. Default: saved pref.",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Root SQL table for this job. Pins the destination; child names follow unless --rename.",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="SQL schema for this job. Default: saved pref or mssql.schema.",
    )
    parser.add_argument(
        "--rename",
        action="append",
        default=None,
        metavar="PATH=TABLE",
        help="Child SQL table override (repeatable). Mongo path=SqlTable. Used with --table.",
    )
    parser.add_argument("--batch", type=int, default=None, help="Write batch size (documents)")
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Profile sample size. 0 = full scan. Does not change how many docs are written.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    log = get_logger()
    try:
        names = parse_table_renames(args.rename) if args.rename is not None else None
        result = run_transfer_job(
            args.collection,
            mode=args.mode,
            batch=args.batch,
            sample=args.sample,
            table=args.table,
            schema=args.schema,
            table_names=names,
        )
    except Exception as exc:
        log.exception("görev başarısız collection=%s error=%s", args.collection, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{result.collection}: {result.requested_mode} -> {result.mode}, "
        f"{result.stats.documents} documents, {result.stats.total_rows} rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
