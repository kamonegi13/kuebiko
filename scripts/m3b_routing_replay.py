"""M3b: 過去の投稿済み記事に M3B_RULES (v13) を当て直し、現行ルール (v12) との食い違いを見る
(read-only)。

対象: 直近 N 日 (既定 30)、posted_channel が非 NULL、``article_importance_v2`` に記録済みの
記事 (同 article_id は最新 1 行)。``articles`` に保存済みの値 (importance / article_type /
posted_channel / routing_rule_id) と ``article_importance_v2`` (severity / severity_basis /
jp / relevant) から ``RoutingSignals`` を組み立て、``src/cti/routing_v2.M3B_RULES`` を評価する。

DB への書き込みは一切行わない (SELECT のみ)。

使い方::

    uv run python scripts/m3b_routing_replay.py [--days 30] [--samples 5]

または (本番 DB に read-only で当てる場合、コンテナ内)::

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro \\
        kuebiko python scripts/m3b_routing_replay.py

## --routing-time (2026-10-10)

既存 (既定) の replay は ``article_importance_v2`` の**記録済みの値**を当て直すだけなので、
以下 2 点を過大に見積もる:

1. ``sir_ids`` — 記録済みの値には投稿**後**に付く SIR タグ (LLM judge の主題アクター判定・
   夜間 full-replace rebuild) まで混じる。routing 時点で使えるのは規則ベースの SIR 照合だけ
   (``src/cti/importance_v2_inline.py`` が ``rule_based_sir_ids(..., subject_actor_ids=None)``
   で評価しているのと同じ)。
2. 深刻度の軸 (severity axes) — 古い記事の記録済みの軸は旧モデル (s21) 由来。routing 時点の
   本番は ``kuebiko-sft:i58`` (``build_llm_for(Step.SEVERITY_AXES, ...)``) を使う。

``--routing-time`` は、記事ごとに ``src/cti/importance_v2_inline.build_importance_v2_inline``
と**同じ組み立て**を DB の保存済み値から再現する (同じ helper — ``build_inputs`` /
``rule_based_sir_ids`` / ``derive_mention_entities`` — を再利用し、入力の作り方だけを
「保存済みの BriefingMessage」から「DB の articles / article_entities / article_severity_axes
行」に変える)。相違点:

- ``actor_values`` (SIR 照合の検出アクター) と関与国 (``involved_countries``) は
  ``article_entities`` (``entity_type='actor'`` / ``'involved_country'``) から読む。これらは
  ingest 時点で付くエンティティで、SIR タグのような投稿後の再付与対象ではないため、ここでの
  「保存済み値を使う」ことによる過大評価は severity axes や SIR ほど大きくない。
- CVE 集合は本文 regex (``_CVE_TOKEN_RE`` と同一パターン) ∪ ``article_entities``
  (``entity_type='cve'``) — ``msg.iocs`` (実行時の LLM 抽出値) の代用。
- KEV catalog / NVD CVSS cache は **replay 実行時点の最新**を使う (過去のある時点の
  スナップショットではない)。これは既存の記録済み値 replay にも共通する限界 — stored
  severity/severity_basis も生成当時の KEV/NVD で確定済みの値であり本質的に同じ近似。
- ``subject_kev`` の年判定 (``SUBJECT_CVE_YEARS``) は記事の ``created_at`` 年を使う
  (「routing 時点の "今年"」を近似。replay 実行日の年ではない)。

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
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if TYPE_CHECKING:
    from src.cti.importance_v2 import ImportanceInputs, ImportanceV2
    from src.cti.routing_signals import RoutingSignals

DEFAULT_DB_PATH = Path("data/run_history.db")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SAMPLES_PER_CELL = 5
DEFAULT_AXES_CACHE_PATH = Path("data/eval/m3b_rt_axes_cache.jsonl")
#: DB IN (...) の chunk サイズ (src/storage/repo_severity_axes.py の _CHUNK と同じ値)。
_DB_CHUNK = 400

_SQL = """
SELECT article_id, title, importance, article_type, posted_channel, routing_rule_id,
       severity, severity_basis, jp, relevant
