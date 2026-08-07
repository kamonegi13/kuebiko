"""脅威マップ API (Phase 3)。

蓄積した victim_country / actor 帰属を地図プロット用に集約して返す read-only endpoint。
集計は ``src/ui/services/geo_cyber_map.py``、描画は frontend の自前 SVG (外部タイル/geo
ライブラリ非依存、CLAUDE.md エアギャップ原則)。counts のみ返すため secret 非露出 →
READ_ONLY instance でも安全。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

geo_api = APIRouter(prefix="/api/v1/geo", tags=["geo"])

# 集計はやや重い (全 posted 記事スキャン) ので overview と同じ in-process cache。
_TTL_SECONDS = 60.0
# key = (window_days, threat_class, source_status, min_importance, pmesii), value = (ts, payload)
_cache: dict[tuple[int, str, str, str, str, str], tuple[float, dict[str, Any]]] = {}

_THREAT_CLASSES = ("all", "ransomware", "other")


def _norm_class(value: str) -> str:
    """threat_class を allowlist に正規化 (任意文字列を SQL に流さない)。"""
    v = value.strip().lower()
    return v if v in _THREAT_CLASSES else "all"


_SOURCE_STATUSES = ("all", "posted", "collected")


def _norm_source_status(value: str) -> str:
    """source_status を allowlist に正規化 (報道=posted / 台帳=collected / すべて)。"""
    v = value.strip().lower()
    return v if v in _SOURCE_STATUSES else "all"


_IMPORTANCE = ("all", "medium_up", "high")


def _norm_importance(value: str) -> str:
    """min_importance を allowlist に正規化 (意義の地図 = 重要度しきい値)。"""
    v = value.strip().lower()
    return v if v in _IMPORTANCE else "all"


# 地域フィルタの ISO 上限 (欧州でも ~30、暴走/超長クエリ防止)。
_MAX_REGION_ISOS = 80


def _parse_isos(value: str) -> list[str]:
    """カンマ区切りの ISO3166-1 alpha-2 を検証して返す (任意文字列を SQL に流さない)。"""
    out: list[str] = []
    for tok in value.split(","):
        code = tok.strip().upper()
        if len(code) == 2 and code.isalpha():
            out.append(code)
        if len(out) >= _MAX_REGION_ISOS:
            break
    return out


_DOMAINS = ("cyber", "geopolitical")


def _norm_domain(value: str) -> str:
    """domain を allowlist に正規化 (cyber / geopolitical)。"""
    v = value.strip().lower()
    return v if v in _DOMAINS else "cyber"


# PMESII 直交フィルタ (地政学レイヤー用): all / 8 軸。任意文字列を SQL に流さない。
_PMESII = ("all", "p", "m", "e", "s", "i_infra", "i_cyber", "p_env", "t")


def _norm_pmesii(value: str) -> str:
    """pmesii 軸を allowlist に正規化 (地政学レイヤーの領域フィルタ、色=動機 と直交)。"""
    v = value.strip().lower()
    return v if v in _PMESII else "all"


@geo_api.get("/cyber-map")
def get_cyber_map(
    days: int = Query(default=30, ge=0, le=365),
    threat_class: str = Query(default="all"),
    source_status: str = Query(default="all"),
    min_importance: str = Query(default="all"),
    pmesii: str = Query(default="all"),
    time_basis: str = Query(default="report"),
) -> dict[str, Any]:
    """国別 被害件数バブル + 地政学イベント(動機色) + カバレッジ信頼層 + 盤外件数を返す。

    ``days=0`` は全期間。``threat_class`` = all / ransomware / other で被害を絞る
    (ransomware = ransomware.live 由来 + 攻撃者が ransom_group)。``source_status`` = all /
    posted(報道) / collected(台帳) で収集経路を絞る。``min_importance`` = all / medium_up /
    high で「意義の地図」(重要度しきい値、cyber)。``pmesii`` = all / p..t で地政学レイヤーを
    領域で直交フィルタ (色=動機 intent と直交)。read-only・counts のみ。
    """
    from src.ui.services.geo_cyber_map import build_cyber_map

    window = max(0, min(365, days))
    cls = _norm_class(threat_class)
    src = _norm_source_status(source_status)
    imp = _norm_importance(min_importance)
    pm = _norm_pmesii(pmesii)
    tb = "event" if time_basis == "event" else "report"
    now = time.monotonic()
    key = (window, cls, src, imp, pm, tb)
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    data = build_cyber_map(
        window_days=window or None,
        threat_class=cls,
        source_status=src,
        min_importance=imp,
        pmesii=pm,
        time_basis=tb,
    )
    _cache[key] = (now, data)
    return data


@geo_api.get("/trend")
def get_trend(
    days: int = Query(default=30, ge=0, le=365),
    threat_class: str = Query(default="all"),
    group_by: str = Query(default="country"),
    domain: str = Query(default="cyber"),
    source_status: str = Query(default="all"),
    isos: str = Query(default=""),
    min_importance: str = Query(default="all"),
    pmesii: str = Query(default="all"),
) -> dict[str, Any]:
    """被害/事象の日次件数推移を国別/セクター別 上位系列で返す (地図下グラフ)。

    ``isos`` (カンマ区切り ISO3166-1 alpha-2) 指定で地域に絞る (P2 地域ブラッシング)。
    ``min_importance`` で意義しきい値 (cyber)、``pmesii`` で領域フィルタ (geopolitical)。read-only。
    """
    from src.ui.services.geo_cyber_map import trend

    gb = group_by if group_by in ("country", "sector") else "country"
    region_isos = _parse_isos(isos) if isos else None
    return trend(
        window_days=(max(0, min(365, days)) or None),
        threat_class=_norm_class(threat_class),
        group_by=gb,
        domain=_norm_domain(domain),
        source_status=_norm_source_status(source_status),
        isos=region_isos,
        min_importance=_norm_importance(min_importance),
        pmesii=_norm_pmesii(pmesii),
    )


@geo_api.get("/country/{iso}")
def get_country_articles(
    iso: str,
    days: int = Query(default=30, ge=0, le=365),
    threat_class: str = Query(default="all"),
    domain: str = Query(default="cyber"),
    source_status: str = Query(default="all"),
    min_importance: str = Query(default="all"),
    pmesii: str = Query(default="all"),
) -> dict[str, Any]:
    """指定国の 被害/事象 記事を新しい順で返す (バブル/ダイヤ click-to-drill)。read-only。"""
    from src.ui.services.geo_cyber_map import country_articles

    code = iso.strip().upper()
    if not (len(code) == 2 and code.isalpha()):
        raise HTTPException(status_code=400, detail="iso は ISO 3166-1 alpha-2")
    return country_articles(
        code,
        window_days=(max(0, min(365, days)) or None),
        threat_class=_norm_class(threat_class),
        domain=_norm_domain(domain),
        source_status=_norm_source_status(source_status),
        min_importance=_norm_importance(min_importance),
        pmesii=_norm_pmesii(pmesii),
    )
