"""日本重要インフラ 脅威 posture board の集約サービス (対象中心 I&W、近接度ラダー)。

観測源中心の地図に対し **対象中心** (subject=日本の重要インフラ) で NISC 分野ごとに
「脅威がどの段階まで来ているか」を合成する。設計 = docs/jp_critical_infra_board_design.md。

近接度ラダー (src/cti/ci_posture.py):
  静穏 → 予兆 (同盟国同分野/分野関心/分野技術の脆弱性)
       → 標的化 (アクターが日本の当分野を標的 / KEV×分野技術)
       → 侵害(IT系) (日本の当分野で敵対事象)
       → 制御系接近 (OT/境界 or prepositioning)

board の信頼性の土台 (ノイズゲート):
- 敵対性ゲート: 誤送信/紛失等の事務事故は脅威計上せず nonthreat として正直に別掲。
- canonical ゲート: 非 CI 事業者 (小売/メディア等) はデータ種別言及があっても対象外。
- 報道クラスタ集約: 同一事案の多重報道 (KDDI 型) を victim_org 連結で 1 事象に。
- 潜在露出は 製品→分野 (sectors_for_tech) リンクのみ (text fallback は誤配置源のため廃止)。

変化検知 (I&W の核心): 同長の前窓で観測/潜在を再計算し段階の昇降を changes に報告する
(アクター標的傾向は snapshot 由来の遅い信号のため窓間比較の対象外 = 両窓で共通)。
DB access は geo_cyber_map と同じ bare-connection + SQLite dialect (translate_sql が PG 変換)。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.attack_catalog import is_ics_technique
from src.cti.ci_operator_roster import classify_org, designated_counts_by_sector
from src.cti.ci_posture import (
    ACTIVE_STAGES,
    STAGE_LABELS,
    STAGE_RANKS,
    compose_headline,
    is_brand_impersonation,
    is_nonthreat_mishap,
    is_survey_or_statistics,
    merge_observed_events,
    observed_stage,
    sector_stage,
)
from src.cti.diamond_model import INTENT_LABELS_JA_SHORT
from src.cti.nisc_sectors import (
    NISC_SECTORS,
    kw_hit,
    nisc_sector_for,
    sector_label,
    sectors_for_tech,
)
from src.cti.system_domain import SYSTEM_DOMAIN_LABELS, derive_system_domain
from src.cti.threat_actor_doctrine import (
    actor_target_niscs,
    dominant_strategic_intent,
    is_state_actor,
)
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# 観測 (敵対事象) の category 群。phishing はブランド詐称 (顧客標的) であって
# 事業者システムへの侵入ではないため観測に含めない。
_OBSERVED_CATEGORIES: tuple[str, ...] = ("breach", "incident", "apt", "apt_leak", "malware")
_POTENTIAL_CATEGORIES: tuple[str, ...] = ("vulnerability", "advisory")
# read-across の対象同盟国 (JP で観測される前に同分野を警戒)。
_ALLY_ISOS: frozenset[str] = frozenset({"US", "TW", "KR", "GB", "AU"})
# 1 分野 1 段階あたりの表示上限 (bound)。counts は全数を保持。
_PER_STAGE_CAP = 6
_FETCH_CAP = 800


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _win_clause(since: datetime | None, until: datetime | None) -> tuple[str, list[Any]]:
    frag = ""
    params: list[Any] = []
    if since is not None:
        frag += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    if until is not None:
        frag += " AND COALESCE(datetime(published_at), datetime(created_at)) < datetime(?)"
        params.append(until.isoformat())
    return frag, params


def _fetch_rows(
    con: Any,
    categories: tuple[str, ...],
    where_extra: str,
    since: datetime | None,
    until: datetime | None,
) -> list[Any]:
    cat_ph = ",".join(["?"] * len(categories))
    win_frag, win_params = _win_clause(since, until)
    sql = (
        "SELECT article_id, title, url, importance, category, "
        "victim_sector_canonical AS sector, victim_country_iso AS iso, "
        "socio_political_intent AS intent, intent_confidence, event_date, "
        "feed_title, summary, status "
        "FROM articles "
        f"WHERE category IN ({cat_ph}) {where_extra}{win_frag} "
        "ORDER BY COALESCE(datetime(published_at), datetime(created_at)) DESC "
        f"LIMIT {_FETCH_CAP}"
    )
    return list(con.execute(sql, (*categories, *win_params)).fetchall())


def _entities_for(
    con: Any, article_ids: list[str], types: tuple[str, ...]
) -> dict[str, dict[str, list[str]]]:
    """article_id → {entity_type: [values]} (batch, N+1 回避)。"""
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    if not article_ids:
        return out
    id_ph = ",".join(["?"] * len(article_ids))
    type_ph = ",".join(["?"] * len(types))
    sql = (
        "SELECT article_id, entity_type, value FROM article_entities "
        f"WHERE article_id IN ({id_ph}) AND entity_type IN ({type_ph})"
    )
    for row in con.execute(sql, (*article_ids, *types)).fetchall():
        out[str(row["article_id"])][str(row["entity_type"])].append(str(row["value"]))
    return out


def _confidence_tier(source_count: int, top_share: float) -> str:
    if source_count <= 2 or top_share >= 0.70:
        return "thin"
    if source_count >= 5:
        return "rich"
    return "medium"


def _importance_confidence(importance: str | None) -> str:
    return {"high": "high", "medium": "medium"}.get(importance or "", "low")


# ---------- 記事 → 分野バケット (観測/潜在。現窓・前窓の両方で使う) ----------


def _empty_stage_buckets() -> dict[str, list[dict[str, Any]]]:
    return {stage: [] for stage in ACTIVE_STAGES}


_ORG_SUFFIX_NOISE = re.compile(r"株式会社|\(株\)|（株）|ホールディングス|グループ|\s+")
_PERIPHERAL_LINK_MIN_LEN = 4  # 短 CJK 誤爆防止 (length-aware 教訓)


def _peripheral_link_names(items: list[dict[str, Any]]) -> list[str]:
    """周辺観測 org の中核名 (title 突合用)。"""
    names: set[str] = set()
    for item in items:
        for org in item.get("orgs") or []:
            core = _ORG_SUFFIX_NOISE.sub("", str(org)).lower()
            if len(core) >= _PERIPHERAL_LINK_MIN_LEN:
                names.add(core)
    return sorted(names)


def _finalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """merge 済み事象に 表示 operator / tier / systemic / 精度 を付けて payload 形に整える。"""
    operators = [str(o) for o in event.get("operators") or []]
    match = next((m for o in operators if (m := classify_org(o)) is not None), None)
    if match is not None and match.tier == "designated":
        operator: str | None = match.canonical
    else:
        operator = operators[0] if operators else None
    domain = str(event["system_domain"])
    precision = (
        "system_identified"
        if domain != "unknown" and operator
        else ("operator_identified" if operator else "sector_only")
    )
    event.pop("orgs", None)
    return {
        **event,
        "operator": operator,
        "operator_tier": match.tier if match is not None else None,
        "operator_id": match.operator_id if match is not None else None,
        "systemic": bool(match is not None and match.systemic),
        "precision": precision,
    }


def _collect_article_buckets(
    observed_rows: list[Any],
    potential_rows: list[Any],
    entmap: dict[str, dict[str, list[str]]],
    kev_set: frozenset[str],
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, int],
    dict[str, list[str]],
    dict[str, list[dict[str, Any]]],
]:
    """記事行 → 分野ごとの段階バケット + nonthreat 件数 + coverage ソース + 周辺観測。"""
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {
        nisc: _empty_stage_buckets() for nisc in NISC_SECTORS
    }
    nonthreat: dict[str, int] = dict.fromkeys(NISC_SECTORS, 0)
    sources: dict[str, list[str]] = defaultdict(list)
    peripheral: dict[str, list[dict[str, Any]]] = {nisc: [] for nisc in NISC_SECTORS}

    # --- 観測 (敵対事象): 調査記事除外 → 3層ゲート (名簿/型/周辺) → 敵対性ゲート
    #     → 報道クラスタ集約 → 段階
    raw_observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_peripheral: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in observed_rows:
        aid = str(r["article_id"])
        title = str(r["title"] or "")
        if is_survey_or_statistics(title) or is_brand_impersonation(title):
            continue  # 統計・調査・回顧記事 / ブランド詐称は事業者への事象ではない
        ents = entmap.get(aid, {})
        orgs = [o for o in ents.get("victim_org", []) if o]
        text = f"{title} {r['summary'] or ''}"
        # 3層ゲート: org 特定済み記事は名簿/型で検証。全 org 非該当なら周辺観測へ
        # (被害主体が特定できているのに CI 事業者でない = 非 CI 確定)。org 無しは検証不能
        # なので canonical ゲート (nisc_sector_for) を信頼して段階に残す (半田病院型の recall)。
        match = None
        matched_org: str | None = None
        for o in orgs:
            if (m := classify_org(o)) is not None:
                match, matched_org = m, o
                break
        is_peripheral = bool(orgs) and match is None
        nisc: str | None
        if match is not None and matched_org is not None:
            nisc = match.sector
            # 判定に使った org (+ 名簿 canonical) を先頭へ — merge の operators cap (3件) や
            # DB 返却順 (値ソート) で判定根拠が落ちて tier 表示が欠落するのを防ぐ。
            # canonical は merge 鍵にもなる (別名報道の連結)。
            ordered = [matched_org, *[o for o in orgs if o != matched_org]]
            orgs = [match.canonical, *ordered] if match.canonical else ordered
        else:
            nisc = nisc_sector_for(r["sector"], title, str(r["summary"] or ""))
        if nisc is None or nisc not in buckets:
            continue
        if is_nonthreat_mishap(title):
            if not is_peripheral:
                if r["feed_title"]:
                    sources[nisc].append(str(r["feed_title"]))
                nonthreat[nisc] += 1
            continue  # 非 CI の事務事故は板の対象外 (CI 側のみ nonthreat 計上)
        item = {
            "id": f"obs-{aid}",
            "kind": "observed",
            "status": "actual",
            "title": title[:160],
            "orgs": orgs,
            "system_domain": derive_system_domain(
                text=text,
                ttps=ents.get("ttp", []),
                affected_products=ents.get("affected_product", []),
            ),
            "importance": r["importance"],
            "event_date": r["event_date"],
            "url": r["url"],
            "confidence": _importance_confidence(r["importance"]),
            "intent": r["intent"],
            "intent_confidence": r["intent_confidence"],
        }
        if is_peripheral:
            raw_peripheral[nisc].append(item)
            continue
        if r["feed_title"]:
            sources[nisc].append(str(r["feed_title"]))
        raw_observed[nisc].append(item)

    # org 抽出の揺らぎ対策: 同一非 CI 事案の org 抽出漏れ記事 (org 無し) は、周辺 org の
    # 中核名 (会社形態/HD 接尾辞除去、4 文字以上) が title に現れる場合に道連れで周辺へ。
    for nisc in list(raw_observed):
        link_names = _peripheral_link_names(raw_peripheral.get(nisc, []))
        if not link_names:
            continue
        keep: list[dict[str, Any]] = []
        for item in raw_observed[nisc]:
            title_lower = str(item["title"]).lower()
            if not item.get("orgs") and any(kw_hit(n, title_lower) for n in link_names):
                raw_peripheral[nisc].append(item)
            else:
                keep.append(item)
        raw_observed[nisc] = keep

    for nisc, items in raw_observed.items():
        for event in merge_observed_events(items):
            buckets[nisc][
                observed_stage(
                    str(event["system_domain"]),
                    event.get("intent"),
                    event.get("intent_confidence"),
                )
            ].append(_finalize_event(event))
    for nisc, items in raw_peripheral.items():
        peripheral[nisc] = [_finalize_event(e) for e in merge_observed_events(items)]

    # --- 潜在露出: 製品→分野リンクのみ (露出クラス、事業者名は付けない)
    for r in potential_rows:
        aid = str(r["article_id"])
        ents = entmap.get(aid, {})
        cves = [c.upper() for c in ents.get("cve", [])]
        products = ents.get("affected_product", [])
        on_kev = any(c in kev_set for c in cves)
        if not on_kev and r["importance"] != "high":
            continue
        target_niscs: set[str] = set()
        for p in products:
            target_niscs.update(sectors_for_tech(p))
        if not target_niscs:
            continue  # 製品リンクの無い汎用告知は分野の露出ではない
        domain = derive_system_domain(
            text=f"{r['title'] or ''} {r['summary'] or ''}",
            ttps=ents.get("ttp", []),
            affected_products=products,
        )
        stage = "targeting" if on_kev else "indication"
        for nisc in target_niscs:
            if nisc not in buckets:
                continue
            buckets[nisc][stage].append(
                {
                    "id": f"pot-{aid}-{nisc}",
                    "kind": "potential",
                    "status": "potential",
                    "title": str(r["title"] or "")[:160],
                    "operator": None,
                    "system_domain": domain,
                    "precision": "system_identified" if products else "sector_only",
                    "cves": cves[:4],
                    "kev": on_kev,
                    "url": r["url"],
                    "confidence": "high" if on_kev else "medium",
                }
            )
    return buckets, nonthreat, sources, peripheral


# ---------- アクター標的傾向 / 同盟国リードアクロス (snapshot 由来の遅い信号) ----------


def _fetch_snapshot(window_days: int | None, db_path: Path) -> Any:
    """threat_operations snapshot を一度だけ取得 (actor レーン + 行動レーンで共有)。"""
    from src.ui.services.threat_operations import fetch_threat_operations_snapshot

    try:
        # 観測/潜在と窓を揃える。全期間 (None) は actor 集計コストを 365 日で bound
        # (アクターの活動性は 1 年超で急速に陳腐化するため実質全期間と扱える)。
        return fetch_threat_operations_snapshot(
            lookback_days=window_days if window_days is not None else 365,
            db_path=db_path,
        )
    except Exception as e:  # noqa: BLE001 — actor 集計失敗で board 全体を止めない
        _log.warning("jp_ci_board_actor_snapshot_failed", error=str(e))
        return None


def _collect_actor_items(snap: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """分野 → {targeting: [...], indication: [...]} のアクター由来 item (JP 近接度レーン)。"""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {
        nisc: {"targeting": [], "indication": []} for nisc in NISC_SECTORS
    }
    if snap is None:
        return out
    for actor in snap.actors:
        actor_niscs = {
            n for sec, _ in actor.top_sectors if (n := nisc_sector_for(sec, "")) is not None
        }
        if not actor_niscs:
            continue
        victim_isos = {iso.upper() for iso, _ in actor.top_countries}
        jp_count = actor.japan_targeted_count
        hits_jp = jp_count > 0 or "JP" in victim_isos
        allies = victim_isos & _ALLY_ISOS
        domain = "ot" if any(is_ics_technique(t) for t in actor.top_ttps) else "unknown"
        for nisc in actor_niscs:
            if nisc not in out:
                continue
            if hits_jp:
                jp_note = f"日本 {jp_count}件" if jp_count > 0 else "日本を標的圏に含む"
                out[nisc]["targeting"].append(
                    {
                        "id": f"act-{actor.actor_id}-{nisc}",
                        "kind": "actor",
                        "status": "potential",
                        "title": f"{actor.canonical} が当分野を標的 ({jp_note})",
                        "actor": actor.canonical,
                        "nation": actor.nation,
                        "system_domain": domain,
                        "precision": "sector_only",
                        "confidence": "high" if jp_count >= 2 else "medium",
                    }
                )
            elif allies:
                ally_labels = "/".join(sorted(allies))
                out[nisc]["indication"].append(
                    {
                        "id": f"rax-{actor.actor_id}-{nisc}",
                        "kind": "readacross",
                        "status": "potential",
                        "title": (
                            f"{actor.canonical} が同盟国({ally_labels})で当分野を標的 → 日本も警戒"
                        ),
                        "actor": actor.canonical,
                        "nation": actor.nation,
                        "allies": ally_labels,
                        "system_domain": domain,
                        "precision": "sector_only",
                        "confidence": "medium",
                    }
                )
    return out


# ---------- 行動中心レーン: 世界の国家アクター行動 (victim 非依存の先行指標) ----------

# intent の表示優先順 (I&W: 事前潜伏を最上位)。
_INTENT_ORDER: dict[str, int] = {
    "prepositioning": 0,
    "disruption": 1,
    "espionage": 2,
    "subversion": 3,
    "coercion": 4,
}
# ラベルは SSoT (diamond_model.INTENT_LABELS_JA_SHORT) から導出 (M3: 方言排除)。
_INTENT_LABELS: dict[str, str] = {k: INTENT_LABELS_JA_SHORT[k] for k in _INTENT_ORDER}
# 横断キャンペーン検出の閾値 (協調的インフラ作戦の兆候)。
_CAMPAIGN_MIN_SECTORS = 3
_CAMPAIGN_MIN_ARTICLES = 3


def _collect_actor_behavior(
    snap: Any,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """世界コーパスの国家アクター行動を分野へ射影 + 横断キャンペーンを検出。

    doctrine (公的勧告由来の標的分野) ∪ 観測 top_sectors で分野を確定し、intent (事前潜伏/
    諜報/破壊) を戦略 intent に絞る。victim が JP でなくても分野の**先行指標**として炙り出す。
    """
    per_sector: dict[str, list[dict[str, Any]]] = {nisc: [] for nisc in NISC_SECTORS}
    campaigns: list[dict[str, Any]] = []
    if snap is None:
        return per_sector, campaigns

    for actor in snap.actors:
        if not is_state_actor(actor.nation, actor.family):
            # 敵性国家の国家系アクターのみ。旧実装は nation しか見ず、コメントの
            # 「犯罪 ransomware 等は除外」は intent 頼みだった — nation=ru の犯罪系
            # (Qilin 等) が disruption intent で行動レーンに漏れ得たため family でも除外
            # (2026-07-18、NON_STATE_FAMILIES)
            continue
        intent = dominant_strategic_intent(actor.top_intents)
        if intent is None:
            continue  # 戦略 intent 不明 (金融犯罪等) は行動レーン対象外
        observed = {n for s, _ in actor.top_sectors if (n := nisc_sector_for(s, "")) is not None}
        niscs = actor_target_niscs([actor.canonical, *actor.aliases], observed)
        if not niscs:
            continue
        ics = any(is_ics_technique(t) for t in actor.top_ttps)
        base = {
            "actor": actor.canonical,
            "nation": actor.nation,
            "intent": intent,
            "articles": actor.total_articles,
            "jp_targeted": actor.japan_targeted_count > 0,
            "spike": bool(actor.is_spike),
            "is_new": bool(actor.is_new),
            "ics": ics,
        }
        for nisc in sorted(niscs):
            if nisc in per_sector:
                per_sector[nisc].append({**base, "id": f"beh-{actor.actor_id}-{nisc}"})
        # 横断キャンペーン: 事前潜伏/破壊 × 3+ 分野 × 活発 = 協調的インフラ作戦の兆候
        if (
            intent in ("prepositioning", "disruption")
            and len(niscs) >= _CAMPAIGN_MIN_SECTORS
            and actor.total_articles >= _CAMPAIGN_MIN_ARTICLES
        ):
            campaigns.append(
                {
                    "actor": actor.canonical,
                    "nation": actor.nation,
                    "intent": intent,
                    "sector_ids": sorted(niscs),
                    "sectors": [sector_label(n) for n in sorted(niscs)],
                    "articles": actor.total_articles,
                    "jp_targeted": actor.japan_targeted_count > 0,
                    "spike": bool(actor.is_spike),
                }
            )

    def _key(b: dict[str, Any]) -> tuple[int, bool, int]:
        return (_INTENT_ORDER.get(str(b["intent"]), 9), not b["jp_targeted"], -int(b["articles"]))

    for nisc in per_sector:
        per_sector[nisc].sort(key=_key)
        per_sector[nisc] = per_sector[nisc][:_PER_STAGE_CAP]
    campaigns.sort(
        key=lambda c: (
            _INTENT_ORDER.get(str(c["intent"]), 9),
            -len(c["sector_ids"]),
            -c["articles"],
        )
    )
    return per_sector, campaigns


# ---------- board 合成 ----------


def build_jp_ci_board(
    *,
    window_days: int | None = 30,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """全 NISC 分野の posture (近接度ラダー + 前窓比 delta)。疎でも全分野を返す。"""
    from src.tools.kev_client import get_kev_cve_set

    now = datetime.now(UTC)
    since = now - timedelta(days=window_days) if window_days else None
    prev_since = now - timedelta(days=window_days * 2) if window_days else None
    kev_set = get_kev_cve_set()

    jp_filter = "AND (victim_country_iso = 'JP' OR posted_channel = 'japan_watch')"
    con = _connect(db_path)
    try:
        observed_rows = _fetch_rows(con, _OBSERVED_CATEGORIES, jp_filter, since, None)
        potential_rows = _fetch_rows(con, _POTENTIAL_CATEGORIES, "", since, None)
        prev_observed: list[Any] = []
        prev_potential: list[Any] = []
        if window_days:
            prev_observed = _fetch_rows(con, _OBSERVED_CATEGORIES, jp_filter, prev_since, since)
            prev_potential = _fetch_rows(con, _POTENTIAL_CATEGORIES, "", prev_since, since)
        all_ids = [
            str(r["article_id"])
            for r in [*observed_rows, *potential_rows, *prev_observed, *prev_potential]
        ]
        entmap = _entities_for(
            con, all_ids, ("victim_org", "ttp", "affected_product", "cve", "actor")
        )
    finally:
        con.close()

    buckets, nonthreat, sources, peripheral = _collect_article_buckets(
        observed_rows, potential_rows, entmap, kev_set
    )
    snap = _fetch_snapshot(window_days, db_path)  # actor レーン + 行動レーンで共有
    actor_items = _collect_actor_items(snap)
    behavior, campaigns = _collect_actor_behavior(snap)
    prev_buckets: dict[str, dict[str, list[dict[str, Any]]]] | None = None
    if window_days:
        prev_buckets, _, _, _ = _collect_article_buckets(
            prev_observed, prev_potential, entmap, kev_set
        )

    designated_total = designated_counts_by_sector()  # カバレッジ分母 (暗域の定量化)

    sectors: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for nisc in NISC_SECTORS:
        stages = buckets[nisc]
        # アクター由来 item を段階バケットへ (標的化はアクター優先で先頭に)
        stages["targeting"] = [*actor_items[nisc]["targeting"], *stages["targeting"]]
        stages["indication"] = [*actor_items[nisc]["indication"], *stages["indication"]]
        stage = sector_stage(stages)

        prev_stage: str | None = None
        delta: int | None = None
        if prev_buckets is not None:
            merged_prev = prev_buckets[nisc]
            # アクター/リードアクロスは窓間比較の対象外 → 両窓に同じ床を与える
            merged_prev["targeting"] = [
                *actor_items[nisc]["targeting"],
                *merged_prev["targeting"],
            ]
            merged_prev["indication"] = [
                *actor_items[nisc]["indication"],
                *merged_prev["indication"],
            ]
            prev_stage = sector_stage(merged_prev)
            diff = STAGE_RANKS[stage] - STAGE_RANKS[prev_stage]
            delta = 0 if diff == 0 else (1 if diff > 0 else -1)

        observed_events = [*stages["ot_approach"], *stages["intrusion_it"]]
        mix: Counter[str] = Counter()
        for item in [*observed_events, *stages["targeting"], *stages["indication"]]:
            if item.get("kind") in ("observed", "potential"):
                mix[str(item["system_domain"])] += 1

        # カバレッジ: 指定事業者 M 中、観測された distinct 指定事業者 K (残りは無音 = 暗域)
        observed_designated = {
            e["operator_id"]
            for e in observed_events
            if e.get("operator_tier") == "designated" and e.get("operator_id")
        }
        has_systemic = any(e.get("systemic") for e in observed_events)

        feed_titles = sources[nisc]
        src_counter = Counter(feed_titles)
        source_count = len(src_counter)
        top_share = (max(src_counter.values()) / len(feed_titles)) if feed_titles else 0.0
        coverage_tier = _confidence_tier(source_count, top_share)
        headline = compose_headline(stage, stages, coverage_tier)

        if delta is not None and delta != 0 and prev_stage is not None:
            changes.append(
                {
                    "nisc_sector": nisc,
                    "label": sector_label(nisc),
                    "from_stage": prev_stage,
                    "to_stage": stage,
                    "direction": "up" if delta > 0 else "down",
                    "driver": headline
                    if delta > 0
                    else f"{STAGE_LABELS[prev_stage]}から後退 (期間内の新規観測なし)",
                }
            )

        sectors.append(
            {
                "nisc_sector": nisc,
                "label": sector_label(nisc),
                "stage": stage,
                "prev_stage": prev_stage,
                "delta": delta,
                "headline": headline,
                "quiet_not_safe": not observed_events,
                "systemic_hit": has_systemic,
                # 暗域の定量化: 指定 M 事業者中、窓内で観測された distinct 指定事業者 K。
                "designated_total": designated_total.get(nisc, 0),
                "designated_observed": len(observed_designated),
                "coverage": {
                    "tier": coverage_tier,
                    "source_count": source_count,
                    "note": "観測なし — 静か≠安全 (日本の重要インフラは開示が少ない)"
                    if not observed_events
                    else f"{source_count} ソース",
                },
                "system_domain_mix": {
                    "ot": mix.get("ot", 0),
                    "boundary": mix.get("boundary", 0),
                    "it": mix.get("it", 0),
                    "unknown": mix.get("unknown", 0),
                },
                "stages": {s: stages[s][:_PER_STAGE_CAP] for s in ACTIVE_STAGES},
                # 周辺観測: org 特定済みだが名簿/型に合致しない非 CI 事業者の報道。
                # 段階を駆動しない (表示は折りたたみ)。ゲートの見落とし監査用に可視化。
                "peripheral": peripheral[nisc][:_PER_STAGE_CAP],
                # 行動中心レーン: 世界の国家アクター行動 (victim 非依存の先行指標)。
                # JP 観測が静穏でも、世界で当分野クラスを狙う国家アクター行動があればここに出る。
                "threat_behavior": behavior[nisc],
                "world_active": bool(behavior[nisc]),
                "counts": {
                    **{s: len(stages[s]) for s in ACTIVE_STAGES},
                    "nonthreat": nonthreat[nisc],
                    "peripheral": len(peripheral[nisc]),
                    "behavior": len(behavior[nisc]),
                },
            }
        )

    # 変化は昇段を先に、次に段階の高い順
    changes.sort(
        key=lambda c: (c["direction"] != "up", -STAGE_RANKS[str(c["to_stage"])]),
    )
    return {
        "window_days": window_days,
        "generated_at": now.isoformat(),
        "note": (
            "段階 = 脅威が当分野へどこまで来ているかの観測に基づく評価。"
            "観測は収集網の観測であり、日本の重要インフラは被害開示が少ない。"
            "『静か』は『安全』ではない。潜在露出は分野レベルの露出仮説で、"
            "特定事業者の被害を意味しない。"
        ),
        "stage_labels": STAGE_LABELS,
        "system_domain_labels": SYSTEM_DOMAIN_LABELS,
        "intent_labels": _INTENT_LABELS,
        "changes": changes,
        # 横断キャンペーン: 同一国家アクターが 3+ 分野で事前潜伏/破壊 = 協調的インフラ作戦の兆候。
        # 集約が個別記事を超えて示す創発信号 (相関のみ、因果は主張しない)。
        "campaigns": campaigns,
        "sectors": sectors,
    }
