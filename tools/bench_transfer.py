"""
Offline speed check for the loader's CPU side: BSON decode and flattening.

Builds synthetic conversation documents (tests/fixtures.py), times the
flattener of the last release, the current reference one and the compiled
one, and checks that the compiled rows equal the reference rows. SQL and
network speed are not measured here; the pre-flight check does that against
real servers (`tools/run_transfer.py --preflight-only`).

    python tools/bench_transfer.py --docs 20000 --nesting hybrid
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bson  # noqa: E402

from core.convert import Flattener  # noqa: E402
from core.reader import LENIENT  # noqa: E402
from core.transfer import flatten_document  # noqa: E402
from tests import legacy_reference as legacy  # noqa: E402
from tests.fixtures import NESTINGS, canon, conversation, plan_for  # noqa: E402


def timed(step, items):
    started = time.perf_counter()
    out = [step(item) for item in items]
    return time.perf_counter() - started, out


def compare(docs, reference, compiled) -> tuple[int, int, int]:
    """(equal, different, skipped) — skipped: longer than the profile saw, so the writer widens."""
    equal = different = skipped = 0
    for (key, root, children, truncated), flat in zip(reference, compiled):
        if truncated or flat.overflow:
            skipped += 1
            continue
        same = (
            flat.reject is None
            and canon(flat.key) == canon(key)
            and canon(flat.root) == canon(root)
            and canon(flat.children) == canon(children)
        )
        if same:
            equal += 1
        else:
            different += 1
    return equal, different, skipped


def bench(nesting: str, count: int, profile_docs: int, seed: int, with_legacy: bool) -> int:
    rng = random.Random(seed)
    raw = [bson.encode(conversation(rng, index)) for index in range(count)]
    decode_seconds, docs = timed(lambda data: bson.decode(data, codec_options=LENIENT), raw)
    plan, key_type = plan_for(docs, nesting, profile_docs=profile_docs)
    tables = 1 + len(plan["children"])

    results: list[tuple[str, float]] = [("BSON çözme", decode_seconds)]
    if with_legacy:
        seconds, _ = timed(lambda doc: legacy.flatten_document(doc, plan, key_type), docs)
        results.append(("düzleştirme, önceki sürüm", seconds))
    seconds, reference = timed(lambda doc: flatten_document(doc, plan, key_type), docs)
    results.append(("düzleştirme, referans", seconds))
    flattener = Flattener(plan, key_type)
    seconds, compiled = timed(flattener.flatten, docs)
    results.append(("düzleştirme, derlenmiş", seconds))

    rows = sum(flat.rows for flat in compiled)
    kilobytes = sum(len(data) for data in raw) / count / 1024
    print(f"\n{nesting}: {count:,} belge, ~{kilobytes:.1f} KB/belge, {tables} tablo, {rows / count:.1f} satır/belge")
    print(f"  {'aşama':<28}{'belge/sn':>12}{'satır/sn':>12}{'süre sn':>10}")
    for label, seconds in results:
        rate = count / seconds if seconds else float("inf")
        print(f"  {label:<28}{rate:>12,.0f}{rate * rows / count:>12,.0f}{seconds:>10.2f}")

    equal, different, skipped = compare(docs, reference, compiled)
    print(f"  çıktı: {equal:,} aynı, {different:,} farklı, {skipped:,} genişletme gerektiren (karşılaştırılmadı)")
    return different


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time BSON decoding and flattening on synthetic documents.")
    parser.add_argument("--docs", type=int, default=20_000, help="documents to generate (default 20000)")
    parser.add_argument("--nesting", choices=[*NESTINGS, "all"], default="hybrid")
    parser.add_argument("--profile", type=int, default=2000, help="documents the plan is profiled on")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-legacy", action="store_true", help="skip the slow previous-release flattener")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    nestings = NESTINGS if args.nesting == "all" else (args.nesting,)
    different = sum(bench(n, max(1, args.docs), args.profile, args.seed, not args.no_legacy) for n in nestings)
    return 1 if different else 0


if __name__ == "__main__":
    raise SystemExit(main())
