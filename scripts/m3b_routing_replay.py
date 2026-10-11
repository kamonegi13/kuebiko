"""M3b: 過去の投稿済み記事に v13 (M3B_RULES) を当て直し、現行 (投稿時点) との食い違いを見る
(read-only)。

対象: 直近 N 日 (既定 30)、posted_channel が非 NULL、``article_importance_v2`` に記録済みの
記事 (同 article_id は最新 1 行)。

## 配信判定の経路 (2026-10-11、二重実装の解消)

本スクリプトは ``src/pipeline/briefing.py:decide_channel`` を**本番と同じ関数**として呼ぶ。
以前は ``evaluate_routing_rules``/``RoutingSignals`` をこのスクリプト側で自前に組み立てて
いたため、本番 (``_build_briefing``) が route() 直前に通す ``_enrich_signals_v2``
(severity/jp/relevant/severity_basis の注入) の欠落を再現できなかった — v13 切替後に
alert が 0 件になった不具合 (10-10 20:33〜10-11 17:25) の検証で露呈した。再発防止のため
``decide_channel`` を経由しない配信判定の実装 (``route``/``evaluate_routing_rules`` の
直接呼び出し) はこのスクリプトから排除している (tests/unit で固定)。

DB への書き込みは一切行わない (SELECT のみ)。

使い方::

    uv run python scripts/m3b_routing_replay.py [--days 30] [--samples 5] [--axes stored|i58]

または (本番 DB に read-only で当てる場合、コンテナ内)::

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro \\
        kuebiko python scripts/m3b_routing_replay.py

``decide_channel`` は記事ごとに ``src/cti/importance_v2_inline.build_importance_v2_inline``
を内部で呼ぶ (``_enrich_signals_v2`` 経由)。このスクリプトは DB の保存済み値から
``BriefingMessage`` 代替を組み立てて渡すだけで、severity/jp/relevant/SIR/CVE 等の計算は
一切再実装しない。相違点 (DB 保存値を材料にすることによる近似):

- ``sir_ids`` の評価には規則ベースの照合だけが使える (``subject_actor_ids=None``、
  判定時点ではまだ articles 永続化後の主題アクター層が確定していない — inline と同じ)。
- 年判定 (``SUBJECT_CVE_YEARS``) は ``build_importance_v2_inline`` と同じく常に
  **replay 実行時点の年** を使う (記事の投稿年ではない)。
- ``actor_values`` / 関与国 / CVE は ``article_entities`` (ingest 時点で付くエンティティ)
  から読む。KEV catalog / NVD CVSS cache は **replay 実行時点の最新** を使う。

``--axes {stored,i58}``:

- ``stored``: ``article_severity_axes`` の保存済み行 (旧モデル混在) をそのまま使う。
- ``i58``: ``importance_v2_inline.axes_eligible`` が true の記事だけ、本番と同じ
  ``classify_axes_inline`` (Step.SEVERITY_AXES = 現行 i58) を呼んで軸を取り直す。結果は
  ``data/eval/m3b_rt_axes_cache.jsonl`` に 1 記事 1 行で append し、再実行時は既存行を
  スキップする (resumable)。concurrency は 1 (逐次 await、並列呼出しはしない)。

LLM 呼出し中は本番のスケジュール済みジョブと Ollama を取り合わないよう、呼出し前に必ず
``_wait_until_safe_to_call_llm`` を通す — 06:30 / 19:30 JST ±15 分の帯、または
``GET /api/v1/jobs`` (full instance, 127.0.0.1:8001) で ``morning-brief`` /
``evening-brief`` の ``last_run.status == 'running'`` を検知したら poll して待つ
(到達不能なら fail-open で続行)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if TYPE_CHECKING:
    from src.tools.discord_publisher import BriefingMessage

DEFAULT_DB_PATH = Path("data/run_history.db")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SAMPLES_PER_CELL = 5
DEFAULT_AXES_CACHE_PATH = Path("data/eval/m3b_rt_axes_cache.jsonl")
#: DB IN (...) の chunk サイズ (src/storage/repo_severity_axes.py の _CHUNK と同じ値)。
_DB_CHUNK = 400

#: 対象記事の取得。``importance_v2_inline.build_importance_v2_inline`` (= decide_channel が
#: 内部で呼ぶもの) が ``BriefingMessage`` から読む材料 (title/summary/body/feed_title/
#: category/article_type/victim_sector_canonical/victim_country_iso/socio_political_intent/
#: is_ransomware) を articles から取る。``article_importance_v2`` への JOIN は対象母集団
#: (= 旧 replay と同一の「投稿済み + 旧 importance 計算済み」記事) を揃えるためだけに残す
#: (severity/jp/relevant 列は再計算するため選択しない)。
_SQL_ARTICLES = """
SELECT article_id, title, summary, body, feed_title, category, article_type,
       importance, posted_channel, created_at,
       victim_sector_canonical, victim_country_iso,
       socio_political_intent, is_ransomware
