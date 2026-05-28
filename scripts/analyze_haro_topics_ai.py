#!/usr/bin/env python3
"""AI topic analysis of stored HARO journalist requests (article types, asks, themes)."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from openai import OpenAI
from sqlalchemy import select

from app.config import settings
from app.db import get_session, init_db
from app.models import HaroRequest

BATCH_SIZE = 35
TEXT_MAX = 520

SYSTEM = """You classify HARO journalist queries for market research. Be consistent across batches.
Return ONLY valid JSON: {"items": [ ... ]} with one object per request id given.
Each item MUST include "id" (integer, matching the ID from the user message).

Each item MUST use exactly these enum values where applicable:

article_format (pick one):
  expert_commentary, how_to_guide, personal_story, product_review_roundup,
  interview_profile, data_research_study, listicle_roundup, opinion_op_ed,
  local_business_spotlight, gift_guide, podcast_guest, book_author,
  legal_medical_expert, travel_destination, finance_money, other

ask_type (pick one — what the journalist wants from sources):
  expert_quotes, consumer_experiences, business_owner_founder, healthcare_professional,
  product_samples_reviews, personal_anecdote, data_statistics, case_study,
  diverse_voices, celebrity_notable, academic_researcher, attorney, real_estate,
  travel_experience, general_commentary, other

reporter_medium (pick one):
  online_publication, print_magazine, newspaper, podcast, tv_radio, newsletter,
  blog_independent, book, unknown

topic_label: 2-6 words, lowercase, specific subject (e.g. "credit card debt", "menopause symptoms")

