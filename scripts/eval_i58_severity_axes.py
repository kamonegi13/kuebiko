#!/usr/bin/env python3
"""i58 (= fast ティア, 2026-10-10 投入) の深刻度の軸・深刻さ導出の精度を測る (読み取り専用)。

測定対象の 2 集合:
  - v4 set:     data/eval/severity_gold_v4.jsonl (203 件、S3 適合率の測定用集合)
  - random set: data/eval/axes_teacher_v4.jsonl の split=="heldout_random" (200 件、
                無作為抽出 — S3 見逃し率の母集団推定に使う集合)

各記事について本番と同じ入力 (title + summary) で classify_axes を呼び、
本番と同じ client (build_llm_for(Step.SEVERITY_AXES, ...)) を使う。得た軸から
importance_v2.derive_severity() で深刻さを導き、正解の軸で導いた深刻さと両方記録する。

学習データ (axes_teacher_v1..v4 の split=="train"、triage_flat_teacher_v1.jsonl の
split=="train"、pir_teacher_v2.jsonl 全件) に出てくる記事は除外する
(triage_flat は article_id を持たないため sha256(feed_title+title) で突合)。

再開可: 既存の出力 jsonl を読み、済んだ (article_id, set) をスキップして追記する。
並列は 1 (本番の OLLAMA_NUM_PARALLEL=1 に合わせる)。朝夕ブリーフ (06:30 / 19:30 JST)
の前後は一時停止する。

DB・kev/nvd キャッシュは読み取りのみ (refresh系は呼ばない)。書き込みは出力 jsonl のみ。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

from src.config_loader import load_app_config  # noqa: E402
from src.cti.importance_v2 import (  # noqa: E402
    ImportanceInputs,
    derive,
    stated_cvss,
    stated_loss_usd,
    subject_kev,  # noqa: E402
)
from src.cti.severity_axes import classify_axes  # noqa: E402
from src.storage.db_backend import connect as db_connect  # noqa: E402
from src.tools.kev_client import get_kev_cve_set  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402
from src.tools.nvd_client import max_cvss  # noqa: E402

EVAL_DIR = Path("/app/data/eval")
OUT_PATH = EVAL_DIR / "i58_severity_eval_v1.jsonl"
AX = ("scope", "impact", "confirmation", "magnitude", "exploitation", "actor", "target")
_ENTITY_TYPES = ("cve", "involved_country", "mentioned_country", "pir")
_CHUNK = 300

#: 朝夕ブリーフの前後は一時停止する幅 (分、JST)。
_BRIEF_WINDOWS = ((6, 30), (19, 30))
_BRIEF_MARGIN_MIN = 15
_JOBS_API = "http://host.docker.internal:8001/api/v1/jobs"


def _jst_now() -> datetime:
    return datetime.now(UTC) + timedelta(hours=9)


def _near_brief_window(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    for h, m in _BRIEF_WINDOWS:
        center = h * 60 + m
        if abs(minutes - center) <= _BRIEF_MARGIN_MIN:
            return True
    return False


def _heavy_job_running() -> bool:
    try:
        with urllib.request.urlopen(_JOBS_API, timeout=3) as resp:  # noqa: S310
            data = json.load(resp)
    except Exception:  # noqa: BLE001 — 到達不能なら判定不能として進める
        return False
    for j in data.get("jobs", []):
        is_brief = j.get("id") in ("morning-brief", "evening-brief")
        if is_brief and (j.get("last_run") or {}).get("status") == "running":
            return True
    return False


def pause_if_needed() -> None:
    """朝夕ブリーフの前後、または実行中なら待つ。"""
    first = True
    while True:
        now = _jst_now()
        if not _near_brief_window(now) and not _heavy_job_running():
            return
        if first:
            print(f"[pause] {now.isoformat()} ブリーフ帯のため待機中…", flush=True)
            first = False
        time.sleep(60)


# ===== 正解集・除外集合の読み込み =====


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _triage_flat_key(feed: str, title: str) -> str:
    return hashlib.sha256(f"{feed}{title}".encode()).hexdigest()


def load_gold_sets() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    v4 = _read_jsonl(EVAL_DIR / "severity_gold_v4.jsonl")
    axes_v4 = _read_jsonl(EVAL_DIR / "axes_teacher_v4.jsonl")
    random_set = [r for r in axes_v4 if r.get("split") == "heldout_random"]
    return v4, random_set


def load_train_ids() -> set[str]:
    """axes_teacher_v1..v4 の split=="train" + pir_teacher_v2 (全件) の article_id。"""
    train_ids: set[str] = set()
    for v in (1, 2, 3, 4):
        path = EVAL_DIR / f"axes_teacher_v{v}.jsonl"
        if not path.exists():
            continue
        for r in _read_jsonl(path):
            if r.get("split") == "train":
                train_ids.add(str(r["article_id"]))
    for r in _read_jsonl(EVAL_DIR / "pir_teacher_v2.jsonl"):
        aid = r.get("article_id")
        if aid:
            train_ids.add(str(aid))
    return train_ids


def load_triage_flat_train_keys() -> set[str]:
    """triage_flat_teacher_v1.jsonl の split=="train" の key (= sha256(feed_title+title)) 集合。

    この teacher は article_id を持たず key だけなので、gold 記事側で同じ式で key を
    組み立てて突合する (match_triage_excluded)。
    """
    keys: set[str] = set()
    for r in _read_jsonl(EVAL_DIR / "triage_flat_teacher_v1.jsonl"):
        if r.get("split") == "train":
            keys.add(str(r.get("key") or ""))
    return keys


def match_triage_excluded(
    rows: list[dict[str, Any]], feed_title_by_id: dict[str, str], triage_keys: set[str]
) -> set[str]:
    out: set[str] = set()
    for r in rows:
        aid = str(r["article_id"])
        title = str(r.get("title") or "")
        feed = feed_title_by_id.get(aid, "")
        if _triage_flat_key(feed, title) in triage_keys:
            out.add(aid)
    return out


# ===== DB からの材料取得 (読み取りのみ) =====


@dataclass(frozen=True)
class ArticleRow:
    title: str
    summary: str
    body: str
    category: str
    article_type: str
    victim_country: str
    feed_title: str


def fetch_articles(conn: Any, ids: list[str]) -> dict[str, ArticleRow]:
    out: dict[str, ArticleRow] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        ph = ",".join("?" * len(chunk))
        sql = (
            "SELECT article_id, title, summary, body, category, article_type, "
            "victim_country_iso, feed_title FROM ("
            "  SELECT a.article_id, a.title, a.summary, a.body, a.category, a.article_type, "
            "         a.victim_country_iso, a.feed_title, "
            "         ROW_NUMBER() OVER ("
            "             PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn"
            f"  FROM articles a WHERE a.article_id IN ({ph})"
            ") t WHERE rn = 1"
        )
        for r in conn.execute(sql, tuple(chunk)).fetchall():
            out[str(r["article_id"])] = ArticleRow(
                title=str(r["title"] or ""),
                summary=str(r["summary"] or ""),
                body=str(r["body"] or ""),
                category=str(r["category"] or ""),
                article_type=str(r["article_type"] or ""),
                victim_country=str(r["victim_country_iso"] or ""),
                feed_title=str(r["feed_title"] or ""),
            )
    return out


def fetch_entities(conn: Any, ids: list[str]) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = {}
    types_ph = ",".join("?" * len(_ENTITY_TYPES))
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        ph = ",".join("?" * len(chunk))
        sql = (
            "SELECT article_id, entity_type, value FROM article_entities "
            f"WHERE entity_type IN ({types_ph}) AND article_id IN ({ph})"
        )
        for r in conn.execute(sql, (*_ENTITY_TYPES, *chunk)).fetchall():
            aid, et, v = str(r["article_id"]), str(r["entity_type"]), str(r["value"])
            out.setdefault(aid, {}).setdefault(et, set()).add(v if et == "pir" else v.upper())
    return out


# ===== 深刻さ導出 =====


def derive_sev(
    *,
    article: ArticleRow,
    category: str,
    article_type: str,
    axes: dict[str, str],
    ents: dict[str, set[str]],
    kev: frozenset[str],
) -> tuple[str | None, str | None, str]:
    """深刻さを導く。``category``/``article_type`` は正解集の labeling 時点の値を使う

    (DB の現在値ではない — s21 の測定スクリプト ``heldout_random_axes_report.py`` と
    同じ方式。articles テーブルの再分類で drift していても測定条件を揃える)。
    """
    text = f"{article.summary}\n{article.body}"
    cves = sorted(ents.get("cve", set()))
    inp = ImportanceInputs(
        category=category,
        article_type=article_type,
        axes=axes,
        on_kev=subject_kev(cves, kev, 2026),
        max_cvss=max(max_cvss(cves), stated_cvss(text)),
        victim_country=article.victim_country,
        involved_countries=frozenset(ents.get("involved_country", set())),
        mentioned_countries=frozenset(ents.get("mentioned_country", set())),
        sir_ids=frozenset(ents.get("pir", set())),
        loss_usd=stated_loss_usd(text),
    )
    d = derive(inp)
    return d.severity, d.strategic_weight, d.severity_basis


# ===== Wilson 95% 区間 =====


def wilson_interval(hits: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ===== main =====


async def main() -> None:
    cfg = load_app_config()
    llm = build_llm_for(Step.SEVERITY_AXES, cfg)
    model_label = str(getattr(llm, "model", ""))
    print(f"[info] model = {model_label}", flush=True)

    v4_rows, random_rows = load_gold_sets()
    all_rows = [("v4", r) for r in v4_rows] + [("random", r) for r in random_rows]
    all_ids = [str(r["article_id"]) for _, r in all_rows]

    train_ids = load_train_ids()
    triage_keys = load_triage_flat_train_keys()

    conn = db_connect()
    try:
        articles = fetch_articles(conn, all_ids)
        ents = fetch_entities(conn, all_ids)
    finally:
        conn.close()

    feed_title_by_id = {aid: a.feed_title for aid, a in articles.items()}
    triage_excluded = match_triage_excluded([r for _, r in all_rows], feed_title_by_id, triage_keys)
    exclude_ids = train_ids | triage_excluded
    kev = get_kev_cve_set()

    n_excluded_v4 = sum(1 for r in v4_rows if str(r["article_id"]) in exclude_ids)
    n_excluded_random = sum(1 for r in random_rows if str(r["article_id"]) in exclude_ids)
    print(
        f"[info] exclude total={len(exclude_ids)} "
        f"(train_ids={len(train_ids)}, triage_hash_matched={len(triage_excluded)}) "
        f"-> v4 excluded {n_excluded_v4}/{len(v4_rows)}, "
        f"random excluded {n_excluded_random}/{len(random_rows)}",
        flush=True,
    )

    done: set[tuple[str, str]] = set()
    if OUT_PATH.exists():
        for r in _read_jsonl(OUT_PATH):
            done.add((str(r["article_id"]), str(r["set"])))
        print(f"[info] resuming, {len(done)} already done", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    n_done_now = 0
    with OUT_PATH.open("a", encoding="utf-8") as out:
        for set_name, row in all_rows:
            aid = str(row["article_id"])
            if (aid, set_name) in done:
                continue
            if aid in exclude_ids:
                continue
            article = articles.get(aid)
            if article is None:
                print(f"[warn] {aid} missing from DB, skipping", flush=True)
                continue

            pause_if_needed()

            gold = row["gold"]
            gold_severity = gold.get("severity")
            gold_boundary = bool(gold.get("severity_boundary"))
            gold_ax = {k: gold.get(k) for k in AX}
            e = ents.get(aid, {})
            category = str(row.get("category") or "")
            article_type = str(row.get("article_type") or "")

            failure = False
            try:
                pred_ax_model = await classify_axes(llm, article.title, article.summary)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] classify_axes failed for {aid}: {type(exc).__name__}", flush=True)
                pred_ax_model = None
            if pred_ax_model is None:
                failure = True
                pred_ax: dict[str, str] = {}
            else:
                pred_ax = {k: getattr(pred_ax_model, k) for k in AX}

            per_axis_match = (
                {k: (pred_ax.get(k) == gold_ax.get(k)) for k in AX} if not failure else {}
            )

            pred_sev, pred_weight, pred_basis = (None, None, "no_axes")
            if not failure:
                pred_sev, pred_weight, pred_basis = derive_sev(
                    article=article,
                    category=category,
                    article_type=article_type,
                    axes=pred_ax,
                    ents=e,
                    kev=kev,
                )
            gold_sev, gold_weight, gold_basis = derive_sev(
                article=article,
                category=category,
                article_type=article_type,
                axes={k: v for k, v in gold_ax.items() if v},
                ents=e,
                kev=kev,
            )

            rec = {
                "article_id": aid,
                "set": set_name,
                "title": article.title,
                "gold_severity": gold_severity,
                "gold_severity_boundary": gold_boundary,
                "pred_severity_i58_axes": pred_sev,
                "pred_basis_i58_axes": pred_basis,
                "pred_severity_gold_axes": gold_sev,
                "pred_basis_gold_axes": gold_basis,
                "pred_axes": pred_ax,
                "gold_axes": gold_ax,
                "per_axis_match": per_axis_match,
                "failure": failure,
                "model": model_label,
                "ts": datetime.now(UTC).isoformat(),
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            n_done_now += 1
            if n_done_now % 10 == 0:
                print(f"[info] {n_done_now} done this run", flush=True)

    print(f"[done] {n_done_now} newly evaluated, output -> {OUT_PATH}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