FROM (
    SELECT a.article_id, a.title, a.importance, a.article_type, a.posted_channel,
           a.routing_rule_id, v.severity, v.severity_basis, v.jp, v.relevant,
           ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
    FROM articles a
    JOIN article_importance_v2 v ON v.article_id = a.article_id
    WHERE a.created_at >= ? AND a.posted_channel IS NOT NULL
) t
WHERE rn = 1
"""  # noqa: S608 — 固定 SQL (バインドは since のみ)

#: --routing-time 用の拡張 SQL。importance_v2_inline.build_importance_v2_inline が
#: BriefingMessage から読む材料 (title/summary/body/feed_title/category/article_type/
#: victim_sector_canonical/victim_country_iso/socio_political_intent/is_ransomware) を
#: articles から取る。severity/severity_basis/jp/relevant は「保存済み値 replay」との
#: 比較用に引き続き同梱する。
_SQL_ROUTING_TIME = """
SELECT article_id, title, summary, body, feed_title, category, article_type,
       importance, posted_channel, created_at,
       victim_sector_canonical, victim_country_iso,
       socio_political_intent, is_ransomware,
       severity, severity_basis, jp, relevant
FROM (
    SELECT a.article_id, a.title, a.summary, a.body, a.feed_title, a.category,
           a.article_type, a.importance, a.posted_channel, a.created_at,
           a.victim_sector_canonical, a.victim_country_iso,
           a.socio_political_intent, a.is_ransomware,
           v.severity, v.severity_basis, v.jp, v.relevant,
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

#: 本文中の CVE トークン (src/cti/routing_signals._CVE_TOKEN_RE と同一パターンの複製。
#: private 属性の cross-module import を避けるため、値を複製して保持する)。
_CVE_TOKEN_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)

_JST = ZoneInfo("Asia/Tokyo")
_BRIEF_TIMES_JST: tuple[tuple[int, int], ...] = ((6, 30), (19, 30))
_BRIEF_WINDOW_MINUTES = 15
_BRIEF_JOB_IDS = frozenset({"morning-brief", "evening-brief"})
_JOBS_API_URL = "http://127.0.0.1:8001/api/v1/jobs"
_BRIEF_POLL_SECONDS = 30.0


@dataclass(frozen=True)
class ReplayRow:
    article_id: str
    title: str
    importance: str
    article_type: str
    old_channel: str
    old_rule_id: str
    severity: str
    severity_basis: str
    jp: str
    relevant: bool


@dataclass(frozen=True)
class RoutingTimeRow:
    """``--routing-time`` 用の 1 記事分の材料 (DB から組み立てた BriefingMessage 代替)。"""

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
    # 保存済み値 (「保存済み値 replay」との比較用)。
    stored_severity: str
    stored_severity_basis: str
    stored_jp: str
    stored_relevant: bool
    actor_ids: frozenset[str] = field(default_factory=frozenset)
    involved_countries: frozenset[str] = field(default_factory=frozenset)
    persisted_cves: frozenset[str] = field(default_factory=frozenset)
    stored_axes: dict[str, str] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return f"{self.title}\n{self.summary}\n{self.body}"


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def fetch_rows(*, since: datetime, db_path: Path = DEFAULT_DB_PATH) -> list[ReplayRow]:
    """対象記事を取得する (read-only)。"""
    with _connect(db_path) as con:
        rows = con.execute(_SQL, (since.isoformat(),)).fetchall()
    return [
        ReplayRow(
            article_id=str(r["article_id"]),
            title=str(r["title"] or ""),
            importance=str(r["importance"] or ""),
            article_type=str(r["article_type"] or ""),
            old_channel=str(r["posted_channel"] or ""),
            old_rule_id=str(r["routing_rule_id"] or ""),
            severity=str(r["severity"] or ""),
            severity_basis=str(r["severity_basis"] or ""),
            jp=str(r["jp"] or ""),
            relevant=bool(r["relevant"]),
        )
        for r in rows
    ]