confidence: 0.0-1.0 how clear the ask is from the text"""


def _stratified_sample(rows: list[HaroRequest], n: int, seed: int) -> list[HaroRequest]:
    if n <= 0 or len(rows) <= n:
        return list(rows)
    rng = random.Random(seed)
    by_cat: dict[str, list[HaroRequest]] = defaultdict(list)
    for r in rows:
        by_cat[(r.category or "unknown").strip() or "unknown"].append(r)
    total = len(rows)
    picked: list[HaroRequest] = []
    for cat, items in by_cat.items():
        share = max(1, round(n * len(items) / total))
        share = min(share, len(items))
        picked.extend(rng.sample(items, share))
    if len(picked) > n:
        rng.shuffle(picked)
        picked = picked[:n]
    elif len(picked) < n:
        rest = [r for r in rows if r not in picked]
        rng.shuffle(rest)
        picked.extend(rest[: n - len(picked)])
    return picked


def _batch_prompt(batch: list[HaroRequest]) -> str:
    lines = []
    for r in batch:
        text = (r.request_text or "").strip().replace("\n", " ")
        if len(text) > TEXT_MAX:
            text = text[:TEXT_MAX] + "…"
        outlet = (r.outlet or "").strip() or "?"
        cat = (r.category or "").strip() or "?"
        lines.append(
            "ID %s | haro_section=%s | outlet=%s | text: %s" % (r.id, cat, outlet[:80], text)
        )
    return (
        "Classify each request below. Return one JSON item per ID (include id on every item):\n\n"
        + "\n\n".join(lines)
    )


def _classify_batch(client: OpenAI, batch: list[HaroRequest], model: str) -> list[dict]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _batch_prompt(batch)},
        ],
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    by_id = {r.id: r for r in batch}
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid not in by_id:
            continue
        out.append(item)
    return out


def _counter_report(labels: Counter, title: str, top_n: int = 15) -> str:
    lines = ["## %s\n" % title]
    total = sum(labels.values()) or 1
    for name, count in labels.most_common(top_n):
        pct = 100.0 * count / total
        lines.append("- **%s**: %s (%.1f%%)" % (name.replace("_", " "), count, pct))
    lines.append("")
    return "\n".join(lines)


def _synthesis(client: OpenAI, model: str, stats: dict) -> str:
    prompt = (
        "You are writing an executive brief for a PR/outreach team about HARO journalist demand.\n"
        "Use ONLY the aggregated statistics below (do not invent numbers).\n"
        "Write 4-6 short paragraphs covering: (1) dominant article formats reporters want, "
        "(2) what they ask sources to provide, (3) outlet/medium mix, (4) hottest topic clusters, "
        "(5) practical implications for which pitches to prioritize.\n"
        "Be specific and quantitative. Plain markdown.\n\n"
        "STATS:\n%s" % json.dumps(stats, indent=2)
    )
    response = client.chat.completions.create(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": "Clear, direct business writing."},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def main() -> int:
    p = argparse.ArgumentParser(description="AI analysis of HARO request topics and article types.")
    p.add_argument("--days", type=int, default=0, help="Only requests since N days ago (0 = all).")
    p.add_argument("--sample", type=int, default=700, help="Max requests to classify (stratified by HARO section).")
    p.add_argument("--all", action="store_true", help="Classify every request (slow/costly).")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--model", default="gpt-4o-mini", help="Model for per-request classification.")
    p.add_argument("--report-model", default="gpt-4o-mini", help="Model for final narrative.")
    p.add_argument("-o", "--output", default="", help="Write markdown report to this path.")
    args = p.parse_args()

    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        return 1

    init_db()
    client = OpenAI(api_key=settings.openai_api_key)

    with get_session() as db:
        q = select(HaroRequest).order_by(HaroRequest.created_at.desc())
        if args.days > 0:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
            q = q.where(HaroRequest.created_at >= cutoff)
        rows = list(db.scalars(q).all())

    if not rows:
        print("No haro_requests in range.")
        return 0

    n_classify = len(rows) if args.all else min(args.sample, len(rows))
    sample = rows if args.all else _stratified_sample(rows, n_classify, args.seed)

    print("Loaded %s requests; classifying %s with %s…" % (len(rows), len(sample), args.model), flush=True)

    article_fmt: Counter = Counter()
    ask_type: Counter = Counter()
    reporter_medium: Counter = Counter()
    topic_label: Counter = Counter()
    haro_section: Counter = Counter()
    classified = 0
    errors = 0

    for i in range(0, len(sample), args.batch_size):
        batch = sample[i : i + args.batch_size]
        try:
            items = _classify_batch(client, batch, args.model)
        except Exception as e:
            print("  batch %s failed: %s" % (i // args.batch_size + 1, e), file=sys.stderr)
            errors += 1
            continue
        for item in items:
            classified += 1
            af = str(item.get("article_format") or "other").strip().lower()
            at = str(item.get("ask_type") or "other").strip().lower()
            rm = str(item.get("reporter_medium") or "unknown").strip().lower()
            tl = str(item.get("topic_label") or "unspecified").strip().lower()
            article_fmt[af] += 1
            ask_type[at] += 1
            reporter_medium[rm] += 1
            topic_label[tl] += 1
            rid = int(item["id"])
            req = next((r for r in batch if r.id == rid), None)
            if req:
                haro_section[(req.category or "unknown").strip() or "unknown"] += 1
        print("  … %s / %s batches (%s classified)" % (i // args.batch_size + 1, (len(sample) + args.batch_size - 1) // args.batch_size, classified), flush=True)

    stats = {
        "total_in_db": len(rows),
        "sample_classified": classified,
        "batch_errors": errors,
        "article_format": dict(article_fmt.most_common(25)),
        "ask_type": dict(ask_type.most_common(25)),
        "reporter_medium": dict(reporter_medium.most_common(15)),
        "topic_label_top_40": dict(topic_label.most_common(40)),
        "haro_digest_section_in_sample": dict(haro_section.most_common(15)),
    }

    narrative = _synthesis(client, args.report_model, stats)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_parts = [
        "# HARO demand analysis (AI)\n",
        "Generated: %s\n" % now,
        "Database: **%s** total requests; **%s** classified in this run (%s).\n"
        % (len(rows), classified, "full corpus" if args.all else "stratified sample of %s" % len(sample)),
        _counter_report(article_fmt, "Most common article formats journalists want"),
        _counter_report(ask_type, "Most common things reporters ask sources for"),
        _counter_report(reporter_medium, "Reporter / outlet medium"),
        _counter_report(topic_label, "Most common specific topics (AI labels)", top_n=25),
        _counter_report(haro_section, "HARO digest sections in this sample", top_n=12),
        "## Executive summary\n",
        narrative,
        "\n---\n",
        "<details><summary>Raw stats JSON</summary>\n\n```json\n%s\n```\n</details>\n" % json.dumps(stats, indent=2),
    ]
    report = "\n".join(report_parts)

    print("\n" + "=" * 72)
    print(report)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print("\nWrote %s" % args.output)

    return 0 if classified > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
