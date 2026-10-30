"""地政学の救済の prompt 比較 (flat vs pir) の GPU 測定 (**Ollama を使う。本番稼働中は流さない**)。

triage_rejections (--since 以降) のうち救済の対象 (importance=low かつ理由が地政学語) から N 件を
乱数で抜き、救済モデル (Step.TRIAGE_GEO_RESCUE の割当) で **同じ入力** を prompt=flat と
prompt=pir で判定し、記事 id ごとの判定 (medium 以上 = 救済) を jsonl に出す。
Opus の正解 60 件との McNemar は別途 (この出力の id と正解の id を突き合わせる)。

⚠ 入力: triage_rejections に RSS 概要は保存されていない。本番の triage の入力は題 + RSS 概要
(最大 1500 字) なので、題だけで比べると本番を再現しない。**--summaries は必須**:
jsonl {"id":..., "summary":...} (RSS を元のフィードから読み取りで再取得して作る)。標本はこの
ファイルに概要がある候補だけから抜く。出力に本文・概要は含めない (id と判定のみ)。

--gold <jsonl: {"id":..., "rescue": true|false}> (Opus の正解 60 件) を渡すと、正解のある id で
flat / pir の正誤の McNemar (exact 両側) を出す。層 (stratum) = 理由に含まれる地政学語。

使い方:
  set -a; . ./.env; set +a
  DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko" \\
  uv run python scripts/measure_geo_rescue_prompt.py --n 200 --out decisions.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import src.storage.db_backend as _db  # noqa: E402

_db.ensure_pg_schema = lambda *a, **k: None

import src.storage.config_store as _cs  # noqa: E402

# 読み取り専用の接続では config_store の CREATE TABLE IF NOT EXISTS が失敗し、DB の割当を
# 読めずにコード既定のモデルへ黙って落ちる (10-30 の測定で s24ci65 が使われた原因)。DDL を飛ばす
_cs._DDL = ""

from src.config_loader import load_app_config  # noqa: E402
from src.eval.geo_rescue_stats import summarize  # noqa: E402
from src.pipeline.filters import _GEO_REASON  # noqa: E402
from src.tools.article_model import Article  # noqa: E402
from src.tools.article_triage import ArticleTriage  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

DEFAULT_N = 200
DEFAULT_SINCE = "2026-10-12"
SEED = 20261030
KEEP = {"medium", "high"}
MIN_GRADED_GOLD = 50  # 正解 60 件のうち標本に入っているべき最小件数


def load_candidates(since: str) -> list[dict[str, Any]]:
    conn = _db.connect()
    try:
        cur = conn.execute(
            "SELECT article_id, url, title, feed_title, feed_url, reason "
            "FROM triage_rejections WHERE ts >= ? AND importance = 'low'",
            [since],
        )
        rows = [
            {"id": r[0], "url": r[1], "title": r[2], "feed": r[3], "feed_url": r[4], "reason": r[5]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()
    return [r for r in rows if _GEO_REASON.search(r["reason"] or "")]


def stratum_of(reason: str) -> str:
    """救済対象になった理由の語 (最初に当たった地政学語)。層別の件数に使う。"""
    m = _GEO_REASON.search(reason or "")
    return m.group(1) if m else "other"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def to_article(row: dict[str, Any], summary: str) -> Article:
    return Article(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        summary_html=summary,
        published=datetime.now(UTC),
        feed_title=row["feed"],
        feed_url=row["feed_url"],
    )


async def judge(triage: ArticleTriage, articles: list[Article], conc: int) -> dict[str, str]:
    sem = asyncio.Semaphore(conc)

    async def _one(a: Article) -> tuple[str, str]:
        async with sem:
            d = await triage.triage(a)
            return a.id, "error" if d.error else d.importance

    return dict(await asyncio.gather(*(_one(a) for a in articles)))


async def run(args: argparse.Namespace) -> None:
    os.environ["TRIAGE_FLAT"] = "1"  # 本番と同じ条件 (flat 側の再現。pir 側は force_pir で上書き)
    summaries = {o["id"]: o["summary"] for o in load_jsonl(args.summaries) if o.get("summary")}
    cands = [r for r in load_candidates(args.since) if r["id"] in summaries]
    if not cands:
        raise SystemExit("--summaries に概要のある救済候補がない (id を確認)")
    sample = random.Random(SEED).sample(cands, min(args.n, len(cands)))
    strata = {r["id"]: stratum_of(r["reason"]) for r in sample}
    gold = {o["id"]: bool(o["rescue"]) for o in load_jsonl(args.gold)} if args.gold else {}
    articles = [to_article(r, summaries[r["id"]]) for r in sample]
    llm = build_llm_for(Step.TRIAGE_GEO_RESCUE, load_app_config())
    expected = os.environ.get("GEO_MEASURE_EXPECT_MODEL", "kuebiko-sft:26b")
    got = getattr(llm, "model", None)
    if got != expected:
        raise SystemExit(
            f"救済のモデルが想定と違う: want={expected} got={got} (DB の割当を読めていない可能性)"
        )
    results: dict[str, dict[str, str]] = {}
    for mode in ("flat", "pir"):
        triage = ArticleTriage(llm, force_pir=mode == "pir")
        if triage.prompt_path != mode:
            raise RuntimeError(f"prompt 経路が想定と違う: want={mode} got={triage.prompt_path}")
        results[mode] = await judge(triage, articles, args.concurrency)
    records = [
        {
            "id": a.id,
            "stratum": strata[a.id],
            "flat": results["flat"][a.id],
            "pir": results["pir"][a.id],
            "flat_rescued": results["flat"][a.id] in KEEP,
            "pir_rescued": results["pir"][a.id] in KEEP,
        }
        for a in articles
    ]
    args.out.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    summary = summarize(records, strata, gold)
    if gold and summary["graded"] < MIN_GRADED_GOLD:
        print(
            f"WARNING: 標本に入った正解は {summary['graded']}/{len(gold)} 件 "
            f"(< {MIN_GRADED_GOLD})。McNemar の検出力が足りない (--n を増やすか概要を足す)"
        )
    print(f"n={len(articles)} candidates={len(cands)} -> {args.out}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--summaries", type=Path, required=True, help="id と RSS 概要の jsonl (必須)")
    ap.add_argument("--gold", type=Path, default=None, help="Opus の正解 jsonl (id, rescue)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--out", type=Path, required=True)
    asyncio.run(run(ap.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