FROM (
    SELECT a.article_id, a.title, a.summary, a.body, a.feed_title, a.category,
           a.article_type, a.importance, a.posted_channel, a.created_at,
           a.victim_sector_canonical, a.victim_country_iso,
           a.socio_political_intent, a.is_ransomware,
           ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
    FROM articles a
    JOIN article_importance_v2 v ON v.article_id = a.article_id
    WHERE a.created_at >= ? AND a.posted_channel IS NOT NULL
) t
WHERE rn = 1
"""  # noqa: S608 — 固定 SQL (バインドは since のみ)

_ENTITIES_SQL = """
SELECT article_id, entity_type, value FROM article_entities
WHERE entity_type IN ('actor', 'involved_country', 'cve') AND article_id IN ({placeholders})
"""  # noqa: S608 — IN の placeholder 数のみ可変、値はすべてバインド

_AXES_COLS = (
    "scope",
    "impact",
    "confirmation",
    "magnitude",
    "exploitation",
    "actor",
    "target",
)
_AXES_BOOL_EXTRA_COLS = ("recent_action", "credential_compromise", "distribution_compromise")
_AXES_STR_EXTRA_COLS = ("victim_size", "recoverability")
_AXES_SQL = """
SELECT article_id, {cols} FROM article_severity_axes WHERE article_id IN ({placeholders})
"""  # noqa: S608 — 列名は固定定数、IN の placeholder 数のみ可変

_JST = ZoneInfo("Asia/Tokyo")
_BRIEF_TIMES_JST: tuple[tuple[int, int], ...] = ((6, 30), (19, 30))
_BRIEF_WINDOW_MINUTES = 15
_BRIEF_JOB_IDS = frozenset({"morning-brief", "evening-brief"})
_JOBS_API_URL = "http://127.0.0.1:8001/api/v1/jobs"
_BRIEF_POLL_SECONDS = 30.0


@dataclass(frozen=True)
class RoutingTimeRow:
    """1 記事分の材料 (DB から組み立てた BriefingMessage 代替)。"""

    article_id: str
    title: str
    summary: str
    body: str
    feed_title: str
    category: str
    article_type: str
    importance: str
    old_channel: str
    created_at: str
    victim_sector: str
    victim_country: str
    socio_political_intent: str
    is_ransomware: bool
    actor_ids: frozenset[str] = field(default_factory=frozenset)
    involved_countries: frozenset[str] = field(default_factory=frozenset)
    persisted_cves: frozenset[str] = field(default_factory=frozenset)
    stored_axes: dict[str, str] = field(default_factory=dict)


def _connect(db_path: Path) -> Any:
    import sqlite3

    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _entity_maps(article_ids: list[str], *, db_path: Path) -> dict[str, dict[str, frozenset[str]]]:
    """article_id → {'actor': set, 'involved_country': set, 'cve': set} (チャンク済み SELECT)。"""
    out: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"actor": set(), "involved_country": set(), "cve": set()}
    )
    with _connect(db_path) as con:
        for i in range(0, len(article_ids), _DB_CHUNK):
            chunk = article_ids[i : i + _DB_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            sql = _ENTITIES_SQL.format(placeholders=placeholders)
            for r in con.execute(sql, chunk).fetchall():
                aid = str(r["article_id"])
                etype = str(r["entity_type"])
                out[aid][etype].add(str(r["value"]))
    return {aid: {k: frozenset(v) for k, v in types.items()} for aid, types in out.items()}


def _axes_bool_str(value: object) -> str | None:
    if value is None:
        return None
    return "true" if bool(value) else "false"


def _stored_axes_map(article_ids: list[str], *, db_path: Path) -> dict[str, dict[str, str]]:
    """article_id → 保存済みの軸 (dict[str,str]、未計算の記事は空 dict)。

    ``src/storage/repo_severity_axes.py:get_severity_axes`` と同じ変換規則を直接 SQL で
    再現する (Repository クラスは ``__init__`` でスキーマ migration/ensure を走らせるため、
    本番 DB への SELECT-only 制約下では使わない)。
    """
    out: dict[str, dict[str, str]] = {}
    cols = ", ".join((*_AXES_COLS, *_AXES_BOOL_EXTRA_COLS, *_AXES_STR_EXTRA_COLS))
    with _connect(db_path) as con:
        for i in range(0, len(article_ids), _DB_CHUNK):
            chunk = article_ids[i : i + _DB_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            sql = _AXES_SQL.format(cols=cols, placeholders=placeholders)
            for r in con.execute(sql, chunk).fetchall():
                values: dict[str, str] = {k: str(r[k]) for k in _AXES_COLS if r[k] is not None}
                for k in _AXES_BOOL_EXTRA_COLS:
                    v = _axes_bool_str(r[k])
                    if v is not None:
                        values[k] = v
                for k in _AXES_STR_EXTRA_COLS:
                    if r[k] is not None:
                        values[k] = str(r[k])
                if values:
                    out[str(r["article_id"])] = values
    return out


def fetch_routing_time_rows(
    *, since: datetime, db_path: Path = DEFAULT_DB_PATH, want_stored_axes: bool
) -> list[RoutingTimeRow]:
    """対象記事を取得する (read-only、articles + 付帯 entity/axes)。"""
    with _connect(db_path) as con:
        base_rows = con.execute(_SQL_ARTICLES, (since.isoformat(),)).fetchall()
    article_ids = [str(r["article_id"]) for r in base_rows]
    entities = _entity_maps(article_ids, db_path=db_path)
    axes_map = _stored_axes_map(article_ids, db_path=db_path) if want_stored_axes else {}

    out: list[RoutingTimeRow] = []
    for r in base_rows:
        aid = str(r["article_id"])
        empty_entities: dict[str, frozenset[str]] = {
            "actor": frozenset(),
            "involved_country": frozenset(),
            "cve": frozenset(),
        }
        ent = entities.get(aid, empty_entities)
        out.append(
            RoutingTimeRow(
                article_id=aid,
                title=str(r["title"] or ""),
                summary=str(r["summary"] or ""),
                body=str(r["body"] or ""),
                feed_title=str(r["feed_title"] or ""),
                category=str(r["category"] or ""),
                article_type=str(r["article_type"] or ""),
                importance=str(r["importance"] or ""),
                old_channel=str(r["posted_channel"] or ""),
                created_at=str(r["created_at"] or ""),
                victim_sector=str(r["victim_sector_canonical"] or ""),
                victim_country=str(r["victim_country_iso"] or ""),
                socio_political_intent=str(r["socio_political_intent"] or ""),
                is_ransomware=bool(r["is_ransomware"]),
                actor_ids=ent["actor"],
                involved_countries=ent["involved_country"],
                persisted_cves=ent["cve"],
                stored_axes=axes_map.get(aid, {}),
            )
        )
    return out


# ===== decide_channel への入力組み立て (本番と同じ関数を呼ぶだけ、判定は再実装しない) =====

_VALID_IMPORTANCE: frozenset[str] = frozenset({"high", "medium", "low"})


def message_for_row(row: RoutingTimeRow) -> BriefingMessage:
    """DB の保存値から ``decide_channel`` に渡す ``BriefingMessage`` 代替を組み立てる。

    ``build_importance_v2_inline`` (decide_channel が内部で呼ぶ) が読む metadata キー
    だけを持たせる — 新規抽出はしない (保存済みの値のみ、判定ロジックはここに置かない)。
    """
    from src.tools.discord_publisher import BriefingMessage, Source

    metadata: dict[str, object] = {"article_type": row.article_type}
    if row.victim_sector:
        metadata["victim_sector_canonical"] = row.victim_sector
    if row.victim_country:
        metadata["victim_country_iso"] = row.victim_country
    if row.involved_countries:
        metadata["involved_country_isos"] = sorted(row.involved_countries)
    if row.socio_political_intent:
        metadata["socio_political_intent"] = row.socio_political_intent
    if row.is_ransomware:
        metadata["is_ransomware"] = True
    if row.actor_ids:
        metadata["detected_actor_ids"] = sorted(row.actor_ids)
    importance = row.importance if row.importance in _VALID_IMPORTANCE else "low"
    return BriefingMessage(
        title=row.title,
        importance=importance,  # type: ignore[arg-type]
        category=row.category,
        summary=row.summary,
        iocs=sorted(row.persisted_cves),
        sources=[Source(title=row.feed_title or "source", url="", language="auto")],
        metadata=metadata,
    )


# ===== --axes i58: LLM 呼出し (resumable cache + 本番ジョブとの時間帯衝突回避) =====


def _near_brief_time(now_jst: datetime) -> bool:
    for hour, minute in _BRIEF_TIMES_JST:
        target = now_jst.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if abs((now_jst - target).total_seconds()) <= _BRIEF_WINDOW_MINUTES * 60:
            return True
    return False


def _brief_job_running() -> bool:
    """morning-brief / evening-brief が実行中か。到達不能・形式不明は False (fail-open)。

    ⚠ ジョブ API の ``running`` という単体 field は常に空 — 見るのは ``last_run.status``
    (agent-memory jobs_api_running_field.md)。
    """
    try:
        import httpx

        resp = httpx.get(_JOBS_API_URL, timeout=5.0)
        resp.raise_for_status()
        jobs = resp.json()
    except Exception:  # noqa: BLE001 — 到達不能は安全側 (false) に倒す
        return False
    if not isinstance(jobs, list):
        return False
    for job in jobs:
        if not isinstance(job, dict) or job.get("id") not in _BRIEF_JOB_IDS:
            continue
        last_run = job.get("last_run")
        if isinstance(last_run, dict) and last_run.get("status") == "running":
            return True
    return False


async def _wait_until_safe_to_call_llm(poll_seconds: float = _BRIEF_POLL_SECONDS) -> None:
    """06:30/19:30 JST ±15分、または brief job 実行中なら待つ (本番との Ollama 衝突回避)。"""
    waited = False
    while True:
        now_jst = datetime.now(_JST)
        if not _near_brief_time(now_jst) and not _brief_job_running():
            if waited:
                print("  (再開: ブリーフ帯を抜けました)", file=sys.stderr)
            return
        waited = True
        when = now_jst.strftime("%H:%M")
        print(
            f"  (一時停止: 朝夕ブリーフ帯のため {poll_seconds:.0f}秒後に再確認、now={when} JST)",
            file=sys.stderr,
        )
        await asyncio.sleep(poll_seconds)


def _load_axes_cache(path: Path) -> dict[str, dict[str, str] | None]:
    cache: dict[str, dict[str, str] | None] = {}
    if not path.exists():
        return cache
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cache[str(rec.get("article_id"))] = rec.get("axes")
    return cache


def _append_axes_cache(
    path: Path, article_id: str, axes: dict[str, str] | None, model: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps({"article_id": article_id, "axes": axes, "model": model}, ensure_ascii=False)
        )
        f.write("\n")


async def fill_i58_axes_cache(
    rows: list[RoutingTimeRow], cache_path: Path
) -> dict[str, dict[str, str] | None]:
    """eligible な記事だけ i58 で軸を取り直し、cache (jsonl) に逐次 append する。

    concurrency は 1 (逐次 await)。既に cache にある article_id はスキップ (resumable)。
    """
    from src.cti.importance_v2_inline import axes_eligible, classify_axes_inline

    cache = _load_axes_cache(cache_path)
    for row in rows:
        if row.article_id in cache:
            continue
        if not axes_eligible(row.importance, row.category):
            # 本番でも対象外 (importance 帯外 / 非サイバー category) — 軸なしとして確定記録。
            _append_axes_cache(cache_path, row.article_id, None, "")
            cache[row.article_id] = None
            continue
        await _wait_until_safe_to_call_llm()
        axes, model = await classify_axes_inline(
            row.title, row.summary, importance=row.importance, category=row.category
        )
        _append_axes_cache(cache_path, row.article_id, axes, model)
        cache[row.article_id] = axes
    return cache


# ===== 集計 =====


@dataclass
class RoutingTimeReport:
    axes_mode: str
    days: int
    total_rows: int
    crosstab: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[str]] = field(default_factory=dict)
    per_day: dict[str, dict[str, int]] = field(default_factory=dict)


def _cell_key(old_ch: str, new_ch: str) -> str:
    return f"{old_ch or '(none)'} -> {new_ch or '(none)'}"


def run_routing_time_replay(
    rows: list[RoutingTimeRow],
    *,
    axes_mode: str,
    days: int,
    samples: int,
    i58_cache: dict[str, dict[str, str] | None] | None,
) -> RoutingTimeReport:
    """本番と同じ ``decide_channel`` を記事ごとに呼び、旧チャンネルとの食い違いを集計する。

    ``route``/``evaluate_routing_rules`` はここから直接呼ばない (tests/unit/
    test_m3b_routing_replay.py が import 検査で固定)。
    """
    from src.pipeline.briefing import decide_channel

    report = RoutingTimeReport(axes_mode=axes_mode, days=days, total_rows=len(rows))
    crosstab: dict[tuple[str, str], int] = defaultdict(int)
    samples_by_cell: dict[tuple[str, str], list[str]] = defaultdict(list)
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in rows:
        axes = row.stored_axes if axes_mode == "stored" else (i58_cache or {}).get(row.article_id)
        axes = axes or None
        msg = message_for_row(row)
        decision = decide_channel(
            msg,
            body_text=row.body,
            feed_title=row.feed_title,
            severity_axes=axes,
            article_id=row.article_id,
        )
        new_ch = decision.channel

        day = row.created_at[:10] if row.created_at else "(unknown)"
        cell = (row.old_channel, new_ch)
        crosstab[cell] += 1
        per_day[day][new_ch or "(none)"] += 1
        if row.old_channel != new_ch and len(samples_by_cell[cell]) < samples:
            samples_by_cell[cell].append(row.title)

    report.crosstab = {_cell_key(*k): v for k, v in crosstab.items()}
    report.samples = {_cell_key(*k): v for k, v in samples_by_cell.items()}
    report.per_day = {d: dict(v) for d, v in sorted(per_day.items())}
    return report


def _print_crosstab(title: str, crosstab: dict[str, int]) -> None:
    print(f"\n{title}")
    for cell, count in sorted(crosstab.items(), key=lambda kv: -kv[1]):
        print(f"  {cell:<30} {count:>6}")


def _print_samples(title: str, samples: dict[str, list[str]]) -> None:
    print(f"\n{title}")
    for cell, titles in samples.items():
        print(f"\n  {cell}:")
        for t in titles:
            print(f"    - {t}")


_DAILY_VOLUME_CHANNELS: tuple[str, ...] = ("alert", "vuln", "japan_watch")


def _print_per_day(title: str, per_day: dict[str, dict[str, int]]) -> None:
    print(title)
    for day, counts in per_day.items():
        parts = ", ".join(f"{ch}={counts.get(ch, 0)}" for ch in _DAILY_VOLUME_CHANNELS)
        print(f"  {day}: {parts}")


def run_and_report(args: argparse.Namespace) -> None:
    since = datetime.now(UTC) - timedelta(days=args.days)
    want_stored_axes = args.axes == "stored"
    rows = fetch_routing_time_rows(
        since=since, db_path=args.db_path, want_stored_axes=want_stored_axes
    )
    print(f"対象記事 (axes={args.axes}): {len(rows)} 件 (直近 {args.days} 日)")
    if not rows:
        return

    i58_cache: dict[str, dict[str, str] | None] | None = None
    if args.axes == "i58":
        i58_cache = asyncio.run(fill_i58_axes_cache(rows, args.axes_cache))

    report = run_routing_time_replay(
        rows, axes_mode=args.axes, days=args.days, samples=args.samples, i58_cache=i58_cache
    )

    _print_crosstab("旧 (投稿時点) × 新 (decide_channel, v13) チャンネルの件数:", report.crosstab)
    _print_samples(f"食い違ったセルのサンプル (各最大 {args.samples}件):", report.samples)
    _print_per_day("\n日次 alert/vuln/japan_watch 件数:", report.per_day)

    out_path = Path("data/eval") / f"m3b_routing_time_{args.axes}_{args.days}d.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "axes_mode": report.axes_mode,
                "days": report.days,
                "total_rows": report.total_rows,
                "crosstab": report.crosstab,
                "samples": report.samples,
                "per_day": report.per_day,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n詳細を {out_path} に保存しました。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES_PER_CELL)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--axes",
        choices=("stored", "i58"),
        default="stored",
        help="深刻度の軸を保存済み値 (stored) か i58 再計算 (i58) にするか",
    )
    parser.add_argument("--axes-cache", type=Path, default=DEFAULT_AXES_CACHE_PATH)
    args = parser.parse_args()
    run_and_report(args)


if __name__ == "__main__":
    main()