def new_channel_for(
    row: ReplayRow, *, rules: list[dict[str, Any]] | None = None
) -> tuple[str, str]:
    """ルール (既定 M3B_RULES) を保存済みの値で評価して (channel, rule_id) を返す。"""
    from src.cti.router import get_source_quality
    from src.cti.routing_rules import evaluate_routing_rules
    from src.cti.routing_signals import RoutingSignals
    from src.cti.routing_v2 import M3B_RULES

    signals = RoutingSignals(
        source="briefing",
        importance=row.importance if row.importance in ("high", "medium", "low") else "low",  # type: ignore[arg-type]
        article_type=row.article_type,  # type: ignore[arg-type]
        severity=row.severity,
        severity_basis=row.severity_basis,
        jp=row.jp,
        relevant=row.relevant,
    )
    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=rules or M3B_RULES)
    if decision is None:
        return "", ""
    return decision.channel, decision.rule_id


# ===== --routing-time: DB から BriefingMessage 代替を組み立てる =====


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
    """``--routing-time`` 用の対象記事を取得する (read-only、articles + 付帯 entity/axes)。"""
    with _connect(db_path) as con:
        base_rows = con.execute(_SQL_ROUTING_TIME, (since.isoformat(),)).fetchall()
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
                stored_severity=str(r["severity"] or ""),
                stored_severity_basis=str(r["severity_basis"] or ""),
                stored_jp=str(r["jp"] or ""),
                stored_relevant=bool(r["relevant"]),
                actor_ids=ent["actor"],
                involved_countries=ent["involved_country"],
                persisted_cves=ent["cve"],
                stored_axes=axes_map.get(aid, {}),
            )
        )
    return out


def _article_year(created_at: str) -> int:
    """``created_at`` の年 (parse 失敗は replay 実行時点の UTC 年、SUBJECT_CVE_YEARS 判定用)。"""
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).year
    except ValueError:
        return datetime.now(UTC).year


def _article_data_for_sir(row: RoutingTimeRow) -> dict[str, Any]:
    """``rule_based_sir_ids`` が読む article_data (importance_v2_inline と同じ形)。

    ``subject_actor_ids=None`` / ``subject_actor_source=None`` — 主題アクター層は routing
    時点ではまだ未確定 (legacy keyword 照合にフォールバック、inline と同じ挙動)。
    """
    return {
        "title": row.title,
        "summary": row.summary,
        "body": row.body,
        "feed_title": row.feed_title,
        "victim_sector_canonical": row.victim_sector or None,
        "victim_country_iso": row.victim_country or None,
        "article_type": row.article_type or None,
        "category": row.category or None,
        "socio_political_intent": row.socio_political_intent or None,
        "is_ransomware": row.is_ransomware,
        "subject_actor_ids": None,
        "subject_actor_source": None,
    }


def _compute_sir_ids(row: RoutingTimeRow) -> frozenset[str]:
    from src.pipeline.persistence import rule_based_sir_ids

    try:
        return rule_based_sir_ids(_article_data_for_sir(row), set(row.actor_ids))
    except Exception:  # noqa: BLE001 — SIR 評価の失敗で replay を止めない (inline と同じ fail-safe)
        return frozenset()


def _compute_cves(row: RoutingTimeRow) -> set[str]:
    found = {m.group(0).upper() for m in _CVE_TOKEN_RE.finditer(row.full_text)}
    return found | set(row.persisted_cves)


def build_importance_inputs(row: RoutingTimeRow, axes: dict[str, str] | None) -> ImportanceInputs:
    """``importance_v2_inline.build_importance_v2_inline`` と同じ組み立て (DB 行から)。"""
    from src.cti.importance_v2 import build_inputs
    from src.cti.mention_tagger import derive_mention_entities
    from src.tools.kev_client import get_kev_cve_set
    from src.tools.nvd_client import max_cvss as nvd_max_cvss

    cves = _compute_cves(row)
    kev = get_kev_cve_set()
    mention_entities = derive_mention_entities(
        title=row.title,
        summary=row.summary,
        involved_isos={c.lower() for c in row.involved_countries},
    )
    mentioned = [v for (etype, v) in mention_entities if etype == "mentioned_country"]
    sir_ids = _compute_sir_ids(row)

    return build_inputs(
        category=row.category,
        article_type=row.article_type,
        axes=axes,
        cves=cves,
        kev=kev,
        year=_article_year(row.created_at),
        victim_country=row.victim_country,
        involved_countries=row.involved_countries,
        mentioned_countries=mentioned,
        sir_ids=sir_ids,
        text=row.full_text,
        stored_max_cvss=nvd_max_cvss(list(cves)),
    )


