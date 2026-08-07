"""脅威マップ (Geo-Cyber Map) の集計サービス (Phase 3)。

蓄積した victim_country / actor 帰属を **地図にプロット可能な形** に集約する。
設計原則 ([[geo_map_design_discussion]]):
  - **精度を正直に符号化**: 国しか分からない事象は国の集計バブルで表し、鋭いピンに
    しない (precision="country")。施設/組織の精密点 (GeoNames/Wikidata) は後続 Phase。
  - **サイバーは点でなくフロー**: 攻撃者 nation → 被害国 を弧 (flow) で表す。両端とも
    国レベルでデータと噛み合う。
  - **盤上に乗らない事象を隠さない**: 国を解決できなかった件数 (global / 複数国 /
    unknown) を ``unplaced_count`` として返し、地図の網羅率を誠実に示す。

LLM 不要・軽量 (raw SQL 2 本 + geocoder lookup)。read-only (書込み無し)。
overview.py と同じ backend 接続・窓集計パターンを踏襲する。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.category_scopes import CYBER_ATTACK_EVENTS
from src.cti.diamond_model import NATION_LABELS_JA
from src.cti.geocoder import Geocoder
from src.cti.routing_signals import looks_accidental_leak
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# 表示ラベルは yaml (countries / victim_sectors) を SSoT に。ハードコード重複を避ける。
_COUNTRIES_YAML = Path("config/countries.yaml")
_SECTORS_YAML = Path("config/victim_sectors.yaml")

# actor nation コード → 表示ラベル (SSoT = diamond_model.NATION_LABELS_JA、M3 複製解消)。
_NATION_LABELS: dict[str, str] = NATION_LABELS_JA

# #8: 地図は **サイバー攻撃被害のみ** を表示する。victim_country は geopolitical 記事
# (国への「言及」) にも付くため、category で実際の cyber-attack incident に絞る。
# geopolitical / policy / advisory / vulnerability / research / other / grok_x_signal_* は
# 「攻撃による被害」ではないので除外 (地政学イベントは #6 の別レイヤーで扱う予定)。
# さらに category=incident/breach でも「誤送信等の運用ミス」は looks_accidental_leak で除外。
# SSoT: category_scopes (国家情勢の広義レンズとは意図的に別分母 — 件数は直接比較不可)。
_CYBER_ATTACK_CATEGORIES: frozenset[str] = CYBER_ATTACK_EVENTS


def _cyber_category_clause(col: str) -> str:
    """cyber-attack category の IN 句を組み立てる (placeholder は呼出側で params に渡す)。"""
    ph = ",".join(["?"] * len(_CYBER_ATTACK_CATEGORIES))
    return f"{col} IN ({ph})"


def _cyber_category_params() -> list[str]:
    return sorted(_CYBER_ATTACK_CATEGORIES)


def _class_clause(threat_class: str, col: str = "is_ransomware") -> str:
    """ランサム/その他 の絞り込み句。'ransomware'→=1、'other'→=0、'all'→空。"""
    if threat_class == "ransomware":
        return f" AND {col}=1"
    if threat_class == "other":
        return f" AND ({col}=0 OR {col} IS NULL)"
    return ""


# source_status: 報道(posted=triage 済ニュース) / 台帳(collected=ransomware.live 等の生レジストリ・
# 未投稿・地図専用) / すべて。両者の差分が「収集の盲点」を示すため UI で明示区別する。
_SOURCE_STATUSES: frozenset[str] = frozenset({"all", "posted", "collected"})


def _status_filter(source_status: str, col: str = "status") -> tuple[str, list[str]]:
    """source_status に応じた status 句と params を返す。

    'posted' / 'collected' は単一 status、'all' (既定) は posted + collected。
    placeholder を使い任意文字列を SQL に流さない (allowlist は呼出側で正規化済み前提)。
    """
    if source_status == "posted":
        return f"{col} = ?", ["posted"]
    if source_status == "collected":
        return f"{col} = ?", ["collected"]
    return f"{col} IN ('posted', 'collected')", []


# 意義の地図 (A): importance しきい値で「対象」を絞る。importance は PIR 駆動 triage の出力で
# 日本関連 / セクター / PIR を既に内包する holistic な significance なので、別スコアを作らず
# これを直接使う (二重計上回避)。'all'=無し / 'medium_up'=high+medium / 'high'=high のみ。
# 値は固定リテラルなので placeholder 不要 (col 名のみ呼出側指定、外部入力は API で allowlist 化)。
_IMPORTANCE_LEVELS: frozenset[str] = frozenset({"all", "medium_up", "high"})


def _importance_clause(min_importance: str, col: str = "importance") -> str:
    """importance しきい値の AND 句。null importance は significance から除外 (= low 扱い)。"""
    if min_importance == "high":
        return f" AND {col} = 'high'"
    if min_importance == "medium_up":
        return f" AND {col} IN ('high', 'medium')"
    return ""


def _short_source_label(feed_url: str | None, feed_title: str | None) -> str:
    """支配ソースの短い表示名。feed_title 優先、無ければ feed_url の host、最後は (不明)。"""
    if feed_title and feed_title.strip():
        return feed_title.strip()
    if feed_url and feed_url.strip():
        host = feed_url.split("://", 1)[-1].split("/", 1)[0]
        return host or feed_url.strip()
    return "(不明ソース)"


# カバレッジ信頼度の閾値 (本番 PG 実測で確定: 国の 62% がソース ≤2 の monoculture。
# 明るい国も単一依存が多い [DE 13件の77%が単一ソース等] → source 数と単一依存度の両方で判定)。
_CONF_SINGLE_MAX_SOURCES = 2
_CONF_DOMINANCE_THRESHOLD = 0.70
_CONF_RICH_MIN_SOURCES = 5


def _confidence_tier(source_count: int, top_share: float) -> str:
    """カバレッジ信頼度 3 段。'thin'(薄い/単一) / 'medium'(並) / 'rich'(厚い)。

    薄い = ソース ≤2 **または** 単一ソースが 70% 以上 (明るいが単一依存=脆いも薄い扱い)。
    厚い = ソース ≥5 かつ単一依存でない。それ以外は 並。
    """
    if source_count <= _CONF_SINGLE_MAX_SOURCES or top_share >= _CONF_DOMINANCE_THRESHOLD:
        return "thin"
    if source_count >= _CONF_RICH_MIN_SOURCES:
        return "rich"
    return "medium"


@lru_cache(maxsize=4)
def _yaml_display_map(path_str: str) -> dict[str, str]:
    """canonical: {id: {display}} 形式 yaml から {id: display} を読む (cache)。"""
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # pragma: no cover - 設定不正は warn して空
        _log.warning("geo_yaml_parse_failed", path=path_str, error=str(e))
        return {}
    canonical = raw.get("canonical", {})
    if not isinstance(canonical, dict):
        return {}
    out: dict[str, str] = {}
    for cid, body in canonical.items():
        if isinstance(cid, str) and isinstance(body, dict):
            disp = body.get("display")
            out[cid] = str(disp) if isinstance(disp, str) and disp else cid
    return out


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _win_frag(time_basis: str, alias: str = "") -> str:
    """窓フィルタ SQL 片 (先頭 ' AND ' 込み、param は since.isoformat() を1つ追加)。

    time_basis='report' (既定)=報道/取得時刻 (COALESCE published/created)。
    'event'=事象の実発生時刻 (event_date)。dated subset のみ (event_date IS NOT NULL)。
    """
    if time_basis == "event":
        # event_date は TEXT 'YYYY-MM-DD'。datetime() で cast されない (timestamptz でない) ため、
        # since.isoformat() の日付部 substr(?,1,10) と **辞書順比較** (ISO 日付は辞書順=時系列順)。
        return f" AND {alias}event_date >= substr(?, 1, 10) AND {alias}event_date IS NOT NULL"
    pub, cre = f"{alias}published_at", f"{alias}created_at"
    return f" AND COALESCE(datetime({pub}), datetime({cre})) >= datetime(?)"


def _country_sector_rows(
    con: Any,
    since: datetime | None,
    threat_class: str = "all",
    source_status: str = "all",
    min_importance: str = "all",
    time_basis: str = "report",
) -> list[Any]:
    """窓内の **cyber-attack** 記事の (iso, sector, title, summary, feed_url/feed_title, status)。

    title/summary は looks_accidental_leak で運用ミス (誤送信等) を弾くために返す。
    feed_url/feed_title/status はカバレッジ信頼層 (ソース数・単一依存度・台帳/報道内訳) に使う。
    min_importance で「意義の地図」(重要度しきい値) に絞る。
    """
    status_clause, status_params = _status_filter(source_status)
    sql = (
        "SELECT victim_country_iso AS iso, victim_sector_canonical AS sector, "
        "socio_political_intent AS intent, "
        "title, summary, feed_url, feed_title, status FROM articles "
        f"WHERE {status_clause} "
        "AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
        f"AND {_cyber_category_clause('category')}"
        + _class_clause(threat_class)
        + _importance_clause(min_importance)
    )
    params: list[Any] = [*status_params, *_cyber_category_params()]
    if since is not None:
        sql += _win_frag(time_basis)
        params.append(since.isoformat())
    return list(con.execute(sql, tuple(params)).fetchall())


def _unplaced_count(
    con: Any, since: datetime | None, min_importance: str = "all", time_basis: str = "report"
) -> int:
    """国を解決できなかった (iso=null だが raw あり) posted 記事数 = 盤外件数。

    min_importance で「意義の地図」の盤上件数と整合させる (significant な盤外件数)。
    """
    sql = (
        "SELECT COUNT(*) AS n FROM articles WHERE status IN ('posted', 'collected') "
        "AND victim_country_iso IS NULL AND victim_country_raw IS NOT NULL "
        "AND victim_country_raw != ''" + _importance_clause(min_importance)
    )
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += _win_frag(time_basis)
        params = (since.isoformat(),)
    row = con.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def _noncyber_count(con: Any, since: datetime | None, time_basis: str = "report") -> int:
    """国は付いているが cyber-attack でない (geopolitical/policy/言及 等) 件数 = 除外数。"""
    sql = (
        "SELECT COUNT(*) AS n FROM articles WHERE status='posted' "
        "AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
        f"AND NOT (category IS NOT NULL AND {_cyber_category_clause('category')})"
    )
    params: list[Any] = _cyber_category_params()
    if since is not None:
        sql += _win_frag(time_basis)
        params.append(since.isoformat())
    row = con.execute(sql, tuple(params)).fetchone()
    return int(row["n"]) if row else 0


# #6 (レイヤー切替): 地政学イベント = category geopolitical / policy。サイバー攻撃被害とは
# 別レイヤーとして国マーカで表示する (国/地域スケールの事象なので country 代表点で honest)。
_GEOPOLITICAL_CATEGORIES: frozenset[str] = frozenset({"geopolitical", "policy"})


def _geopolitical_category_clause(col: str) -> str:
    """地政学 category の IN 句 (placeholder は呼出側で params に渡す)。"""
    ph = ",".join(["?"] * len(_GEOPOLITICAL_CATEGORIES))
    return f"{col} IN ({ph})"


def _geopolitical_category_params() -> list[str]:
    return sorted(_GEOPOLITICAL_CATEGORIES)


# PMESII-PT 軸 → 列。地政学レイヤーの **直交フィルタ** (色=動機 intent / フィルタ=領域 PMESII)。
# 動機 (intent) が「何を狙うか」、PMESII が「どの領域に作用するか」で交差させて絞り込む。
_PMESII_AXES: dict[str, str] = {
    "p": "pmesii_p",
    "m": "pmesii_m",
    "e": "pmesii_e",
    "s": "pmesii_s",
    "i_infra": "pmesii_i_infra",
    "i_cyber": "pmesii_i_cyber",
    "p_env": "pmesii_p_env",
    "t": "pmesii_t",
}


def _pmesii_clause(pmesii: str) -> str:
    """PMESII 軸フィルタの AND 句 ('all'=無し / 'p'..'t'=その軸=1)。列名は allowlist 由来。"""
    col = _PMESII_AXES.get(pmesii)
    return f" AND {col}=1" if col else ""


def _domain_filter(domain: str, col: str, threat_class: str) -> tuple[str, list[str]]:
    """domain (cyber/geopolitical) に応じた category 句 + class 句と params を返す。

    cyber は cyber category + threat_class (ランサム/その他)、geopolitical は地政学 category
    のみ (threat_class はサイバー専用なので無視)。被害国ドリル / 推移グラフで共用する。
    """
    if domain == "geopolitical":
        return _geopolitical_category_clause(col), _geopolitical_category_params()
    return _cyber_category_clause(col) + _class_clause(threat_class), _cyber_category_params()


def _geo_event_rows(
    con: Any, since: datetime | None, pmesii: str = "all", time_basis: str = "report"
) -> list[Any]:
    """窓内 posted の地政学イベント (geopolitical/policy) の (iso, intent) を取得。

    intent (socio_political_intent) は地政学マーカの **動機色分け** に使う (統一 intent 軸)。
    pmesii で **直交フィルタ** (その PMESII 軸に作用する事象のみ) を掛けられる。
    """
    ph = ",".join(["?"] * len(_GEOPOLITICAL_CATEGORIES))
    sql = (
        "SELECT victim_country_iso AS iso, socio_political_intent AS intent,"
        " intent_confidence FROM articles "
        "WHERE status='posted' "
        "AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
        f"AND category IN ({ph})" + _pmesii_clause(pmesii)
    )
    params: list[Any] = sorted(_GEOPOLITICAL_CATEGORIES)
    if since is not None:
        sql += _win_frag(time_basis)
        params.append(since.isoformat())
    return list(con.execute(sql, tuple(params)).fetchall())


def _flow_rows(
    con: Any,
    since: datetime | None,
    threat_class: str = "all",
    source_status: str = "all",
    min_importance: str = "all",
    time_basis: str = "report",
) -> list[Any]:
    """窓内の cyber-attack 記事の (iso, actor_id, title, summary) を取得 (アクター中心ビュー用)。"""
    status_clause, status_params = _status_filter(source_status, "a.status")
    # D1 (2026-07-27): 同盟国の報告/防御機関 (NSA 等) を攻撃帰属から除外 (legacy 行の mention
    # fallback でも NSA を残さないため subject-gate と併用する)。
    from src.cti.actor_roles import reporter_org_actor_ids
    from src.cti.subject_gate import subject_gate_clause

    reporter_ids = sorted(reporter_org_actor_ids())
    reporter_clause = ""
    if reporter_ids:
        reporter_ph = ",".join("?" for _ in reporter_ids)
        reporter_clause = f" AND ae.value NOT IN ({reporter_ph})"
    # subject-gate (案A): 評価済みは主題のみ、legacy は mention fallback (窓 40%)。
    gate = subject_gate_clause(
        mention_col="ae.value",
        subject_ids_col="a.subject_actor_ids",
        subject_source_col="a.subject_actor_source",
    )
    sql = (
        "SELECT a.victim_country_iso AS iso, ae.value AS actor, a.title AS title, "
        "a.summary AS summary "
        "FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id "
        f"WHERE ae.entity_type='actor' AND {status_clause} "
        "AND a.victim_country_iso IS NOT NULL AND a.victim_country_iso != '' "
        f"AND {_cyber_category_clause('a.category')}"
        + reporter_clause
        + f" AND {gate}"
        + _class_clause(threat_class, "a.is_ransomware")
        + _importance_clause(min_importance, "a.importance")
    )
    params: list[Any] = [*status_params, *_cyber_category_params(), *reporter_ids]
    if since is not None:
        sql += _win_frag(time_basis, "a.")
        params.append(since.isoformat())
    return list(con.execute(sql, tuple(params)).fetchall())


def _time_coverage(con: Any, since: datetime | None) -> dict[str, int]:
    """時間軸トグルのカバレッジ: 報道窓内の cyber+geo 記事の dated 率 (発生日付き / 全)。

    発生時刻モードは dated subset のみ表示するため「N/M 件が発生日付き」を正直に示す。
    """
    sql = (
        "SELECT COUNT(*) AS total, "  # noqa: S608 — カテゴリは内部固定
        "COUNT(*) FILTER (WHERE event_date IS NOT NULL) AS dated FROM articles "
        "WHERE status='posted' AND victim_country_iso IS NOT NULL AND victim_country_iso != '' "
        f"AND ({_cyber_category_clause('category')} OR category IN "
        f"({','.join(['?'] * len(_GEOPOLITICAL_CATEGORIES))}))"
    )
    params: list[Any] = [*_cyber_category_params(), *sorted(_GEOPOLITICAL_CATEGORIES)]
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    row = con.execute(sql, tuple(params)).fetchone()
    return {"dated": int(row["dated"] or 0), "total": int(row["total"] or 0)}


def build_cyber_map(
    *,
    window_days: int | None = 30,
    threat_class: str = "all",
    source_status: str = "all",
    min_importance: str = "all",
    pmesii: str = "all",
    time_basis: str = "report",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """脅威マップの plot データを 1 バッチで返す。

    window_days=None で全期間。threat_class: 'all' / 'ransomware' / 'other' で被害を絞る。
    source_status: 'all' / 'posted'(報道) / 'collected'(台帳) で収集経路を絞る。
    min_importance: 'all' / 'medium_up' / 'high' で「意義の地図」(重要度しきい値) に絞る。
    返す count は「報道/観測件数」(攻撃件数ではない)。各 node はカバレッジ信頼層
    (source_count / top_source_share / confidence / 台帳・報道内訳) を含む。
    """
    geo = Geocoder()
    country_labels = _yaml_display_map(str(_COUNTRIES_YAML))
    sector_labels = _yaml_display_map(str(_SECTORS_YAML))
    registry = load_actor_aliases()
    # actor id → nation (小文字 ISO)。overview と同じ辞書。
    nation_map = {a.id: a.nation for a in registry.actors if a.nation}
    # actor id → 表示名 (canonical)。actor 中心ビュー用。
    actor_label = {a.id: a.canonical for a in registry.actors}

    now = datetime.now(UTC)
    since = now - timedelta(days=window_days) if window_days else None

    con = _connect(db_path)
    try:
        cs_rows = _country_sector_rows(
            con, since, threat_class, source_status, min_importance, time_basis
        )
        unplaced = _unplaced_count(con, since, min_importance, time_basis)
        noncyber = _noncyber_count(con, since, time_basis)
        flow_raw = _flow_rows(con, since, threat_class, source_status, min_importance, time_basis)
        geo_event_raw = _geo_event_rows(con, since, pmesii, time_basis)
        time_coverage = _time_coverage(con, since)
    finally:
        con.close()

    # ── geo_events: 地政学イベント (geopolitical/policy) の国別件数 + **動機(intent)内訳** ──
    # 平板な件数バブルでなく、国ごとの支配動機 (top_intent) で色分けできるよう intent を集約する。
    geo_event_counts: Counter[str] = Counter()
    geo_event_intents: dict[str, Counter[str]] = defaultdict(Counter)
    for r in geo_event_raw:
        iso = str(r["iso"]).upper()
        geo_event_counts[iso] += 1
        it = r["intent"]
        # H3 (intent_confidence 配線): low=仮説は支配動機 (色分け=分析的主張) に投票させない。
        # NULL は旧レジームの確定 tag なので残す (NULL≠low)。
        conf = r["intent_confidence"]
        if it and str(it) != "unknown" and str(conf or "") != "low":
            geo_event_intents[iso][str(it)] += 1
    geo_events: list[dict[str, Any]] = []
    for iso, n in geo_event_counts.most_common():
        pt = geo.country(iso)
        if pt is None:
            continue
        intents = [{"intent": k, "count": v} for k, v in geo_event_intents[iso].most_common()]
        geo_events.append(
            {
                "iso": iso,
                "label": country_labels.get(iso, iso),
                "lat": pt.lat,
                "lon": pt.lon,
                "count": n,
                # 支配動機 (色分け用) + 内訳。intent 未判定が多い国は top_intent=None。
                "top_intent": intents[0]["intent"] if intents else None,
                "intents": intents,
                "intent_count": sum(i["count"] for i in intents),
            }
        )

    def _is_accident(row: Any) -> bool:
        text = f"{row['title'] or ''} {row['summary'] or ''}"
        return looks_accidental_leak(text)

    # ── nodes: 国別 件数 + セクター内訳 (運用ミス = 事故起因の漏洩は除外) ──
    # カバレッジ信頼層: 国ごとに寄与ソース (feed_url) と status を集計し、ソース数・
    # 単一依存度・台帳/報道内訳から「この国の像はどれだけ多源か」を符号化する。
    per_country_total: Counter[str] = Counter()
    per_country_sector: dict[str, Counter[str]] = defaultdict(Counter)
    per_country_intent: dict[str, Counter[str]] = defaultdict(Counter)
    per_country_source: dict[str, Counter[str]] = defaultdict(Counter)
    per_country_source_label: dict[str, dict[str, str]] = defaultdict(dict)
    per_country_status: dict[str, Counter[str]] = defaultdict(Counter)
    excluded_accidental = 0
    for r in cs_rows:
        if _is_accident(r):
            excluded_accidental += 1
            continue
        iso = str(r["iso"]).upper()
        per_country_total[iso] += 1
        sec = r["sector"]
        if sec:
            per_country_sector[iso][str(sec)] += 1
        it = r["intent"]
        if it and str(it) != "unknown":
            per_country_intent[iso][str(it)] += 1
        src_key = (r["feed_url"] or "").strip() or "(none)"
        per_country_source[iso][src_key] += 1
        per_country_source_label[iso][src_key] = _short_source_label(r["feed_url"], r["feed_title"])
        per_country_status[iso][str(r["status"] or "")] += 1

    nodes: list[dict[str, Any]] = []
    placed_events = 0
    for iso, total in per_country_total.most_common():
        pt = geo.country(iso)
        if pt is None:
            # 想定外 (drift-guard で防いでいる) — 盤外に倒して網羅率を汚さない
            unplaced += total
            continue
        placed_events += total
        sectors = [
            {"sector": s, "label": sector_labels.get(s, s), "count": n}
            for s, n in per_country_sector[iso].most_common()
        ]
        node_intents = [{"intent": k, "count": v} for k, v in per_country_intent[iso].most_common()]
        src_counter = per_country_source[iso]
        source_count = len(src_counter)
        top_url, top_n = src_counter.most_common(1)[0] if src_counter else ("", 0)
        top_share = (top_n / total) if total else 0.0
        nodes.append(
            {
                "iso": iso,
                "label": country_labels.get(iso, iso),
                "lat": pt.lat,
                "lon": pt.lon,
                "count": total,
                "precision": pt.precision.value,
                "top_sector": sectors[0]["sector"] if sectors else None,
                "sectors": sectors,
                # 動機内訳 (色分けトグル「動機」で cyber バブルをパイ塗りするため)。
                "top_intent": node_intents[0]["intent"] if node_intents else None,
                "intents": node_intents,
                "intent_count": sum(i["count"] for i in node_intents),
                # カバレッジ信頼層 (corpus 由来): 真の空白域は点灯しないため常設ラベルで補う。
                "source_count": source_count,
                "top_source_share": round(top_share, 3),
                "top_source": per_country_source_label[iso].get(top_url) if top_url else None,
                "posted_count": per_country_status[iso].get("posted", 0),
                "collected_count": per_country_status[iso].get("collected", 0),
                "confidence": _confidence_tier(source_count, top_share),
            }
        )

    # 国→国の攻撃フロー弧は撤去 (2026-06-18): nation→nation は既知の敵対ペアのみで情報量が
    # 低く、帰属の希薄さで日本(最多被害国)への弧が 0 本になる等、地図の主シグナルと
    # 矛盾し誤読を招いたため。アクター中心ビューは弧でなく被害国の強調に降格。
    # 経緯: [[geo_map_design_discussion]]。

    # ── actors: アクター別の被害国 (actor 中心ビュー = 被害国ハイライト用)。
    # actor_id → {victim_iso: count}。
    actor_victims: dict[str, Counter[str]] = defaultdict(Counter)
    for r in flow_raw:
        if _is_accident(r):
            continue
        actor_victims[str(r["actor"])][str(r["iso"]).upper()] += 1
    actors: list[dict[str, Any]] = []
    for actor_id, vc in actor_victims.items():
        nat = (nation_map.get(actor_id) or "").upper()
        nat_pt = geo.country(nat) if nat else None
        victims = [
            {
                "iso": iso,
                "label": country_labels.get(iso, iso),
                "lat": p.lat,
                "lon": p.lon,
                "count": n,
            }
            for iso, n in vc.most_common()
            if (p := geo.country(iso)) is not None
        ]
        if not victims:
            continue
        actors.append(
            {
                "actor_id": actor_id,
                "label": actor_label.get(actor_id, actor_id),
                "nation": nat or None,
                "nation_label": _NATION_LABELS.get(nat.lower(), nat) if nat else None,
                "nation_lat": nat_pt.lat if nat_pt else None,
                "nation_lon": nat_pt.lon if nat_pt else None,
                "total": sum(v["count"] for v in victims),
                "victims": victims,
            }
        )
    actors.sort(key=lambda a: (-int(a["total"]), str(a["label"])))

    return {
        "window_days": window_days,
        "generated_at": now.isoformat(),
        # 時間軸トグル: 報道時刻(report) / 発生時刻(event)。発生時刻は dated subset のみ表示する
        # ため time_coverage で「N/M 件が発生日付き」を正直に示す (暗域=不明≠安全)。
        "time_basis": time_basis,
        "time_coverage": time_coverage,
        "nodes": nodes,
        "actors": actors,
        "geo_events": geo_events,
        "unplaced_count": unplaced,
        # 誠実性: 地図に乗らなかった件数を内訳で示す。non_cyber=言及/地政学等、
        # accidental=誤送信等の運用ミス、unplaced=国を解決できず。
        "excluded": {
            "non_cyber": noncyber,
            "accidental": excluded_accidental,
            "unplaced": unplaced,
        },
        "totals": {
            "placed_events": placed_events,
            "countries": len(nodes),
        },
    }


# 国別ドリルで返す最大記事数 (新しい順)。
_COUNTRY_DRILL_LIMIT = 100


def country_articles(
    iso: str,
    *,
    window_days: int | None = 30,
    threat_class: str = "all",
    domain: str = "cyber",
    source_status: str = "all",
    min_importance: str = "all",
    pmesii: str = "all",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """指定国 (ISO) の被害/事象 記事を新しい順で返す (click-to-drill 用)。

    ``domain='cyber'`` はサイバー攻撃被害 (バブル)、``'geopolitical'`` は地政学イベント
    (動機色ダイヤ)。``source_status`` で報道/台帳を絞る。``min_importance`` は cyber、
    ``pmesii`` は geopolitical の直交フィルタで、それぞれ地図の同一フィルタを適用し
    マーカが表す記事集合と一致させる。記事には動機 (socio_political_intent) も載せる。read-only。
    """
    iso_u = iso.strip().upper()
    country_labels = _yaml_display_map(str(_COUNTRIES_YAML))
    sector_labels = _yaml_display_map(str(_SECTORS_YAML))
    now = datetime.now(UTC)
    since = now - timedelta(days=window_days) if window_days else None

    cat_clause, cat_params = _domain_filter(domain, "category", threat_class)
    status_clause, status_params = _status_filter(source_status)
    # 意義フィルタは cyber のみ、PMESII 直交フィルタは geopolitical のみ (地図マーカと整合)。
    imp_clause = _importance_clause(min_importance) if domain == "cyber" else ""
    pmesii_clause = _pmesii_clause(pmesii) if domain == "geopolitical" else ""
    sql = (
        "SELECT article_id, title, url, importance, category, "
        "victim_sector_canonical AS sector, socio_political_intent AS intent, intent_confidence, "
        "summary, status, created_at "
        f"FROM articles WHERE {status_clause} AND victim_country_iso=? "
        f"AND {cat_clause}{imp_clause}{pmesii_clause}"
    )
    params: list[Any] = [*status_params, iso_u, *cat_params]
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    sql += " ORDER BY datetime(created_at) DESC"

    con = _connect(db_path)
    try:
        rows = list(con.execute(sql, tuple(params)).fetchall())
    finally:
        con.close()

    articles: list[dict[str, Any]] = []
    for r in rows:
        text = f"{r['title'] or ''} {r['summary'] or ''}"
        if looks_accidental_leak(text):
            continue  # 運用ミスはバブルに含めていないのでドリルでも除外
        sec = r["sector"]
        created = r["created_at"]
        articles.append(
            {
                "article_id": r["article_id"],
                "title": r["title"],
                "url": r["url"],
                "importance": r["importance"],
                "category": r["category"],
                "victim_sector": sec,
                "victim_sector_label": sector_labels.get(str(sec), sec) if sec else None,
                "socio_political_intent": r["intent"],
                "intent_confidence": r["intent_confidence"],
                "status": r["status"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            }
        )
        if len(articles) >= _COUNTRY_DRILL_LIMIT:
            break

    return {
        "iso": iso_u,
        "label": country_labels.get(iso_u, iso_u),
        "count": len(articles),
        "articles": articles,
    }


# 件数推移 (地図下) の系列数上限。超過分は「その他」に集約。
_TREND_TOP_N = 6


def trend(
    *,
    window_days: int | None = 30,
    threat_class: str = "all",
    group_by: str = "country",
    domain: str = "cyber",
    source_status: str = "all",
    isos: list[str] | None = None,
    min_importance: str = "all",
    pmesii: str = "all",
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """被害/事象の **日次件数推移** を国別/セクター別の上位系列で返す (地図下の推移グラフ用)。

    ``domain`` = 'cyber' / 'geopolitical' で対象を切替 (地図レイヤーと連動)。``source_status``
    で報道/台帳を絞る (地図 Seg と連動)。``isos`` 指定で victim_country を地域に絞る (P2 地域
    ブラッシング)。``group_by`` = 'country' / 'sector'。日次バケットは backend 非依存に Python
    集計。上位 _TREND_TOP_N + その他。
    """
    gb = "sector" if group_by == "sector" else "country"
    col = "victim_sector_canonical" if gb == "sector" else "victim_country_iso"
    labels = _yaml_display_map(str(_SECTORS_YAML if gb == "sector" else _COUNTRIES_YAML))
    now = datetime.now(UTC)
    since = now - timedelta(days=window_days) if window_days else None

    cat_clause, cat_params = _domain_filter(domain, "category", threat_class)
    status_clause, status_params = _status_filter(source_status)
    # 意義フィルタは cyber のみ、PMESII 直交フィルタは geopolitical のみ (nodes/geo_events と整合)。
    imp_clause = _importance_clause(min_importance) if domain == "cyber" else ""
    pmesii_clause = _pmesii_clause(pmesii) if domain == "geopolitical" else ""
    sql = (
        f"SELECT {col} AS k, COALESCE(published_at, created_at) AS d FROM articles "
        f"WHERE {status_clause} "
        f"AND {col} IS NOT NULL AND {col} != '' "
        f"AND {cat_clause}{imp_clause}{pmesii_clause}"
    )
    params: list[Any] = [*status_params, *cat_params]
    # 地域フィルタ: 対象国 ISO に絞る (group_by に依らず victim_country_iso で判定)。
    if isos:
        ph = ",".join(["?"] * len(isos))
        sql += f" AND victim_country_iso IN ({ph})"
        params.extend(isos)
    if since is not None:
        sql += " AND COALESCE(datetime(published_at), datetime(created_at)) >= datetime(?)"
        params.append(since.isoformat())
    con = _connect(db_path)
    try:
        rows = list(con.execute(sql, tuple(params)).fetchall())
    finally:
        con.close()

    # 日付バケット (YYYY-MM-DD) を Python で算出。窓があれば since..now、無ければ実データ範囲。
    def _day(v: Any) -> str | None:
        if hasattr(v, "strftime"):
            return str(v.strftime("%Y-%m-%d"))
        s = str(v or "")
        return s[:10] if len(s) >= 10 else None

    per_day_key: dict[tuple[str, str], int] = defaultdict(int)
    key_totals: Counter[str] = Counter()
    days_seen: set[str] = set()
    for r in rows:
        day = _day(r["d"])
        k = str(r["k"])
        if day is None:
            continue
        per_day_key[(day, k)] += 1
        key_totals[k] += 1
        days_seen.add(day)
    if not days_seen:
        return {"group_by": gb, "buckets": [], "series": []}

    # バケット軸: 窓指定があれば since..now の連続日、無ければ観測日の範囲。
    start = since.date() if since is not None else datetime.fromisoformat(min(days_seen)).date()
    end = now.date()
    buckets: list[str] = []
    cur = start
    while cur <= end:
        buckets.append(cur.isoformat())
        cur = cur + timedelta(days=1)

    top_keys = [k for k, _ in key_totals.most_common(_TREND_TOP_N)]
    top_set = set(top_keys)
    series: list[dict[str, Any]] = []
    for k in top_keys:
        series.append(
            {
                "key": k,
                "label": labels.get(k, k),
                "counts": [per_day_key.get((b, k), 0) for b in buckets],
            }
        )
    # その他 (top 外) を集約。
    other_counts = [
        sum(per_day_key.get((b, k), 0) for k in key_totals if k not in top_set) for b in buckets
    ]
    if any(other_counts):
        series.append({"key": "_other", "label": "その他", "counts": other_counts})
    return {"group_by": gb, "buckets": buckets, "series": series}
