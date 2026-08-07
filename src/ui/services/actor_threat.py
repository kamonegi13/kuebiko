"""アクター・ミッション脅威評価 — 関連度 × 能力の透明ティア + 活動状態の併記。

docs/actor_mission_threat_design.md の実装。「脅威をそのまま捉えたときに必要な
脅威度が算出される」ことが要件であり、以下を設計不変条件とする:

- **強制バイアス禁止**: 対日関連度の証拠は (a) 公的一次ソースの名指し (doctrine)、
  (b) 観測された被害事実 (victim_country_iso / victim_sector_canonical) のみ。
  自前の routing 判断 (japan_watch 配信 = 自分の関心) は証拠に使わない — 関心が
  脅威度を釣り上げる自己参照ループを構造的に排除する。下駄係数も置かない。
- **辞書=knowledge / アクター=observation**: ティアは保存せず毎回計算する。
- **暗域=不明≠安全**: 能力 unknown は low と別扱い。能力の証拠不足でティアは
  下がらない (関連度がフロアを決める)。休眠 (観測 0) はティアに影響しない —
  活動状態として併記する (`Critical・休眠` が成立する)。
- **全決定論・LLM 不使用**。重み付き合成でなく バンド × 規則表 (salience と同じく
  定数はコード所有)。駆動要因 (なぜその評価か) を常時併記する。

観測系入力は 90 日固定窓 (``ASSESSMENT_WINDOW_DAYS``) — UI の表示窓に追随させると
窓切替でティアが flap し「脅威度」の意味が壊れるため。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.cti.actor_normalizer import ActorAlias
from src.cti.attack_catalog import is_ics_technique
from src.cti.threat_actor_doctrine import (
    ALLIED_NATIONS,
    STATE_ACTOR_SECTORS,
    is_state_actor,
    jp_targeting_grounds,
    prepositioning_grounds,
)
from src.logging_config import get_logger
from src.ui.services.threat_operations import (
    DEFAULT_DB_PATH,
    ActorActivity,
)

_log = get_logger(__name__)

# ---- 評価ウィンドウ / 閾値 (較正 knob は設計 §7 の 3 箇所のみ: 下記 + 規則表) ----

ASSESSMENT_WINDOW_DAYS = 90

# 防衛/政府 nexus の sector canonical 値 (config/victim_sectors.yaml の語彙)
DEFGOV_SECTORS: frozenset[str] = frozenset({"defense", "government"})

# 日本の防衛/政府被害は単発でも重大 (JAXA 型の実事象は稀かつ決定的) — 閾値 1。
JP_DEFGOV_MIN = 1
# 日本一般被害の「持続的標的」認定 (単発報道ノイズと区別)
JP_SUSTAINED_MIN = 3
# 同盟国 def/gov は単発報道ノイズ対策で 2 (日本と違い単発の mission 直接性が低い)
ALLIED_DEFGOV_MIN = 2
# 同盟国一般被害 (R1 周辺シグナル)
ALLIED_GENERAL_MIN = 2
# 観測 intent 由来のシグナルは単発誤分類でティアが跳ねないよう 2 件以上を要求
INTENT_MIN_COUNT = 2

# 能力シグナル閾値
CAP_TTP_BREADTH_MIN = 15
CAP_MALWARE_MIN = 3
CAP_SIGNALS_HIGH = 3

# 活動状態 (ティア非影響)
ACTIVE_MIN_ARTICLES = 3

Tier = Literal["critical", "high", "moderate", "watch"]
CapabilityBand = Literal["high", "medium", "low", "unknown"]
ActivityState = Literal["spiking", "active", "quiet", "dormant"]


@dataclass(frozen=True)
class ActorThreatAssessment:
    """1 actor のミッション脅威評価 (計算値、保存しない)。tier=None は「未評価」。"""

    tier: Tier | None
    tier_rule: str  # 適用された規則表の行 (透明性: なぜこのティアか)
    relevance_band: int  # 0-3
    capability_band: CapabilityBand
    activity_state: ActivityState
    relevance_factors: tuple[str, ...]
    capability_factors: tuple[str, ...]
    activity_factors: tuple[str, ...]
    coverage_note: str | None  # データ不足の注記 (unknown ≠ low の明示)


# ====================== 関連度 Relevance (R0-R3) ======================


def _victim_slices(
    vcs: tuple[tuple[str, str, int], ...],
) -> dict[str, int]:
    """被害観測 (country, sector, count) からミッション関連スライスを集計する。"""
    jp_total = jp_defgov = allied_total = allied_defgov = defgov_any = 0
    for country, sector, n in vcs:
        is_defgov = sector in DEFGOV_SECTORS
        if is_defgov:
            defgov_any += n
        if country == "JP":
            jp_total += n
            if is_defgov:
                jp_defgov += n
        elif country.lower() in ALLIED_NATIONS:
            allied_total += n
            if is_defgov:
                allied_defgov += n
    return {
        "jp_total": jp_total,
        "jp_defgov": jp_defgov,
        "allied_total": allied_total,
        "allied_defgov": allied_defgov,
        "defgov_any": defgov_any,
    }


def _relevance(
    alias: ActorAlias, activity: ActorActivity | None
) -> tuple[int, tuple[str, ...], bool]:
    """関連度バンド (0-3) と駆動要因、strategic_pressure (R2×High 昇格条件) を返す。

    上のバンドから評価し、該当したバンドの全シグナルを駆動要因として返す。
    """
    names = [alias.canonical, *alias.aliases]
    vcs = activity.victim_country_sectors if activity else ()
    v = _victim_slices(vcs)
    # 敵性国家ベースラインは **国家系アクターのみ** (2026-07-18): Qilin (ransom, ru) 等の
    # nation 帰属犯罪系には適用しない — 犯罪系の関連度は観測 (JP/同盟被害) だけで立つ
    state = is_state_actor(alias.nation, alias.family)
    intents: dict[str, int] = dict(activity.top_intents) if activity else {}
    prep_grounds = prepositioning_grounds(names)
    prep_observed = intents.get("prepositioning", 0)
    disruption_observed = intents.get("disruption", 0)
    strategic_pressure = bool(prep_grounds) or (
        state and (prep_observed >= INTENT_MIN_COUNT or disruption_observed >= INTENT_MIN_COUNT)
    )

    # R3 直接: 公的な日本標的 doctrine / 日本 def-gov 被害の観測 / 敵性国家×事前配置
    r3: list[str] = [f"日本標的 doctrine: {g}" for g in jp_targeting_grounds(names)]
    if v["jp_defgov"] >= JP_DEFGOV_MIN:
        r3.append(f"観測: 日本の防衛/政府分野の被害 {v['jp_defgov']} 件 (90日)")
    if state and prep_grounds:
        r3.extend(
            f"事前配置 doctrine: {g} — 同盟CIへの事前配置はミッション直接関連と評価"
            f" (対日射影、暗域=不明≠安全)"
            for g in prep_grounds
        )
    if state and prep_observed >= INTENT_MIN_COUNT:
        r3.append(f"観測: prepositioning intent {prep_observed} 件 × 敵性国家")
    if r3:
        return 3, tuple(r3), strategic_pressure

    # R2 構造的: 日本の持続的被害 / 同盟 def-gov / 敵性国家×(標的分野 doctrine
    # or def-gov 被害 or disruption intent)
    r2: list[str] = []
    if v["jp_total"] >= JP_SUSTAINED_MIN:
        r2.append(f"観測: 日本の被害 {v['jp_total']} 件 (90日、持続的標的)")
    if v["allied_defgov"] >= ALLIED_DEFGOV_MIN:
        r2.append(f"観測: 同盟国の防衛/政府分野の被害 {v['allied_defgov']} 件 (90日)")
    if state:
        doctrine_sectors = sorted(
            {s for name in names for s in STATE_ACTOR_SECTORS.get(str(name).strip().lower(), ())}
        )
        if doctrine_sectors:
            r2.append(f"標的分野 doctrine (公的勧告): {', '.join(doctrine_sectors)} × 敵性国家")
        if v["defgov_any"] >= INTENT_MIN_COUNT:
            r2.append(f"観測: 防衛/政府分野の被害 {v['defgov_any']} 件 × 敵性国家")
        if disruption_observed >= INTENT_MIN_COUNT:
            r2.append(f"観測: disruption intent {disruption_observed} 件 × 敵性国家")
    if r2:
        return 2, tuple(r2), strategic_pressure

    # R1 周辺: 敵性国家ベースライン / 日本の散発被害 / 同盟国被害 / PIR 掲載
    r1: list[str] = []
    if state:
        r1.append(f"敵性国家 ({alias.nation})")
    if 0 < v["jp_total"] < JP_SUSTAINED_MIN:
        r1.append(f"観測: 日本の被害 {v['jp_total']} 件 (90日、散発)")
    if v["allied_total"] >= ALLIED_GENERAL_MIN:
        r1.append(f"観測: 同盟国の被害 {v['allied_total']} 件 (90日)")
    if activity and activity.matched_pir_ids:
        r1.append(f"PIR 掲載: {', '.join(activity.matched_pir_ids)}")
    if r1:
        return 1, tuple(r1), strategic_pressure

    return 0, ("ミッション関連シグナルなし",), strategic_pressure


# ====================== 能力 Capability ======================


def _ttp_id(raw: str) -> str | None:
    """ "T1558 Steal or Forge..." / "T1059.001" → 先頭の T-ID token (不正形式は None)。"""
    token = raw.strip().split(" ")[0].upper() if raw and raw.strip() else ""
    if not token.startswith("T") or len(token) < 5:
        return None
    body = token[1:]
    if not body.replace(".", "").isdigit():
        return None
    return token


def _capability(
    alias: ActorAlias, activity: ActorActivity | None, kev_cves: frozenset[str]
) -> tuple[CapabilityBand, tuple[str, ...], str | None]:
    """能力バンドと駆動要因、coverage_note を返す。

    技術シグナル 4 本 (TTP 幅 / 装備 / KEV 実悪用 / ICS)。辞書 (knowledge) ∪ 観測。
    MITRE 記載量は研究量に比例する (文書化バイアス) ため、シグナル不足は
    low/unknown に落とすだけで **ティアを下げる方向には使わない** (規則表側で保証)。
    """
    known_ttps = {tid for t in alias.mitre_ttps if (tid := _ttp_id(t))}
    observed_ttps = {tid for t in activity.top_ttps if (tid := _ttp_id(t))} if activity else set()
    ttp_union = known_ttps | observed_ttps
    malware_union = {m.lower() for m in alias.associated_malware} | (
        {m.lower() for m, _ in activity.top_malware_families} if activity else set()
    )
    kev_hits = sorted(c for c in activity.top_cves if c.upper() in kev_cves) if activity else []
    ics_hits = sorted(t for t in ttp_union if is_ics_technique(t))

    factors: list[str] = []
    signals = 0
    if len(ttp_union) >= CAP_TTP_BREADTH_MIN:
        signals += 1
        factors.append(f"TTP 幅 {len(ttp_union)} 種 (辞書∪観測)")
    if len(malware_union) >= CAP_MALWARE_MIN:
        signals += 1
        factors.append(f"関連マルウェア/ツール {len(malware_union)} 種")
    if kev_hits:
        signals += 1
        factors.append(f"実悪用脆弱性 (KEV) の共起: {', '.join(kev_hits[:3])}")
    if ics_hits:
        signals += 1
        factors.append(f"ICS/OT テクニック: {', '.join(ics_hits[:3])} — 物理影響能力 (単独で High)")

    if ics_hits or signals >= CAP_SIGNALS_HIGH:
        return "high", tuple(factors), None
    if signals >= 1:
        return "medium", tuple(factors), None
    has_data = bool(ttp_union or malware_union)
    if has_data:
        return (
            "low",
            (f"技術シグナル該当なし (TTP {len(ttp_union)} 種 / 装備 {len(malware_union)} 種)",),
            None,
        )
    return (
        "unknown",
        ("能力データなし",),
        "辞書に MITRE TTP/マルウェア情報が無く観測も無い — 能力は未知であり「低」ではない",
    )


# ====================== 活動状態 (ティア非影響) ======================


def _activity_state(activity: ActorActivity | None) -> tuple[ActivityState, tuple[str, ...]]:
    """90 日窓の観測 (報道) テンポ。件数は報道量であり活動実態そのものではない。"""
    if activity is None or activity.total_articles == 0:
        return "dormant", (f"期間内観測 0 件 ({ASSESSMENT_WINDOW_DAYS}日)",)
    factors: list[str] = []
    if activity.is_quiet_waking:
        factors.append("再活性 (30日以上の静穏後に出現)")
    if activity.is_spike:
        ratio = "初観測" if activity.is_new else f"baseline 比 {activity.spike_ratio:.1f}×"
        factors.append(f"報道急増 ({ratio}、{activity.total_articles} 件/90日)")
        return "spiking", tuple(factors)
    factors.append(f"観測 {activity.total_articles} 件 ({ASSESSMENT_WINDOW_DAYS}日)")
    if activity.total_articles >= ACTIVE_MIN_ARTICLES:
        return "active", tuple(factors)
    return "quiet", tuple(factors)


# ====================== ティア導出 (関連度 × 能力の規則表) ======================


# 関連度バンド (0-3) の人向けラベル (R 番号は内部コードのため UI には出さない)。
_RELEVANCE_LABELS: dict[int, str] = {
    3: "直接の関連あり",
    2: "構造的な関連あり",
    1: "周辺の関連あり",
    0: "関連なし",
}
# 能力バンドの人向けラベル (CapabilityBand の生 enum を UI に出さない)。
_CAPABILITY_LABELS_JA: dict[CapabilityBand, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "unknown": "不明",
}


def _derive_tier(
    relevance_band: int, cap: CapabilityBand, *, strategic_pressure: bool
) -> tuple[Tier, str]:
    """規則表 (設計 §4.4)。活動性は入らない — テンポが戦略脅威を埋めることを構造的に防ぐ。"""
    cap_solid = cap in ("high", "medium")
    rel_label = _RELEVANCE_LABELS[relevance_band]
    cap_label = _CAPABILITY_LABELS_JA[cap]
    if relevance_band == 3:
        if cap_solid:
            return "critical", f"{rel_label} × 能力 {cap_label} → Critical"
        return "high", f"{rel_label} × 能力 {cap_label} → High (能力データ不足の下限)"
    if relevance_band == 2:
        if cap == "high" and strategic_pressure:
            return (
                "critical",
                f"{rel_label} × 能力 {cap_label} × 事前配置/破壊の兆候 → Critical",
            )
        if cap == "high":
            return "high", f"{rel_label} × 能力 {cap_label} (諜報系) → High"
        if cap == "medium":
            return "high", f"{rel_label} × 能力 {cap_label} → High"
        return "moderate", f"{rel_label} × 能力 {cap_label} → Moderate"
    if relevance_band == 1:
        if cap_solid:
            return "moderate", f"{rel_label} × 能力 {cap_label} → Moderate"
        return "watch", f"{rel_label} × 能力 {cap_label} → Watch"
    return "watch", f"{rel_label} → Watch (監視継続)"


# ====================== Public API ======================


def assess_actor(
    alias: ActorAlias,
    activity: ActorActivity | None,
    kev_cves: frozenset[str],
) -> ActorThreatAssessment | None:
    """1 actor のミッション脅威評価。kind != group (機関/請負) は評価対象外 (None)。

    ``activity`` は **90 日固定窓** の観測集計 (休眠アクターは None 可)。
    """
    if alias.kind != "group":
        return None
    relevance_band, relevance_factors, strategic_pressure = _relevance(alias, activity)
    cap_band, cap_factors, coverage_note = _capability(alias, activity, kev_cves)
    state, activity_factors = _activity_state(activity)

    total = activity.total_articles if activity else 0
    if relevance_band == 0 and cap_band == "unknown" and total == 0:
        # 「評価済みの低位 (Watch)」と「証拠が無くて評価できない」を混同しない
        return ActorThreatAssessment(
            tier=None,
            tier_rule="未評価 — 関連度・能力・観測のいずれの証拠も無い (低脅威の意味ではない)",
            relevance_band=0,
            capability_band="unknown",
            activity_state=state,
            relevance_factors=relevance_factors,
            capability_factors=cap_factors,
            activity_factors=activity_factors,
            coverage_note=coverage_note,
        )

    tier, tier_rule = _derive_tier(relevance_band, cap_band, strategic_pressure=strategic_pressure)
    if tier in ("critical", "high") and state == "dormant":
        activity_factors = (
            *activity_factors,
            "休眠中だが戦略的関連度・高 — 暗域=不明≠安全",
        )
    return ActorThreatAssessment(
        tier=tier,
        tier_rule=tier_rule,
        relevance_band=relevance_band,
        capability_band=cap_band,
        activity_state=state,
        relevance_factors=relevance_factors,
        capability_factors=cap_factors,
        activity_factors=activity_factors,
        coverage_note=coverage_note,
    )


# 90 日評価 snapshot は重い (article 走査 ×2) ため短期 TTL cache。収集は毎時 interval
# なので 10 分は十分新しい。now 指定時 (tests) は cache を使わない。
_ASSESS_CACHE_TTL_SECONDS = 600
_assess_cache: tuple[float, dict[str, ActorThreatAssessment]] | None = None


def invalidate_assessment_cache() -> None:
    """アクター辞書の編集直後などに TTL を待たず評価 cache を破棄する。"""
    global _assess_cache  # noqa: PLW0603
    _assess_cache = None


def fetch_mission_threat_assessments(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    use_cache: bool = True,
) -> dict[str, ActorThreatAssessment]:
    """全 group actor の評価 map (actor_id → assessment)。90 日固定窓で計算する。"""
    global _assess_cache  # noqa: PLW0603
    if use_cache and _assess_cache is not None:
        ts, cached = _assess_cache
        if time.monotonic() - ts < _ASSESS_CACHE_TTL_SECONDS:
            return cached

    from src.cti.actor_normalizer import load_actor_aliases
    from src.ui.services.threat_operations import fetch_threat_operations_snapshot

    try:
        from src.tools.kev_client import get_kev_cve_set

        kev = get_kev_cve_set()
    except Exception as e:  # noqa: BLE001 — KEV cache 障害で評価全体を殺さない
        _log.warning("actor_threat_kev_unavailable", error=str(e))
        kev = frozenset()

    snap = fetch_threat_operations_snapshot(
        lookback_days=ASSESSMENT_WINDOW_DAYS,
        include_dormant=True,
        db_path=db_path,
    )
    activity_by_id = {a.actor_id: a for a in snap.actors}
    registry = load_actor_aliases()
    out: dict[str, ActorThreatAssessment] = {}
    for alias in registry.actors:
        assessment = assess_actor(alias, activity_by_id.get(alias.id), kev)
        if assessment is not None:
            out[alias.id] = assessment
    if use_cache:
        _assess_cache = (time.monotonic(), out)
    return out