def routing_signals_from_importance_v2(row: RoutingTimeRow, iv2: ImportanceV2) -> RoutingSignals:
    from src.cti.routing_signals import RoutingSignals

    return RoutingSignals(
        source="briefing",
        importance=row.importance if row.importance in ("high", "medium", "low") else "low",  # type: ignore[arg-type]
        article_type=row.article_type,  # type: ignore[arg-type]
        severity=iv2.severity or "",
        severity_basis=iv2.severity_basis,
        jp=iv2.jp,
        relevant=iv2.relevant,
        victim_sector=row.victim_sector,
    )


def decide_m3b_rules(signals: RoutingSignals) -> tuple[str, str]:
    from src.cti.router import get_source_quality
    from src.cti.routing_rules import evaluate_routing_rules
    from src.cti.routing_v2 import M3B_RULES

    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=M3B_RULES)
    if decision is None:
        return "", ""
    return decision.channel, decision.rule_id


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
    stored_vs_routing_time: dict[str, int] = field(default_factory=dict)


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
    report = RoutingTimeReport(axes_mode=axes_mode, days=days, total_rows=len(rows))
    crosstab: dict[tuple[str, str], int] = defaultdict(int)
    samples_by_cell: dict[tuple[str, str], list[str]] = defaultdict(list)
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    stored_alert = routing_time_alert = 0

    for row in rows:
        axes = row.stored_axes if axes_mode == "stored" else (i58_cache or {}).get(row.article_id)
        axes = axes or None
        inputs = build_importance_inputs(row, axes)
        from src.cti.importance_v2 import derive

        iv2 = derive(inputs)
        signals = routing_signals_from_importance_v2(row, iv2)

        new_ch, _ = decide_m3b_rules(signals)

        day = row.created_at[:10] if row.created_at else "(unknown)"
        cell = (row.old_channel, new_ch)
        crosstab[cell] += 1
        per_day[day][new_ch or "(none)"] += 1
        if row.old_channel != new_ch and len(samples_by_cell[cell]) < samples:
            samples_by_cell[cell].append(row.title)

        if new_ch == "alert":
            routing_time_alert += 1
        # 保存済み値 (M3B_RULES, 既存 replay 相当) との比較用。
        stored_signals = routing_signals_from_importance_v2(
            row,
            _stored_importance_v2(row),
        )
        stored_ch, _ = decide_m3b_rules(stored_signals)
        if stored_ch == "alert":
            stored_alert += 1

    report.crosstab = {_cell_key(*k): v for k, v in crosstab.items()}
    report.samples = {_cell_key(*k): v for k, v in samples_by_cell.items()}
    report.per_day = {d: dict(v) for d, v in sorted(per_day.items())}
    report.stored_vs_routing_time = {
        "stored_values_replay_alert_count": stored_alert,
        "routing_time_m3b_rules_alert_count": routing_time_alert,
        "alerts_lost_vs_stored_m3b_rules": stored_alert - routing_time_alert,
    }
    return report


def _stored_importance_v2(row: RoutingTimeRow) -> ImportanceV2:
    """保存済みの severity/severity_basis/jp/relevant から ImportanceV2 を組み立てる (比較用)。

    sir_ids/nations/strategic_weight は比較対象の計算 (decide_m3b_rules) が使わないので
    空のままでよい。
    """
    from src.cti.importance_v2 import ImportanceV2

    return ImportanceV2(
        severity=row.stored_severity or None,  # type: ignore[arg-type]
        severity_basis=row.stored_severity_basis,
        strategic_weight=None,
        jp=row.stored_jp or "none",  # type: ignore[arg-type]
        nations=(),
        sir_ids=(),
        relevant=row.stored_relevant,
    )


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


