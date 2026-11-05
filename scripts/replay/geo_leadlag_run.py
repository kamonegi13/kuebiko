"""GEO-LEADLAG-MONTHLY の抽出・凍結・累積の道具 (SELECT のみ、LLM なし)。

仕様: docs/replay_prereg/geo_leadlag_monthly.md。
実行 (コンテナ内。接続は read-only セッション):

    docker compose run --rm --no-deps -T kuebiko python -m scripts.replay.geo_leadlag_run \
        --mode dryrun --allow-owner-connection

--mode dryrun  : 10-29..end の通し。出力に dry-run と書き、E/R/p/区間は書かない (検定は回すが捨てる)
--mode freeze  : 締まった月 (--month YYYY-MM) の日ごとの系列を凍結 (series.json + manifest)
--mode analyze : 凍結した全月をつなげて主の族 6 検定を計算 (dry-run 以外の累積の回)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.replay import _geo_leadlag as gl
from scripts.replay._common import add_connection_args, open_readonly
from scripts.replay._manifest import assert_no_body

JST = timezone(timedelta(hours=9))
DEFAULT_DRYRUN_END = date(2026, 11, 5)
DRYRUN_DIR = Path("data/eval/geo_leadlag/dryrun_20261106")
FROZEN_ROOT = Path("data/eval/geo_leadlag_monthly")
PREREG = Path("docs/replay_prereg/geo_leadlag_monthly.md")
ACTOR_ALIASES = Path("config/cti/actor_aliases.yaml")
COUNTRIES = Path("config/cti/countries.yaml")
NO_FEED = "(none)"

ARTICLES_SQL = f"""
WITH a AS (
  SELECT article_id, status, category, duplicate_of, victim_country_iso,
         subject_actor_ids, socio_political_intent, intent_confidence, feed_url,
         CASE WHEN {{trusted}} THEN 1 ELSE 0 END AS subject_trusted,
         ROW_NUMBER() OVER (PARTITION BY article_id
                            ORDER BY CASE WHEN status = 'posted' THEN 0 ELSE 1 END, id DESC) AS rn
  FROM articles
  WHERE article_id IN (SELECT article_id FROM articles WHERE created_at >= ?)
)
SELECT d.article_id AS article_id, d.created_at AS created_at,
       {{ts}} AS event_ts, a.status AS status, a.category AS category,
       a.duplicate_of AS duplicate_of, a.victim_country_iso AS victim_iso,
       a.subject_actor_ids AS subject_ids, a.subject_trusted AS subject_trusted,
       a.socio_political_intent AS intent, a.intent_confidence AS intent_conf,
       COALESCE(NULLIF(a.feed_url, ''), '{NO_FEED}') AS feed_key
