"""NVD (National Vulnerability Database) CVSS client (Phase 2.5 K5b)。

KEV (実悪用の権威 catalog) を補完し、CVE の **深刻度 (CVSS base score)** を NVD 公式
API から取得して deterministic に扱えるようにする。これまで「危険な脆弱性か」は記事本文
prose からの regex 推定に依存していた。

設計 (KEV client と同じ hot-path 安全思想):
- ``get_cvss`` は **cache を読むだけで fetch しない** (routing/表示の hot-path で network を
  踏まない)。cache 無しは None に degrade。
- ``refresh_cvss`` のみが fetch する。未取得 / TTL 切れの CVE だけを **件数・時間で bounded**
  に取得 (NVD は API key 無しだと ~5 req/30s のため rate limit + deadline で抑制)。
- 公開脆弱性 metadata (CVSS) の取得のみ。記事本文等は一切送信しない (CLAUDE.md §9 適合)。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from src.logging_config import get_logger

_log = get_logger(__name__)

NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_CACHE_PATH = Path("data/nvd_cache.json")
_CACHE_TTL_SECONDS = 30 * 24 * 3600  # CVSS は安定するので 30 日
_FETCH_TIMEOUT_SECONDS = 20.0
# API key 無しの NVD レート制限 (~5 req/30s) を尊重する最小間隔。
_MIN_FETCH_INTERVAL_SECONDS = 6.5
_CVE_RE = re.compile(r"^CVE-(?:19|20)\d{2}-\d{4,7}$", re.IGNORECASE)
CRITICAL_CVSS_THRESHOLD = 9.0

# {CVE(upper): {"base_score": float, "severity": str, "fetched_at": epoch}}
_mem_cache: dict[str, dict[str, Any]] | None = None


def _load_cache() -> dict[str, dict[str, Any]]:
    global _mem_cache  # noqa: PLW0603
    if _mem_cache is not None:
        return _mem_cache
    if _CACHE_PATH.exists():
        try:
            raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _mem_cache = {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
                return _mem_cache
        except Exception:  # noqa: BLE001
            pass
    _mem_cache = {}
    return _mem_cache


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    except OSError as e:
        _log.warning("nvd_cache_write_failed", error=str(e))


def get_cvss(cve_id: str) -> tuple[float, str] | None:
    """CVE の ``(base_score, severity)`` を **cache から** 返す (fetch しない)。

    cache 無しは None (CVSS 不明として degrade)。最新化は ``refresh_cvss`` 経由。
    """
    if not cve_id:
        return None
    entry = _load_cache().get(cve_id.strip().upper())
    if not entry:
        return None
    score = entry.get("base_score")
    if not isinstance(score, (int, float)):
        return None
    return float(score), str(entry.get("severity") or "")


def max_cvss(cve_ids: list[str]) -> float:
    """CVE-ID list の中で cache にある CVSS の最大値 (無ければ 0.0)。"""
    best = 0.0
    for c in cve_ids:
        info = get_cvss(c)
        if info and info[0] > best:
            best = info[0]
    return best


def get_affected(cve_id: str) -> tuple[list[str], list[str]]:
    """CVE の affected ``(vendors, products)`` を cache から返す (無ければ ([], []))。"""
    entry = _load_cache().get(cve_id.strip().upper())
    if not isinstance(entry, dict):
        return [], []
    vendors = entry.get("vendors")
    products = entry.get("products")
    return (
        [str(v) for v in vendors] if isinstance(vendors, list) else [],
        [str(p) for p in products] if isinstance(products, list) else [],
    )


def affected_vendors(cve_ids: list[str]) -> list[str]:
    """CVE-ID list 全体の affected vendor 集合 (cache 由来、exposure 判定用)。"""
    out: set[str] = set()
    for c in cve_ids:
        out.update(get_affected(c)[0])
    return sorted(out)


def cves_for_vendor(vendor: str) -> list[str]:
    """affected vendor が ``vendor`` (大小無視) を含む CVE-ID リスト (cache 逆引き)。

    affected_vendor facet (vendor → CVE 群 → 記事) の 1 hop 目。cache のみ参照
    (network なし)。cache 未取得の CVE は対象外 (warming で増える)。
    """
    v = vendor.strip().lower()
    if not v:
        return []
    out: list[str] = []
    for cve, entry in _load_cache().items():
        if not isinstance(entry, dict):
            continue
        vendors = entry.get("vendors")
        if isinstance(vendors, list) and any(str(x).strip().lower() == v for x in vendors):
            out.append(cve)
    return sorted(out)


def all_vendors() -> list[str]:
    """cache に出現する全 affected vendor (facet autocomplete 用、sorted unique)。"""
    out: set[str] = set()
    for entry in _load_cache().values():
        if not isinstance(entry, dict):
            continue
        vendors = entry.get("vendors")
        if isinstance(vendors, list):
            out.update(str(x).strip() for x in vendors if str(x).strip())
    return sorted(out)


def cves_for_affected(term: str) -> list[str]:
    """affected **vendor または product** が ``term`` (大小無視) に一致する CVE-ID リスト。

    exposure facet を vendor 粒度 (fortinet) だけでなく product 粒度 (fortios) でも
    効かせるための逆引き (1 入力で vendor/product どちらでも絞れる)。cache のみ参照。
    """
    t = term.strip().lower()
    if not t:
        return []
    out: list[str] = []
    for cve, entry in _load_cache().items():
        if not isinstance(entry, dict):
            continue
        vendors = entry.get("vendors")
        products = entry.get("products")
        names = [str(x).strip().lower() for x in (vendors or []) if isinstance(vendors, list)]
        names += [str(x).strip().lower() for x in (products or []) if isinstance(products, list)]
        if t in names:
            out.append(cve)
    return sorted(out)


def all_affected() -> list[str]:
    """cache に出現する全 vendor + product (affected facet の autocomplete 候補、sorted unique)。"""
    out: set[str] = set()
    for entry in _load_cache().values():
        if not isinstance(entry, dict):
            continue
        for key in ("vendors", "products"):
            vals = entry.get(key)
            if isinstance(vals, list):
                out.update(str(x).strip() for x in vals if str(x).strip())
    return sorted(out)


def _parse_cvss(payload: dict[str, Any]) -> tuple[float, str] | None:
    """NVD API レスポンスから CVSS base score/severity を抽出 (v3.1>v3.0>v2)。"""
    vulns = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(vulns, list) or not vulns:
        return None
    cve = vulns[0].get("cve") if isinstance(vulns[0], dict) else None
    metrics = cve.get("metrics") if isinstance(cve, dict) else None
    if not isinstance(metrics, dict):
        return None
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        arr = metrics.get(key)
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            data = arr[0].get("cvssData", {})
            score = data.get("baseScore")
            if isinstance(score, (int, float)):
                sev = data.get("baseSeverity") or arr[0].get("baseSeverity") or ""
                return float(score), str(sev)
    return None


def _parse_cpe(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """NVD response から affected vendor/product を抽出。

    ``cpe:2.3:<part>:<vendor>:<product>:<version>:...`` の vendor/product を集める
    (``*`` / 空は無視、``_`` は空白化)。「自分のスタックは影響を受けるか」(exposure) 用。
    """
    vulns = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(vulns, list) or not vulns:
        return [], []
    cve = vulns[0].get("cve") if isinstance(vulns[0], dict) else None
    configs = cve.get("configurations") if isinstance(cve, dict) else None
    vendors: set[str] = set()
    products: set[str] = set()
    if isinstance(configs, list):
        for cfg in configs:
            nodes = cfg.get("nodes") if isinstance(cfg, dict) else None
            for node in nodes or []:
                matches = node.get("cpeMatch") if isinstance(node, dict) else None
                for m in matches or []:
                    crit = m.get("criteria") if isinstance(m, dict) else None
                    if not isinstance(crit, str) or not crit.startswith("cpe:2.3:"):
                        continue
                    parts = crit.split(":")
                    if len(parts) > 4:
                        v = parts[3].replace("_", " ").strip()
                        p = parts[4].replace("_", " ").strip()
                        if v and v != "*":
                            vendors.add(v)
                        if p and p != "*":
                            products.add(p)
    return sorted(vendors), sorted(products)


def _fetch_one(cve_id: str) -> dict[str, Any] | None:
    """NVD API から 1 CVE の生レスポンスを取得 (失敗時 None)。CVSS/CPE を呼出側で parse。"""
    resp = httpx.get(
        NVD_API_URL,
        params={"cveId": cve_id},
        timeout=_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else None


def refresh_cvss(
    cve_ids: list[str],
    *,
    max_fetch: int = 15,
    deadline_seconds: float = 45.0,
    max_age_seconds: int = _CACHE_TTL_SECONDS,
) -> int:
    """未取得 / TTL 切れの CVE の CVSS を bounded に fetch して cache を更新する。

    ``max_fetch`` 件 または ``deadline_seconds`` 経過で打ち切り (NVD のレート制限尊重)。
    fetch 失敗は skip (fail-safe)。返り値は新規 fetch できた件数。
    hot-path では呼ばない (app 起動時等に呼ぶ)。
    """
    cache = _load_cache()
    now = time.time()
    # 取得対象: 形式が正しく、未取得 or TTL 切れ。重複除去 + 入力順保持。
    targets: list[str] = []
    seen: set[str] = set()
    for raw in cve_ids:
        c = str(raw).strip().upper()
        if c in seen or not _CVE_RE.match(c):
            continue
        seen.add(c)
        entry = cache.get(c)
        if entry is not None:
            fetched = entry.get("fetched_at", 0)
            fresh = isinstance(fetched, (int, float)) and (now - fetched) < max_age_seconds
            # CPE (vendors) を後付けした移行期: fresh でも vendors 未取得なら再 fetch して補完。
            if fresh and "vendors" in entry:
                continue
        targets.append(c)

    fetched_count = 0
    started = time.monotonic()
    for i, cve in enumerate(targets):
        if fetched_count >= max_fetch:
            break
        if time.monotonic() - started > deadline_seconds:
            _log.info("nvd_refresh_deadline", fetched=fetched_count, remaining=len(targets) - i)
            break
        if i > 0:
            time.sleep(_MIN_FETCH_INTERVAL_SECONDS)
        try:
            payload = _fetch_one(cve)
        except Exception as e:  # noqa: BLE001
            _log.warning("nvd_fetch_failed", cve=cve, error=str(e))
            continue
        if payload is not None:
            cvss = _parse_cvss(payload)
            vendors, products = _parse_cpe(payload)
            new_entry: dict[str, Any] = {
                "fetched_at": now,
                "vendors": vendors,
                "products": products,
            }
            if cvss is not None:
                new_entry["base_score"] = cvss[0]
                new_entry["severity"] = cvss[1]
            cache[cve] = new_entry
            fetched_count += 1
    if fetched_count > 0:
        _save_cache(cache)
        _log.info("nvd_cvss_refreshed", fetched=fetched_count)
    return fetched_count
