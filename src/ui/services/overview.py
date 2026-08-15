"""ダッシュボード概観の集計サービス (Phase 2/3)。

「裸のカウント」でなく **変化(前期比) + その変化を駆動した内訳** を返すのが核心。
各 dimension (セクター / 地域 / 敵対国籍 / 重要度) を現在窓 [now-W, now] と前期窓
[now-2W, now-W] で集計し、item 別の current/prior/delta を出す。さらに KEV/高CVSS の
重要脆弱性、notable actor (spike/新規)、source 信頼度構成 (分析的誠実性、抑制でなく表示) を
1 バッチで返す。LLM 不要・軽量。

観測数の trend / 前日比は既存 /api/v1/dashboard/summary を frontend が流用するため、ここでは
扱わない (重複回避)。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.cti.diamond_model import NATION_LABELS_JA
from src.cti.japan_relevance import (
    JAPAN_TARGETED_SQL_FRAGMENT,
    JAPAN_WATCH_CHANNEL,
    is_japan_targeted_row,
)
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# drill-in 先 (Intel Graph) のベース path。triage 行・KPI から深掘りへ。
_INTEL_BASE = "/app/intel"

# ISO 3166-1 alpha-2 → (region_id, 表示ラベル)。被害国を地政学的地域に集約 (戦略レンズ)。
_REGION_MAP: dict[str, tuple[str, str]] = {
    "JP": ("east_asia", "東アジア"),
    "CN": ("east_asia", "東アジア"),
    "KP": ("east_asia", "東アジア"),
    "KR": ("east_asia", "東アジア"),
    "TW": ("east_asia", "東アジア"),
    "UA": ("europe", "欧州"),
    "GB": ("europe", "欧州"),
    "DE": ("europe", "欧州"),
    "FR": ("europe", "欧州"),
    "NL": ("europe", "欧州"),
    "PL": ("europe", "欧州"),
    "IR": ("mideast", "中東"),
    "IL": ("mideast", "中東"),
    "US": ("n_america", "北米"),
    "CA": ("n_america", "北米"),
    "IN": ("s_asia", "南アジア"),
    "PH": ("se_asia", "東南アジア"),
    "VN": ("se_asia", "東南アジア"),
    "AU": ("oceania", "オセアニア"),
}
_REGION_LABELS: dict[str, str] = dict(_REGION_MAP.values())

# actor nation コード → 表示ラベル (SSoT = diamond_model.NATION_LABELS_JA、M3 複製解消)。
_NATION_LABELS: dict[str, str] = NATION_LABELS_JA
# 概観で前面化する敵対国籍 (防衛 CTI ドクトリン中核)。
_ADVERSARY_NATIONS = ("cn", "ru", "kp", "ir")

# セクター未付与の noise (KPI を濁すので標的セクター集計から除外)。
# multi_sector は「複数業種が同時被害」の meta-category であり「どの業種が標的か」の
# 答えにならないため、標的セクター ranking からは除外する (2026-06-16 backfill で顕在化)。
_NON_SECTOR_KEYS = frozenset({"uncategorized", "unknown", "none", "", "multi_sector"})

# notable actor (spike/新規) の lookback。脅威アクター画面 (既定 30日) と揃え、
# drill-in 時に sparkline の窓が一致するようにする (delta 窓 window_days とは独立)。
_NOTABLE_LOOKBACK_DAYS = 30

# まとめ/週刊 記事の title マーカ (発見4)。digest 記事から検出した CVE は title が
# 「今週の気になるニュース」等になり情報ゼロ。検出して title を抑制し NVD canonical へ誘導。
_ROUNDUP_MARKERS: tuple[str, ...] = (
    "気になるセキュリティニュース",
    "セキュリティニュース",
    "ニュースまとめ",
    "今週",
    "週刊",
    "まとめ",
    "週間",
    "roundup",
    "round-up",
    "round up",
    "this week",
    "weekly",
    "in review",
    "wrap-up",
    "wrap up",
    "issue #",
    "issue#",
    "digest",
)

# NVD 詳細ページ (roundup 検出時に title 代替リンクとして提示)。
_NVD_DETAIL = "https://nvd.nist.gov/vuln/detail/{cve}"


def _is_roundup(title: str) -> bool:
    """title がまとめ/週刊系か (CVE 誤帰属の抑制判定)。"""
    t = title.lower()
    return any(m in t for m in _ROUNDUP_MARKERS)


# victim_sector_canonical → 表示ラベル。SSoT = config/cti/victim_sectors.yaml の display
# (M3: ハードコード複製が yaml 更新に追従せず、food_agriculture 等が生 id のまま UI に
# 漏れていた)。読み込みは geo_cyber_map と同じ cache 済みヘルパを共有する。
def _sector_labels() -> dict[str, str]:
    from src.ui.services.geo_cyber_map import _SECTORS_YAML, _yaml_display_map

    return _yaml_display_map(str(_SECTORS_YAML))


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _grouped_counts(con: Any, field: str, since: datetime, until: datetime) -> dict[str, int]:
    """field 別の posted 記事件数 (現在/前期で 2 回呼んで delta を作る)。"""
    sql = (  # noqa: S608 — field は内部固定 (呼出側が列名を限定)
        f"SELECT {field} AS label, COUNT(*) AS n FROM articles "
        f"WHERE status='posted' AND {field} IS NOT NULL AND {field} != '' "
        f"AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) "
        f"GROUP BY {field}"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    return {str(r["label"]): int(r["n"]) for r in rows}


def _movers(
    current: dict[str, int],
    prior: dict[str, int],
    *,
    top: int = 6,
    label_of: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """current/prior dict → item 別 {label, current, prior, delta, share...} (current 降順)。

    **分析的誠実性 (発見1)**: 生カウントの delta は窓間の総量変化 (抽出カバレッジ増・
    収集量増) に汚染される。そこで各窓の **総数で正規化したシェア (%)** と、その
    パーセントポイント差 (share_delta_pp) を併せて返す。カバレッジ増が share では
    キャンセルされ、「本当に比重が動いたか」だけが残る。生カウントは副指標として保持。
    """
    total_cur = sum(current.values())
    total_pri = sum(prior.values())
    # 型付きタプルでソート (dict 値の union を避ける)。
    scored: list[tuple[int, int, str]] = [
        (current.get(k, 0), abs(current.get(k, 0) - prior.get(k, 0)), k)
        for k in (set(current) | set(prior))
    ]
    scored.sort(reverse=True)
    out: list[dict[str, Any]] = []
    for cur, _abs_delta, k in scored[:top]:
        pri = prior.get(k, 0)
        share = (cur / total_cur) if total_cur else 0.0
        share_pri = (pri / total_pri) if total_pri else 0.0
        out.append(
            {
                "key": k,
                "label": label_of(k) if label_of else k,
                "current": cur,
                "prior": pri,
                "delta": cur - pri,
                "share_pct": round(share * 100, 1),
                "share_prior_pct": round(share_pri * 100, 1),
                "share_delta_pp": round((share - share_pri) * 100, 1),
            }
        )
    return out


def _region_counts(country_counts: dict[str, int]) -> dict[str, int]:
    out: Counter[str] = Counter()
    for iso, n in country_counts.items():
        region = _REGION_MAP.get(iso.upper())
        if region:
            out[region[0]] += n
    return dict(out)


def _actor_nation_counts(
    con: Any, since: datetime, until: datetime, nation_map: dict[str, str]
) -> dict[str, int]:
    """article_entities(actor) を窓で集計し、actor→nation で国籍別件数に。"""
    # D1 (2026-07-27): 報告/防御機関を除外 + subject-gate (評価済みは主題のみ、legacy は fallback)。
    from src.cti.actor_roles import reporter_org_actor_ids
    from src.cti.subject_gate import subject_gate_clause

    reporter_ids = reporter_org_actor_ids()
    gate = subject_gate_clause(
        mention_col="ae.value",
        subject_ids_col="a.subject_actor_ids",
        subject_source_col="a.subject_actor_source",
    )
    sql = (
        "SELECT ae.value AS actor FROM article_entities ae "
        "JOIN articles a ON a.article_id = ae.article_id "
        "WHERE ae.entity_type='actor' AND a.status='posted' "
        "AND datetime(a.created_at) >= datetime(?) AND datetime(a.created_at) < datetime(?) "
        f"AND {gate}"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    out: Counter[str] = Counter()
    for r in rows:
        actor_id = str(r["actor"])
        if actor_id in reporter_ids:
            continue
        nat = nation_map.get(actor_id)
        if nat:
            out[nat] += 1
    return dict(out)


def _vulnerabilities(
    con: Any, since: datetime, until: datetime, *, limit: int = 6
) -> list[dict[str, Any]]:
    """窓内 CVE を KEV(実悪用) / 高CVSS で重要度付けし上位を返す。"""
    from src.tools.kev_client import get_kev_cve_set
    from src.tools.nvd_client import get_cvss

    sql = (
        "SELECT ae.value AS cve, a.article_id AS article_id, a.title AS title, a.url AS url, "
        "a.created_at AS created_at FROM article_entities ae "
        "JOIN articles a ON a.article_id = ae.article_id "
        "WHERE ae.entity_type='cve' AND a.status='posted' "
        "AND datetime(a.created_at) >= datetime(?) AND datetime(a.created_at) < datetime(?) "
        "ORDER BY datetime(a.created_at) DESC"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    kev = get_kev_cve_set()
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        cve = str(r["cve"]).upper()
        if cve in seen:
            continue  # CVE 単位で dedup (最新記事を保持)
        cvss_pair = get_cvss(cve)
        title = str(r["title"] or "")
        seen[cve] = {
            "cve": cve,
            "cvss": round(cvss_pair[0], 1) if cvss_pair else None,
            "kev": cve in kev,
            "title": title,
            "url": str(r["url"] or ""),
            "article_id": str(r["article_id"] or ""),
            "created_at": str(r["created_at"] or ""),
            # 発見4: まとめ記事由来は title が無意味 → frontend が CVE 主体表示に切替。
            "is_roundup": _is_roundup(title),
            "nvd_url": _NVD_DETAIL.format(cve=cve),
        }
    items = list(seen.values())
    # KEV(実悪用) を最優先、次に CVSS 降順。「実際に効く脅威」を上に。
    items.sort(key=lambda v: (v["kev"], v["cvss"] or 0.0), reverse=True)
    return items[:limit]


# 防衛 CTI ミッション中核の配信チャンネル (日本標的判定に使用)。
# H4: 判定の SSoT は src/cti/japan_relevance.py (threat_operations と同一定義で数える)。
_JAPAN_CHANNEL = JAPAN_WATCH_CHANNEL


def _japan_targeted_count(con: Any, since: datetime, until: datetime) -> int:
    """日本標的 (victim=JP or japan_watch ch) で high/medium の観測件数 (発見5)。

    JP 防衛アナリストの最重要 KPI。importance low は除外し「読むべき日本案件」に絞る。
    """
    sql = (
        "SELECT COUNT(*) AS n FROM articles WHERE status='posted' "
        "AND importance IN ('high','medium') "
        f"AND {JAPAN_TARGETED_SQL_FRAGMENT} "
        "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
    )
    row = con.execute(sql, (since.isoformat(), until.isoformat())).fetchone()
    return int(row["n"]) if row else 0


def _actionable_count(con: Any, since: datetime, until: datetime, kev_set: AbstractSet[str]) -> int:
    """「要対応」の distinct 記事数 (発見2 + 有機的結合監査 H5 で拡張)。

    定義 = high∩(日本標的 or KEV) + 日本標的の ransomware 被害 + 日本標的の
    prepositioning (確定のみ)。生 high は triage バケツのサイズで警報数ではない —
    「実際に効く/我々に近い」signal で絞った decision-bearing な部分集合を主 KPI にする。
    H5: 生成物/ransomware 取込は importance=medium 固定のため、旧定義 (high 限定) では
    JP ランサム被害・OT 段階事案が構造的に漏れていた。importance でなく述語側で拾う。
    """
    sql = (
        "SELECT a.id AS aid, a.victim_country_iso AS vc, a.posted_channel AS ch, "
        "a.importance AS imp, a.is_ransomware AS ransom, "
        "a.socio_political_intent AS intent, a.intent_confidence AS iconf, "
        "ae.value AS cve FROM articles a "
        "LEFT JOIN article_entities ae "
        "ON ae.article_id = a.article_id AND ae.entity_type='cve' "
        "WHERE a.status='posted' "
        "AND datetime(a.created_at) >= datetime(?) AND datetime(a.created_at) < datetime(?)"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    actionable: set[Any] = set()
    for r in rows:
        jp = is_japan_targeted_row(str(r["vc"] or ""), str(r["ch"] or ""))
        if str(r["imp"] or "") == "high":
            cve = str(r["cve"] or "").upper()
            if jp or (cve and cve in kev_set):
                actionable.add(r["aid"])
                continue
        if not jp:
            continue
        # JP ランサム被害 (medium 固定でも要対応)
        if bool(r["ransom"]):
            actionable.add(r["aid"])
            continue
        # OT 段階の代理指標: 確定 (low=仮説を除く) prepositioning × 日本標的
        if str(r["intent"] or "") == "prepositioning" and str(r["iconf"] or "") != "low":
            actionable.add(r["aid"])
    return len(actionable)


def _kev_cves_in_window(
    con: Any, since: datetime, until: datetime, kev_set: AbstractSet[str]
) -> set[str]:
    """窓内で観測された KEV(実悪用) CVE の集合 (新規判定 / 件数に使用)。"""
    sql = (
        "SELECT DISTINCT ae.value AS cve FROM article_entities ae "
        "JOIN articles a ON a.article_id = ae.article_id "
        "WHERE ae.entity_type='cve' AND a.status='posted' "
        "AND datetime(a.created_at) >= datetime(?) AND datetime(a.created_at) < datetime(?)"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    return {str(r["cve"]).upper() for r in rows if str(r["cve"]).upper() in kev_set}


def _adversary_actors_in_window(
    con: Any, since: datetime, until: datetime, nation_map: dict[str, str]
) -> set[str]:
    """窓内で活動した敵対国籍 (中露朝イ) アクターの distinct 集合 (発見5)。"""
    sql = (
        "SELECT DISTINCT ae.value AS actor FROM article_entities ae "
        "JOIN articles a ON a.article_id = ae.article_id "
        "WHERE ae.entity_type='actor' AND a.status='posted' "
        "AND datetime(a.created_at) >= datetime(?) AND datetime(a.created_at) < datetime(?)"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    return {
        str(r["actor"]) for r in rows if nation_map.get(str(r["actor"]), "") in _ADVERSARY_NATIONS
    }


def _source_confidence(con: Any, since: datetime, until: datetime) -> dict[str, int]:
    """窓内 posted 記事の source 信頼度ティア構成 (分析的誠実性: 抑制でなく表示)。"""
    from src.cti.source_basis import classify_source_tier

    sql = (
        "SELECT feed_title, feed_url FROM articles WHERE status='posted' "
        "AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
    )
    rows = con.execute(sql, (since.isoformat(), until.isoformat())).fetchall()
    out: Counter[str] = Counter()
    for r in rows:
        tier = classify_source_tier(str(r["feed_title"] or ""), str(r["feed_url"] or ""))
        out[tier] += 1
    return {t: out.get(t, 0) for t in ("official", "research", "news", "social", "unknown")}


def _notable_actors(db_path: Path) -> list[dict[str, Any]]:
    """spike / 新規 アクターを実体で (カウントでなく中身=triage)。既存 snapshot を流用。

    lookback は脅威アクター画面と同じ 30日 (drill-in 時に sparkline が一致するように)。
    """
    try:
        from src.ui.services.threat_operations import fetch_threat_operations_snapshot

        snap = fetch_threat_operations_snapshot(
            lookback_days=_NOTABLE_LOOKBACK_DAYS, db_path=db_path
        )
        d = snap.discovery
    except Exception as e:  # noqa: BLE001
        _log.warning("overview_notable_failed", error=str(e))
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tag, actors in (("spike", d.spiking_actors), ("new", d.new_actors)):
        for a in actors:
            if a.actor_id in seen:
                continue
            seen.add(a.actor_id)
            out.append(
                {
                    "actor_id": a.actor_id,
                    "canonical": a.canonical,
                    "nation": _NATION_LABELS.get(a.nation or "", (a.nation or "").upper()),
                    "tag": tag,
                    "total": a.total_articles,
                    "sparkline": a.sparkline,
                }
            )
    return out[:8]


def build_overview(*, window_days: int = 7, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """概観の集計を 1 バッチで返す。各内訳は現在窓 + 前期窓で delta を持つ。"""
    from src.cti.actor_normalizer import load_actor_aliases

    now = datetime.now(UTC)
    cur_since = now - timedelta(days=window_days)
    prior_since = now - timedelta(days=window_days * 2)

    nation_map = {a.id: a.nation for a in load_actor_aliases().actors if a.nation}

    con = _connect(db_path)
    try:
        # 重要度 (現在の構成 + high の前期比)
        imp_cur = _grouped_counts(con, "importance", cur_since, now)
        imp_prior = _grouped_counts(con, "importance", prior_since, cur_since)

        # セクター (戦術) / 国 (→地域、戦略) / 敵対国籍 (戦略)
        # uncategorized 等の noise は標的セクター KPI から除外 (信号品質)。
        sec_cur = {
            k: v
            for k, v in _grouped_counts(con, "victim_sector_canonical", cur_since, now).items()
            if k not in _NON_SECTOR_KEYS
        }
        sec_prior = {
            k: v
            for k, v in _grouped_counts(
                con, "victim_sector_canonical", prior_since, cur_since
            ).items()
            if k not in _NON_SECTOR_KEYS
        }
        ctry_cur = _grouped_counts(con, "victim_country_iso", cur_since, now)
        ctry_prior = _grouped_counts(con, "victim_country_iso", prior_since, cur_since)
        nat_cur = _actor_nation_counts(con, cur_since, now, nation_map)
        nat_prior = _actor_nation_counts(con, prior_since, cur_since, nation_map)

        sectors = _movers(sec_cur, sec_prior, label_of=lambda k: _sector_labels().get(k, k))
        regions = _movers(
            _region_counts(ctry_cur),
            _region_counts(ctry_prior),
            label_of=lambda k: _REGION_LABELS.get(k, k),
        )
        nations = _movers(nat_cur, nat_prior, label_of=lambda k: _NATION_LABELS.get(k, k.upper()))
        # 敵対国籍 (中露朝イ) を前面、その他は後ろ
        nations.sort(key=lambda m: (m["key"] in _ADVERSARY_NATIONS, m["current"]), reverse=True)

        vulns = _vulnerabilities(con, cur_since, now)
        confidence = _source_confidence(con, cur_since, now)

        # ── ミッション KPI (発見2/5): KEV 集合を 1 回取得し各集計で共有 ──
        from src.tools.kev_client import get_kev_cve_set

        kev_set = get_kev_cve_set()

        actionable_cur = _actionable_count(con, cur_since, now, kev_set)
        actionable_prior = _actionable_count(con, prior_since, cur_since, kev_set)

        jp_cur = _japan_targeted_count(con, cur_since, now)
        jp_prior = _japan_targeted_count(con, prior_since, cur_since)

        kev_cur = _kev_cves_in_window(con, cur_since, now, kev_set)
        kev_prior = _kev_cves_in_window(con, prior_since, cur_since, kev_set)
        kev_new = sorted(kev_cur - kev_prior)

        apt_cur = _adversary_actors_in_window(con, cur_since, now, nation_map)
        apt_prior = _adversary_actors_in_window(con, prior_since, cur_since, nation_map)
    finally:
        con.close()

    notable = _notable_actors(db_path)
    spiking_adv = [
        a for a in notable if a["tag"] == "spike" and a["nation"] in _NATION_LABELS.values()
    ]

    return {
        "as_of": now.isoformat(),
        "window_days": window_days,
        "importance": {
            "high": imp_cur.get("high", 0),
            "medium": imp_cur.get("medium", 0),
            "low": imp_cur.get("low", 0),
            "high_prior": imp_prior.get("high", 0),
            "high_delta": imp_cur.get("high", 0) - imp_prior.get("high", 0),
        },
        # 要対応: high のうち「効く/近い」signal を持つ部分集合 (主 KPI)。
        "actionable": {
            "current": actionable_cur,
            "prior": actionable_prior,
            "delta": actionable_cur - actionable_prior,
        },
        "japan_targeted": {
            "current": jp_cur,
            "prior": jp_prior,
            "delta": jp_cur - jp_prior,
        },
        "kev": {
            "current": len(kev_cur),
            "prior": len(kev_prior),
            "delta": len(kev_cur) - len(kev_prior),
            "new_count": len(kev_new),
            "new_cves": kev_new[:8],
        },
        "active_apt": {
            "current": len(apt_cur),
            "prior": len(apt_prior),
            "delta": len(apt_cur) - len(apt_prior),
            "spiking": len(spiking_adv),
        },
        "sectors": sectors,
        "regions": regions,
        "nations": nations,
        "vulnerabilities": vulns,
        "notable_actors": notable,
        "source_confidence": confidence,
        "attention": _build_attention(kev_new, notable, jp_cur - jp_prior),
    }


def _build_attention(
    kev_new: list[str],
    notable: list[dict[str, Any]],
    jp_delta: int,
) -> list[dict[str, Any]]:
    """「今日の要注意」triage 行 (発見7)。briefing と差別化する dashboard の存在理由。

    既存集計を再利用し ≤5 件にマージ。優先度: 新規KEV悪用 > 新規アクター >
    敵対アクター spike > 日本標的の急増。href は drill-in 先。
    """
    items: list[dict[str, Any]] = []
    for cve in kev_new[:3]:
        items.append(
            {
                "kind": "kev",
                "label": cve,
                "detail": "新規 KEV 悪用",
                "href": _NVD_DETAIL.format(cve=cve),
            }
        )
    for a in notable:
        if a["tag"] == "new":
            items.append(
                {
                    "kind": "new_actor",
                    "label": a["canonical"],
                    "detail": f"新規アクター{(' ・' + a['nation']) if a['nation'] else ''}",
                    "href": f"{_INTEL_BASE}/threats",
                }
            )
    for a in notable:
        if a["tag"] == "spike" and a["nation"] in _NATION_LABELS.values():
            items.append(
                {
                    "kind": "spike",
                    "label": a["canonical"],
                    "detail": f"活動が急増・{a['nation']}",
                    "href": f"{_INTEL_BASE}/threats",
                }
            )
    if jp_delta > 0:
        items.append(
            {
                "kind": "japan",
                "label": "日本標的の観測増",
                "detail": f"前期比 +{jp_delta} 件",
                "href": f"{_INTEL_BASE}/threats",
            }
        )
    return items[:5]