def run_and_report_routing_time(args: argparse.Namespace) -> None:
    since = datetime.now(UTC) - timedelta(days=args.days)
    want_stored_axes = args.axes == "stored"
    rows = fetch_routing_time_rows(
        since=since, db_path=args.db_path, want_stored_axes=want_stored_axes
    )
    print(f"対象記事 (routing-time, axes={args.axes}): {len(rows)} 件 (直近 {args.days} 日)")
    if not rows:
        return

    i58_cache: dict[str, dict[str, str] | None] | None = None
    if args.axes == "i58":
        i58_cache = asyncio.run(fill_i58_axes_cache(rows, args.axes_cache))

    report = run_routing_time_replay(
        rows, axes_mode=args.axes, days=args.days, samples=args.samples, i58_cache=i58_cache
    )

    _print_crosstab("旧 (v12) × 新 (M3B_RULES) チャンネルの件数:", report.crosstab)
    _print_samples(
        f"食い違ったセルのサンプル (M3B_RULES, 各最大 {args.samples}件):", report.samples
    )
    _print_per_day("\n日次 alert/vuln/japan_watch 件数 (M3B_RULES):", report.per_day)
    print("\n保存済み値 replay との比較:")
    for k, v in report.stored_vs_routing_time.items():
        print(f"  {k}: {v}")

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
                "stored_vs_routing_time": report.stored_vs_routing_time,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n詳細を {out_path} に保存しました。")


# ===== 既存 (保存済み値) replay =====


def run_legacy_replay(args: argparse.Namespace) -> None:
    since = datetime.now(UTC) - timedelta(days=args.days)
    rows = fetch_rows(since=since, db_path=args.db_path)
    print(f"対象記事: {len(rows)} 件 (直近 {args.days} 日、article_importance_v2 記録済み)")
    if not rows:
        return

    crosstab: dict[tuple[str, str], int] = defaultdict(int)
    samples: dict[tuple[str, str], list[str]] = defaultdict(list)
    would_be_alert_low = 0

    for row in rows:
        new_channel, _new_rule_id = new_channel_for(row)
        cell = (row.old_channel, new_channel)
        crosstab[cell] += 1
        if row.old_channel != new_channel and len(samples[cell]) < args.samples:
            samples[cell].append(row.title)
        # publish._resolve_channel は importance=low の alert を default へ降格する
        # (衛生ゲート)。new_channel='alert' かつ importance='low' は「shadow 上は alert
        # だが本番導入時は降格される」件数として別途数える。
        if new_channel == "alert" and row.importance == "low":
            would_be_alert_low += 1

    print("\n旧 (v12) × 新 (M3B_RULES) チャンネルの件数:")
    print(f"{'old_channel':<16} {'new_channel':<16} {'count':>6}")
    for (old_ch, new_ch), count in sorted(crosstab.items(), key=lambda kv: -kv[1]):
        print(f"{old_ch or '(none)':<16} {new_ch or '(none)':<16} {count:>6}")

    print(f"\n食い違ったセルのサンプル (各最大 {args.samples}件):")
    for cell, titles in samples.items():
        old_ch, new_ch = cell
        print(f"\n  {old_ch or '(none)'} -> {new_ch or '(none)'}:")
        for t in titles:
            print(f"    - {t}")

    print(
        f"\nnew_channel='alert' だが importance='low' (本番導入時に publish._resolve_channel "
        f"が default へ降格する件数): {would_be_alert_low}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES_PER_CELL)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--routing-time",
        action="store_true",
        help="保存済みの article_importance_v2 ではなく、routing 時点と同じ材料で再計算する",
    )
    parser.add_argument(
        "--axes",
        choices=("stored", "i58"),
        default="stored",
        help="--routing-time 専用: 深刻度の軸を保存済み値 (stored) か i58 再計算 (i58) にするか",
    )
    parser.add_argument("--axes-cache", type=Path, default=DEFAULT_AXES_CACHE_PATH)
    args = parser.parse_args()

    if args.routing_time:
        run_and_report_routing_time(args)
    else:
        run_legacy_replay(args)


if __name__ == "__main__":
    main()
