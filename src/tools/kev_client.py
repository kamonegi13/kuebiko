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
from pathlib import Path

import httpx

from src.logging_config import get_logger

_log = get_logger(__name__)

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
_CACHE_PATH = Path("data/kev_cache.json")
_CACHE_TTL_SECONDS = 24 * 3600
_FETCH_TIMEOUT_SECONDS = 20.0

# process 内 cache。
_mem_set: frozenset[str] | None = None


def _fetch_kev_cve_ids() -> set[str]:
    """CISA KEV catalog から CVE-ID 集合を取得する (大文字正規化)。"""
    resp = httpx.get(KEV_URL, timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    vulns = data.get("vulnerabilities", []) if isinstance(data, dict) else []
    return {
        str(v["cveID"]).strip().upper() for v in vulns if isinstance(v, dict) and v.get("cveID")
    }


def _read_disk_cache() -> frozenset[str] | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        ids = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        return frozenset(str(s).upper() for s in ids)
    except Exception:  # noqa: BLE001
        return None


def get_kev_cve_set() -> frozenset[str]:
    """KEV CVE-ID set を **cache から** 返す (fetch しない、hot-path 安全)。

    process 内 cache → disk cache の順に読む。どちらも無ければ空集合 (KEV 照合は無効化され
    regex/LLM 判定に degrade)。最新化は ``refresh_kev_catalog`` 経由。
    """
    global _mem_set  # noqa: PLW0603
    if _mem_set is not None:
        return _mem_set
    disk = _read_disk_cache()
    _mem_set = disk if disk is not None else frozenset()
    return _mem_set


def refresh_kev_catalog(max_age_seconds: int = _CACHE_TTL_SECONDS) -> int:
    """cache が無い/古い時のみ KEV catalog を fetch して更新する。返り値は CVE 件数。

    app 起動時 / pipeline 開始時に呼ぶ (hot-path では呼ばない)。fetch 失敗時は既存 cache に
    degrade。fresh な cache がある場合は fetch せず件数のみ返す。
    """
    global _mem_set  # noqa: PLW0603
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
                _mem_set = disk
                return len(disk)
    # stale / 無し → fetch
    try:
        ids = _fetch_kev_cve_ids()
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        _mem_set = frozenset(ids)
        _log.info("kev_catalog_refreshed", count=len(ids))
        return len(ids)
    except Exception as e:  # noqa: BLE001
        _log.warning("kev_fetch_failed", error=str(e))
        disk = _read_disk_cache()
        _mem_set = disk if disk is not None else frozenset()
        return len(_mem_set)


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
