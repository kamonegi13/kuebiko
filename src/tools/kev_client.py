"""CISA KEV (Known Exploited Vulnerabilities) catalog client (Phase 1 K5)。

KEV は「実際に悪用が確認された CVE」の **権威ある catalog** (CISA 公式)。これまで
「KEV 該当 / 実悪用」は記事本文 prose からの regex 推定 (``_KEV_PATTERNS``) に依存して
いたが、本 client で CISA 公式 catalog との照合を可能にし deterministic な signal にする。

設計 (hot-path 安全):
- ``get_kev_cve_set`` / ``is_cve_on_kev`` は **cache を読むだけで fetch しない**
  (routing/triage の hot-path や test で network を踏まない)。cache 無しは空集合に degrade。
- ``refresh_kev_catalog`` のみが fetch する。**TTL を見て stale な時だけ** 取得し
  ``data/kev_cache.json`` を更新。app 起動時 + pipeline 開始時に呼ぶ。
- 公開 catalog (CVE-ID list) の取得のみ。記事本文等は一切送信しない (CLAUDE.md §9 適合)。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import httpx

from src.logging_config import get_logger

_log = get_logger(__name__)

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
_CACHE_PATH = Path("data/kev_cache.json")
_CACHE_TTL_SECONDS = 24 * 3600
_FETCH_TIMEOUT_SECONDS = 20.0

#: KEV 掲載を「記事の時点の悪用の根拠」とみなす掲載日 (dateAdded) と記事の事象時刻の差。
#: 掲載から 90 日を超えて出た記事 (何年も前に掲載された CVE の再報・Alert 更新) は根拠にしない。
#: 記事が掲載より数日先に出ることがあるので、掲載が記事の後でも 7 日までは許す。
KEV_EVIDENCE_WINDOW_DAYS = 90
KEV_EVIDENCE_LEAD_SLACK_DAYS = 7

# process 内 cache。_mem_set = CVE 集合、_mem_dates = CVE → 掲載日 (不明は None)。
_mem_set: frozenset[str] | None = None
_mem_dates: dict[str, date | None] | None = None


def _parse_date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _fetch_kev_entries() -> dict[str, str]:
    """CISA KEV catalog から {CVE-ID(大文字): dateAdded(YYYY-MM-DD, 不明は空)} を取得する。"""
    resp = httpx.get(KEV_URL, timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    vulns = data.get("vulnerabilities", []) if isinstance(data, dict) else []
    return {
        str(v["cveID"]).strip().upper(): str(v.get("dateAdded") or "")
        for v in vulns
        if isinstance(v, dict) and v.get("cveID")
    }


def _read_disk_cache() -> dict[str, date | None] | None:
    """disk cache を {CVE: 掲載日|None} で返す。旧形式 (CVE の配列) は掲載日 None。"""
    if not _CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k).upper(): _parse_date(v) for k, v in raw.items()}
        return {str(s).upper(): None for s in raw}
    except Exception:  # noqa: BLE001
        return None


def _set_mem(dates: dict[str, date | None]) -> None:
    global _mem_set, _mem_dates  # noqa: PLW0603
    _mem_dates = dates
    _mem_set = frozenset(dates)


def get_kev_cve_set() -> frozenset[str]:
    """KEV CVE-ID set を **cache から** 返す (fetch しない、hot-path 安全)。

    process 内 cache → disk cache の順に読む。どちらも無ければ空集合 (KEV 照合は無効化され
    regex/LLM 判定に degrade)。最新化は ``refresh_kev_catalog`` 経由。
    """
    if _mem_set is not None:
        return _mem_set
    _set_mem(_read_disk_cache() or {})
    return _mem_set or frozenset()


def get_kev_added_dates() -> dict[str, date | None]:
    """CVE → KEV 掲載日 (dateAdded)。cache から返す (fetch しない)。旧形式 cache は None。"""
    if _mem_dates is None:
        get_kev_cve_set()
    return _mem_dates or {}


def refresh_kev_catalog(max_age_seconds: int = _CACHE_TTL_SECONDS) -> int:
    """cache が無い/古い時のみ KEV catalog を fetch して更新する。返り値は CVE 件数。

    app 起動時 / pipeline 開始時に呼ぶ (hot-path では呼ばない)。fetch 失敗時は既存 cache に
    degrade。fresh な cache がある場合は fetch せず件数のみ返す。
    """
    now = time.time()
    # fresh な disk cache があれば fetch しない
    if _CACHE_PATH.exists():
        try:
            fresh = (now - _CACHE_PATH.stat().st_mtime) < max_age_seconds
        except OSError:
            fresh = False
        if fresh:
            disk = _read_disk_cache()
            if disk is not None:
                _set_mem(disk)
                return len(disk)
    # stale / 無し → fetch
    try:
        entries = _fetch_kev_entries()
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(dict(sorted(entries.items()))), encoding="utf-8")
        _set_mem({k: _parse_date(v) for k, v in entries.items()})
        _log.info("kev_catalog_refreshed", count=len(entries))
        return len(entries)
    except Exception as e:  # noqa: BLE001
        _log.warning("kev_fetch_failed", error=str(e))
        _set_mem(_read_disk_cache() or {})
        return len(_mem_set or ())


def is_cve_on_kev(cve_id: str) -> bool:
    """単一 CVE が KEV catalog に載っているか (cache 参照、fetch しない)。"""
    if not cve_id:
        return False
    return cve_id.strip().upper() in get_kev_cve_set()


def any_cve_on_kev(cve_ids: list[str]) -> bool:
    """CVE-ID list のいずれかが KEV に載っているか (空集合 / 空 list なら False)。"""
    if not cve_ids:
        return False
    kev = get_kev_cve_set()
    if not kev:
        return False
    return any(str(c).strip().upper() in kev for c in cve_ids)


def any_cve_on_kev_at(cve_ids: list[str], reference: date | None) -> bool:
    """記事の事象時刻 ``reference`` の時点で、KEV 掲載が悪用の根拠になる CVE があるか。

    「今 KEV にある」ではなく掲載日 (dateAdded) との差で見る: 掲載から
    ``KEV_EVIDENCE_WINDOW_DAYS`` 日を超えた記事 (古い CVE の再報) と、掲載より
    ``KEV_EVIDENCE_LEAD_SLACK_DAYS`` 日を超えて前の記事 (当時は未掲載) は根拠にしない。
    ``reference`` または掲載日が不明なら従来どおり「載っているか」で判定する。
    """
    if reference is None:
        return any_cve_on_kev(cve_ids)
    dates = get_kev_added_dates()
    for raw in cve_ids:
        cve = str(raw).strip().upper()
        if cve not in dates:
            continue
        added = dates[cve]
        if added is None:
            return True
        lag_days = (reference - added).days
        if -KEV_EVIDENCE_LEAD_SLACK_DAYS <= lag_days <= KEV_EVIDENCE_WINDOW_DAYS:
            return True
    return False
