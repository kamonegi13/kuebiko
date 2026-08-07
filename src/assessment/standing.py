"""常設情報要求 (standing situations) — 段A: 器と収穫 (2026-07-13)。

設計: docs/prepositioning_posture_ledger_design.md。
「国家 N は日本の重要インフラへの事前配置を進めているか」を ``kind='standing'`` の
常設 situation として台帳に持つ。段A のスコープは **開設 (code 所有 seed) と決定論の
証拠収穫 (R1-R3) のみ** — 評価 (ACH) は段B。呼出側 (stateful/ledger) が standing を
汎用 matcher・再評価・lifecycle sweep から除外する。

収穫は汎用 assignment matcher に相乗りしない (国 anchor による戦線バケット吸着の
再発防止、設計 §3.1)。R1-R3 はすべて辞書ゲート済み帰属のみを使う (確定原則)。
flag ``STANDING_SITUATIONS`` (既定 off)。rollback = flag off (収穫停止、データは残る)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.cti.nisc_sectors import _CANONICAL_TO_NISC
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.situation_store import SituationStore
    from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

STANDING_KIND = "standing"
_FLAG_ENV = "STANDING_SITUATIONS"

# 収穫規則 (設計 §3.2)
_ADJACENT_INTENTS = ("espionage", "disruption")  # R2: 侵入の目的は後から判明する
_HARVEST_CAP_PER_SITUATION = 30  # 1 run × 1 standing の上限 (超過は detection_log に記録)
_CANDIDATE_POOL_LIMIT = 400


@dataclass(frozen=True)
class StandingSeed:
    """code 所有の常設問い 1 件 (ユーザー定義不可 — 増減はコード変更 = レビューを通る)。"""

    situation_id: str  # 明示の安定 id (title ハッシュではない — title は claim 追従で動きうる)
    nation: str  # lowercase (threat_actor_doctrine.STATE_NATIONS と同じ語彙)
    title: str
    pir_ids: tuple[str, ...]


# 敵性 4 カ国 × 1 question (確定原則: 敵性国家限定・国粒度。64 粒度は却下済み §9)
STANDING_SEEDS: tuple[StandingSeed, ...] = (
    StandingSeed(
        "s-standing-prepos-cn",
        "cn",
        "中国の国家アクターは日本の重要インフラに対する事前配置を進めているか",
        ("pir_china_apt", "pir_critical_infra"),
    ),
    StandingSeed(
        "s-standing-prepos-kp",
        "kp",
        "北朝鮮の国家アクターは日本の重要インフラに対する事前配置を進めているか",
        ("pir_dprk_apt", "pir_critical_infra"),
    ),
    StandingSeed(
        "s-standing-prepos-ru",
        "ru",
        "ロシアの国家アクターは日本の重要インフラに対する事前配置を進めているか",
        ("pir_russia_apt", "pir_critical_infra"),
    ),
    StandingSeed(
        "s-standing-prepos-ir",
        "ir",
        "イランの国家アクターは日本の重要インフラに対する事前配置を進めているか",
        ("pir_critical_infra",),
    ),
)


def standing_enabled() -> bool:
    """flag ``STANDING_SITUATIONS`` (既定 off)。"""
    return os.environ.get(_FLAG_ENV, "0").strip() == "1"


def ensure_standing_situations(*, store: SituationStore, now_iso: str) -> int:
    """seed を冪等に開設する。返り値 = 新規開設数。"""
    opened = 0
    for seed in STANDING_SEEDS:
        if store.get_situation(seed.situation_id) is not None:
            continue
        store.open_situation(
            title=seed.title,
            domain="cyber_incident",
            anchors=frozenset({f"nation:{seed.nation.upper()}"}),
            pir_ids=seed.pir_ids,
            now_iso=now_iso,
            kind=STANDING_KIND,
            situation_id=seed.situation_id,
        )
        opened += 1
    if opened:
        _log.info("standing_situations_opened", opened=opened)
    return opened


@lru_cache(maxsize=1)
def _actor_lookup() -> dict[str, str]:
    """actor 辞書から id/canonical/alias → nation (lowercase) を構築。

    subprocess 単位で 1 回だけ読む (lru_cache)。辞書ゲート済み帰属のみを使う確定原則の
    実装 — entity value (actor id) が辞書に無ければ nation 解決されず収穫対象外。
    """
    from src.cti.actor_normalizer import load_actor_aliases

    nation_by_key: dict[str, str] = {}
    for actor in load_actor_aliases().actors:
        nation = (actor.nation or "").strip().lower()
        if not nation:
            continue
        names = {actor.id.lower(), actor.canonical.lower(), *(a.lower() for a in actor.aliases)}
        for k in names:
            nation_by_key.setdefault(k, nation)
    return nation_by_key


def _fetch_candidates(db_path: Path, *, since_iso: str) -> list[dict[str, Any]]:
    """収穫候補 (posted × 窓内 × R1-R3 の粗 filter を SQL で先掛け)。"""
    from src.storage.db_backend import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT article_id, socio_political_intent, victim_country_iso,"
            " victim_sector_canonical"
            " FROM articles"
            " WHERE status = 'posted' AND datetime(created_at) >= datetime(?)"
            # recap (まとめ/ニュースレター) は一次証拠でない (較正 2026-07-13)
            " AND COALESCE(category, '') != 'recap'"
            " AND (socio_political_intent IN ('prepositioning', 'espionage', 'disruption')"
            "      OR victim_country_iso = 'JP')"
            " ORDER BY datetime(created_at) DESC LIMIT ?",
            (since_iso, _CANDIDATE_POOL_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "article_id": str(r[0]),
            "intent": r[1],
            "victim_country": r[2],
            "victim_sector": r[3],
        }
        for r in rows
    ]


def _match_rules(
    row: dict[str, Any],
    entity_keys: set[str],
    *,
    nation: str,
    nation_by_key: dict[str, str],
) -> str | None:
    """記事 1 件を standing (国家 N) の収穫規則 R1-R3 に照合する (決定論)。"""
    parts = [k.partition(":") for k in entity_keys]
    actor_values = {v.lower() for t, _, v in parts if t == "actor"}
    involved = {v.upper() for t, _, v in parts if t == "involved_country"}
    actor_nation_hit = any(nation_by_key.get(a) == nation for a in actor_values)
    nation_hit = actor_nation_hit or nation.upper() in involved

    intent = str(row.get("intent") or "")
    sector = str(row.get("victim_sector") or "")
    is_ci_sector = sector in _CANONICAL_TO_NISC

    # R1 直接: prepositioning intent (全確度 — low は仮説級として ACH が重みを判断)
    if intent == "prepositioning" and nation_hit:
        return "R1"
    # R2 隣接動機: 辞書ゲート済み国家アクター × **観測された** NISC 分野。
    # 較正 (07-13 実データ): doctrine アクター言及のみのアーム は sector NULL の一般
    # malware 記事まで乗せた (Vidar/roundup 型ノイズ) ため撤去 — doctrine は board の
    # 世界行動レーンが担い、posture 証拠は観測自体に CI 文脈を要求する。
    if intent in _ADJACENT_INTENTS and actor_nation_hit and is_ci_sector:
        return "R2"
    # R3 JP 観測: 帰属済みのみ (帰属なし JP 事象は standing に入れない — 過剰帰属の再発防止)
    if str(row.get("victim_country") or "") == "JP" and is_ci_sector and actor_nation_hit:
        return "R3"
    return None


def harvest_standing_evidence(
    *,
    store: SituationStore,
    repo: RunHistoryRepository,
    db_path: Path,
    now_iso: str,
    lookback_hours: float,
) -> int:
    """開設済み standing への証拠収穫 (決定論・LLM 不使用)。返り値 = 新規追加証拠数。"""
    seeds = [s for s in STANDING_SEEDS if store.get_situation(s.situation_id) is not None]
    if not seeds:
        return 0
    since = datetime.fromisoformat(now_iso).astimezone(UTC) - timedelta(hours=lookback_hours)
    candidates = _fetch_candidates(db_path, since_iso=since.isoformat())
    if not candidates:
        return 0
    entities_by_id = repo.entity_keys_for_articles(
        [c["article_id"] for c in candidates], types=("actor", "involved_country")
    )
    nation_by_key = _actor_lookup()

    added_total = 0
    for seed in seeds:
        matches: list[tuple[str, str]] = []
        for c in candidates:
            aid = str(c["article_id"])
            rule = _match_rules(
                c,
                entities_by_id.get(aid, set()),
                nation=seed.nation,
                nation_by_key=nation_by_key,
            )
            if rule is not None:
                matches.append((aid, rule))
        added = 0
        for aid, _rule in matches[:_HARVEST_CAP_PER_SITUATION]:
            if store.record_assignment(
                situation_id=seed.situation_id,
                article_id=aid,
                added_at=now_iso,
                assigned_by="standing",
            ):
                added += 1
        if added:
            store.touch_situation(seed.situation_id, last_evidence_at=now_iso)
        if len(matches) > _HARVEST_CAP_PER_SITUATION:
            # no silent caps (設計 §3.2): 切り詰めは detection_log に記録して監査可能に
            dropped = len(matches) - _HARVEST_CAP_PER_SITUATION
            _log.warning("standing_harvest_capped", situation_id=seed.situation_id, dropped=dropped)
            store.log_detection(
                run_at=now_iso,
                article_id=matches[_HARVEST_CAP_PER_SITUATION][0],
                decision="rejected",
                reason=f"standing_cap:{seed.situation_id}:+{dropped}",
                situation_id=seed.situation_id,
            )
        added_total += added
    if added_total:
        _log.info("standing_evidence_harvested", added=added_total, seeds=len(seeds))
    return added_total


# ---------- 段B: 評価キュー (予約枠) と較正済み確度 cap ----------

_STALE_DAYS = 7  # 新着ゼロでも週 1 回は必ず問い直す (設計 §4.3)
_DEFAULT_RESERVE = 2  # daily cap 6 の内数予約 (総 LLM 呼出は増えない)
_RESERVE_ENV = "STANDING_RESERVE"

# H-P1 (日本 CI への事前配置が進行中) の確度 cap 判定に使う leading id
POSTURE_ACTIVE_JP = "posture_active_prepositioning_jp"


def standing_reserve() -> int:
    """standing 予約枠 (env `STANDING_RESERVE` で一時上書き可、手動初回評価用)。"""
    raw = os.environ.get(_RESERVE_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_RESERVE


def select_standing_reassessments(
    *,
    standing_rows: list[Any],
    unassessed: dict[str, list[str]],
    latest_rev_at: dict[str, str],
    now_iso: str,
    reserve: int,
) -> list[str]:
    """standing の評価対象を予約枠内で選ぶ (純粋関数)。

    優先度: 初回 (revision 無し) → 未評価証拠が多い → 最終判定が古い。
    staleness: 最終判定から _STALE_DAYS 超は新着ゼロでも候補 (証拠涸れでも問いは生きる)。
    """
    stale_before = (
        datetime.fromisoformat(now_iso).astimezone(UTC) - timedelta(days=_STALE_DAYS)
    ).isoformat()
    candidates: list[str] = []
    for row in standing_rows:
        sid = row.situation_id
        rev_at = latest_rev_at.get(sid)
        if rev_at is None or unassessed.get(sid) or rev_at < stale_before:
            candidates.append(sid)
    order = sorted(
        candidates,
        key=lambda sid: (
            0 if latest_rev_at.get(sid) is None else 1,
            -len(unassessed.get(sid, [])),
            latest_rev_at.get(sid, ""),
            sid,
        ),
    )
    return order[:reserve]


def has_jp_direct_evidence(*, db_path: Path, situation_id: str) -> bool:
    """standing の証拠に日本 victim の帰属済み観測 (R3) があるか (決定論)。

    H-P1 を leading にする場合、JP 直接証拠が無ければ確度は moderate 上限 (設計 §4.2、
    方向中立 cap — 逆に観測ゼロを「配置なし」の支持にもしない)。
    """
    from src.storage.db_backend import connect

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM situation_evidence e JOIN articles a ON a.article_id = e.article_id"
            " WHERE e.situation_id = ? AND a.victim_country_iso = 'JP' LIMIT 1",
            (situation_id,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None