FROM {{dedup}} d JOIN a ON a.article_id = d.article_id AND a.rn = 1
WHERE d.created_at >= ?
"""
ENTITIES_SQL = (
    "SELECT article_id, value FROM article_entities WHERE entity_type = 'involved_country'"
    " AND article_id IN (SELECT article_id FROM articles WHERE created_at >= ?)"
)
NEW_FEEDS_SQL = (
    f"SELECT COALESCE(NULLIF(feed_url, ''), '{NO_FEED}') AS k FROM articles"
    " GROUP BY COALESCE(NULLIF(feed_url, ''), '" + NO_FEED + "') HAVING MIN(created_at) >= ?"
)


def build_articles_sql() -> str:
    from src.cti.subject_gate import trusted_subject_clause
    from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR

    return ARTICLES_SQL.format(
        trusted=trusted_subject_clause(
            source_col="subject_actor_source", confidence_col="subject_actor_confidence"
        ),
        ts=EVENT_TS_EXPR.format(a="d"),
        dedup=DEDUP_ARTICLES,
    )


def to_jst(value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(JST)


def next_month_start(d: date) -> datetime:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(y, m, 1, tzinfo=JST)


def actor_maps() -> tuple[dict[str, str], set[str]]:
    """actor id -> 国家系の国 (大文字)、ハクティビストの id 集合。"""
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.threat_actor_doctrine import state_nation_map

    actors = load_actor_aliases(ACTOR_ALIASES).actors
    nations = {k: v.upper() for k, v in state_nation_map(actors).items()}
    return nations, {a.id for a in actors if (a.family or "") == "hacktivist"}


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(
    start: date, end: date, *, allow_owner: bool, timeout_ms: int
) -> tuple[list[gl.ArticleFacts], list[gl.ArticleFacts], dict[str, Any]]:
    """(主: 新しい取得先を外す, 全取得先, 除外の数と新取得先の鍵)。"""
    since = datetime.combine(start, datetime.min.time(), tzinfo=JST)
    nations, hacktivists = actor_maps()
    conn = open_readonly(allow_owner_connection=allow_owner, statement_timeout_ms=timeout_ms)
    with conn:
        rows = conn.execute(build_articles_sql(), (since, since)).fetchall()
        ents = conn.execute(ENTITIES_SQL, (since,)).fetchall()
        new_feeds = {r["k"] for r in conn.execute(NEW_FEEDS_SQL, (since,)).fetchall()}
    parties: dict[str, set[str]] = {}
    for e in ents:
        parties.setdefault(e["article_id"], set()).add(str(e["value"]).strip().upper())
    main, full = [], []
    excl: Counter[str] = Counter()
    for r in rows:
        day = to_jst(r["event_ts"]).date()
        if not start <= day <= end:
            continue
        month = day.strftime("%Y-%m")
        if to_jst(r["created_at"]) >= next_month_start(day):
            excl[f"{month}:late_ingest"] += 1
            continue
        if r["status"] != "posted":
            excl[f"{month}:not_posted"] += 1
            continue
        if r["duplicate_of"]:
            excl[f"{month}:duplicate"] += 1
            continue
        facts = _facts(r, day, parties.get(r["article_id"], set()), nations, hacktivists)
        full.append(facts)
        if r["feed_key"] in new_feeds:
            excl[f"{month}:new_feed"] += 1
        else:
            main.append(facts)
    return main, full, {"exclusions": dict(excl), "new_feed_keys": sorted(new_feeds)}


def _facts(
    r: Mapping[str, Any],
    day: date,
    parties: set[str],
    nations: Mapping[str, str],
    hacktivists: set[str],
) -> gl.ArticleFacts:
    ids = [s.strip() for s in str(r["subject_ids"] or "").split(",") if s.strip()]
    trusted = bool(r["subject_trusted"])
    return gl.ArticleFacts(
        day=day,
        category=str(r["category"] or ""),
        parties=frozenset(parties),
        victim_iso=str(r["victim_iso"] or "").upper(),
        subject_nations=frozenset(nations[i] for i in ids if i in nations)
        if trusted
        else frozenset(),
        has_hacktivist_subject=trusted and any(i in hacktivists for i in ids),
        intent=str(r["intent"] or ""),
        intent_confidence=str(r["intent_conf"] or ""),
    )


def series_payload(
    days: Sequence[date], main: Sequence[gl.ArticleFacts], full: Sequence[gl.ArticleFacts]
) -> dict[str, Any]:
    cm, cf = gl.build_counts(days, main), gl.build_counts(days, full)
    return {
        "days": [
            {"date": d.isoformat(), "main": a, "all": b}
            for d, a, b in zip(days, cm, cf, strict=True)
        ]
    }


def load_frozen(root: Path) -> tuple[list[date], list[dict[str, int]], list[dict[str, int]]]:
    days: list[date] = []
    main: list[dict[str, int]] = []
    full: list[dict[str, int]] = []
    for path in sorted(root.glob("*/series.json")):
        for row in json.loads(path.read_text(encoding="utf-8"))["days"]:
            days.append(date.fromisoformat(row["date"]))
            main.append(row["main"])
            full.append(row["all"])
    return days, main, full


def result_rows(results: Sequence[gl.TestResult]) -> list[dict[str, Any]]:
    return [r.__dict__ for r in results]


def write_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def git_out(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_manifest(
    *, mode: str, start: date, end: date, days: Sequence[date], counts: Sequence[Mapping[str, int]],
    extra: Mapping[str, Any], series_sha: str, feed_keys_sha: str,
) -> dict[str, Any]:  # fmt: skip
    missing = gl.missing_days(days, counts)
    return {
        "mode": mode,
        "dry_run": mode == "dryrun",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "rows": len(days),
        "geo_missing_days": missing["geo_missing"],
        "cyber_missing_days": missing["cyber_missing"],
        "exclusions": extra["exclusions"],
        "new_feed_key_count": len(extra["new_feed_keys"]),
        "series_sha256": series_sha,
        "excluded_feed_keys_sha256": feed_keys_sha,
        "prereg_path": str(PREREG),
        "prereg_git_sha": git_out("log", "-1", "--format=%H", "--", str(PREREG)),
        "prereg_blob_sha256": sha256_path(PREREG) if PREREG.exists() else "unknown",
        "head_sha": git_out("rev-parse", "HEAD"),
        "actor_aliases_sha256": sha256_path(ACTOR_ALIASES) if ACTOR_ALIASES.exists() else "unknown",
        "countries_sha256": sha256_path(COUNTRIES) if COUNTRIES.exists() else "unknown",
    }


def run_extract_mode(args: argparse.Namespace) -> int:
    start = gl.CONFIRMATORY_START
    if args.mode == "freeze":
        y, m = map(int, args.month.split("-"))
        first = date(y, m, 1)
        start = max(first, gl.CONFIRMATORY_START)
        end = next_month_start(first).date() - timedelta(days=1)
    else:
        end = date.fromisoformat(args.end)
    days = gl.date_range(start, end)
    main, full, extra = extract(
        start, end, allow_owner=args.allow_owner_connection, timeout_ms=args.statement_timeout_ms
    )
    payload = series_payload(days, main, full)
    out = DRYRUN_DIR if args.mode == "dryrun" else FROZEN_ROOT / args.month
    if args.mode == "dryrun":
        payload = {"dry_run": True, **payload}
        _run_discarded_stats(days, payload)
    series_sha = write_json(out / "series.json", payload)
    keys_sha = write_json(out / "excluded_feed_keys.json", {"keys": extra["new_feed_keys"]})
    manifest = build_manifest(
        mode=args.mode, start=start, end=end, days=days,
        counts=[r["main"] for r in payload["days"]], extra=extra,
        series_sha=series_sha, feed_keys_sha=keys_sha,
    )  # fmt: skip
    manifest["stats_tool_ran"] = args.mode == "dryrun"
    assert_no_body(manifest)
    write_json(out / "manifest.json", manifest)
    print(  # noqa: T201
        f"{args.mode}: rows={manifest['rows']} geo_missing={len(manifest['geo_missing_days'])}"
        f" cyber_missing={len(manifest['cyber_missing_days'])}"
        f" excluded={sum(manifest['exclusions'].values())}"
    )
    return 0


def _run_discarded_stats(days: Sequence[date], payload: Mapping[str, Any]) -> None:
    """dry-run: 検定の経路を通すが結果は捨てる (E/R/p を出さない)。"""
    counts = [r["main"] for r in payload["days"]]
    gl.run_primary_family(counts, gl.month_groups(days))


def run_analyze(args: argparse.Namespace) -> int:
    days, main, full = load_frozen(Path(args.frozen_root))
    if not days:
        print("no frozen months", file=sys.stderr)  # noqa: T201
        return 1
    breaks = [date.fromisoformat(s) for s in args.breaks]
    report = {
        "primary": result_rows(gl.run_primary_family(main, gl.month_groups(days))),
        "robust_regime": result_rows(gl.run_primary_family(main, gl.regime_groups(days, breaks))),
        "all_sources": result_rows(gl.run_primary_family(full, gl.month_groups(days))),
        "rows": len(days),
    }
    out = Path(args.frozen_root) / "analysis_latest.json"
    write_json(out, report)
    print(f"analyze: rows={len(days)} -> {out}")  # noqa: T201
    return 0


def main_cli(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["dryrun", "freeze", "analyze"], required=True)
    p.add_argument("--end", default=DEFAULT_DRYRUN_END.isoformat())
    p.add_argument("--month", help="YYYY-MM (freeze)")
    p.add_argument("--frozen-root", default=str(FROZEN_ROOT))
    p.add_argument("--breaks", nargs="*", default=["2026-10-31", "2026-11-03", "2026-11-05"])
    add_connection_args(p)
    args = p.parse_args(argv)
    if args.mode == "freeze" and not args.month:
        p.error("--month is required for freeze")
    return run_analyze(args) if args.mode == "analyze" else run_extract_mode(args)


if __name__ == "__main__":
    raise SystemExit(main_cli())
