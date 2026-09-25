"""Headless transfer for Windows Task Scheduler.

Exit codes: 0 finished clean, 2 finished but some documents were rejected or
values clipped / nulled (see the rejects file), 1 failed. A stopped or failed
run continues from its checkpoint the next time it starts.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.logutil import configure_logging, get_logger  # noqa: E402
from core.rejects import read_lines  # noqa: E402
from core.run_job import run_preflight_job, run_transfer_job  # noqa: E402
from core.settings import SCHEDULE_MODES, parse_table_renames  # noqa: E402
from core.writer import LockBusy  # noqa: E402

EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_WITH_REJECTS = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a Mongo collection to SQL using config.local.yaml prefs."
    )
    parser.add_argument("--collection", required=True, help="MongoDB collection name")
    parser.add_argument(
        "--mode",
        choices=SCHEDULE_MODES,
        default=None,
        help="auto: resume an unfinished full load, else incremental once the root table has rows. "
        "Default: saved pref.",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Root SQL table for this job. Pins the destination; child names follow unless --rename.",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="SQL schema for this job. Default: mssql.schema from Bağlantılar.",
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
        help="Profile sample size. 0 = full scan (capped on big collections unless --full-profile). "
        "Does not change how many docs are written.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore the checkpoint and start a new full load (documents already written are replaced).",
    )
    parser.add_argument(
        "--max-rejects",
        type=int,
        default=None,
        metavar="N",
        help="Stop when more than N documents are rejected. Default: loader.max_rejects (1000).",
    )
    parser.add_argument(
        "--full-profile",
        action="store_true",
        help="With --sample 0, really profile every document even on a big collection.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check drivers, rights, log settings and measure speed on a small sample; write nothing.",
    )
    return parser


def _preflight(args: argparse.Namespace, names: dict[str, str] | None) -> int:
    report = run_preflight_job(
        args.collection,
        mode=args.mode,
        sample=args.sample,
        table=args.table,
        schema=args.schema,
        table_names=names,
        full_profile=args.full_profile,
    )
    for line in report.lines():
        print(line)
    return EXIT_CLEAN


def _utf8_output() -> None:
    """Redirected output (Task Scheduler, `> log.txt`) is cp1252 by default and would choke on Turkish text."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    configure_logging()
    args = build_parser().parse_args(argv)
    log = get_logger()
    try:
        names = parse_table_renames(args.rename) if args.rename is not None else None
        if args.preflight_only:
            return _preflight(args, names)
        result = run_transfer_job(
            args.collection,
            mode=args.mode,
            batch=args.batch,
            sample=args.sample,
            table=args.table,
            schema=args.schema,
            table_names=names,
            restart=args.restart,
            max_rejects=args.max_rejects,
            full_profile=args.full_profile,
        )
    except LockBusy as exc:
        log.error("görev başlamadı collection=%s: %s", args.collection, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:
        log.exception("görev başarısız collection=%s error=%s", args.collection, exc)
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    stats = result.stats
    state = "stopped" if stats.stopped else "done"
    print(
        f"{result.collection}: {result.requested_mode} -> {result.mode} ({state}), "
        f"{stats.written} documents written, {stats.total_rows} rows, "
        f"{stats.rejected} rejected, {stats.truncated} clipped, {stats.nulled} nulled"
    )
    if result.note:
        print(f"  {result.note}")
    if stats.widened:
        print(f"  widened: {', '.join(stats.widened)}")
    if stats.rejects_path:
        print(f"  rejects file: {stats.rejects_path}")
        for entry in read_lines(stats.rejects_path, limit=5):
            where = ".".join(part for part in (entry.get("table"), entry.get("column")) if part)
            reason = (entry.get("error") or entry.get("detail") or "")[:160]
            print(f"    {entry.get('kind')} {entry.get('_id')} {where} {reason}".rstrip())
    return EXIT_CLEAN if stats.clean else EXIT_WITH_REJECTS


if __name__ == "__main__":
    raise SystemExit(main())
