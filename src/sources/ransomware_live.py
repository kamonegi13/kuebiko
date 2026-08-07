"""ransomware.live の構造化被害者データ取込 (Phase: 被害状況コレクタ)。

ransomware.live はランサムウェアの data-leak-site (DLS) を集約する無料サービス。
**構造化 JSON API** (victim / group / country(ISO2) / activity(業種) / 日付 / description) を
持つため、ニュース記事のように LLM で本文抽出する必要がなく、**被害組織・被害国・攻撃者を
決定論的に**得られる (地図の被害カバレッジ律速を直撃)。Grok JSONL と同じ「構造化 in →
構造化 out」パターン ([[geo_map_design_discussion]])。

OPSEC: 取得するのは公開 DLS 集約データのみ (全件 pull、狙い撃ちクエリではない)。本サービスは
既に RSS で hourly pull 済みのため追加の egress 露出はない。記事本文をクラウドに送らない原則も不変。

エンドポイント (v2):
  - ``/v2/recentvictims``       直近の被害者 (global、~100 件)。日次の ongoing 取込用。
  - ``/v2/countryvictims/{cc}`` 国別の被害者 (履歴付き)。JP backfill に使う (304 件規模)。

本モジュールは **取得・パース・正規化・dedup までの純粋な核** を提供する。Article/Discord への
変換と pipeline 配線は呼出側 (main.py) が担う (投稿ポリシーから独立してテスト可能にするため)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from src.cti.taxonomy_normalizer import TaxonomyNormalizer, load_normalizer
from src.logging_config import get_logger

_log = get_logger(__name__)

_API_BASE = "https://api.ransomware.live/v2"
_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "kuebiko-bot"


@dataclass(frozen=True)
class RansomwareIncident:
    """1 件のランサムウェア被害 (構造化、map/persist 用に正規化済)。"""

    victim: str  # 被害組織名 (原文ママ)
    group: str  # 攻撃グループ名 (ransom_group)
    country_iso: str | None  # ISO 3166-1 alpha-2 (解決できなければ None)
    country_raw: str  # API が返した生の国表記
    sector_canonical: str | None  # taxonomy 正規化後 (未マップは None)
    sector_raw: str  # API の activity 原文
    discovered: str  # 発覚/掲載日 (ISO8601 文字列、dedup と published_at に使う)
    description: str  # DLS 投稿の説明文
    url: str  # claim_url / post_url (永続 URL)


def _first_str(raw: dict[str, object], *keys: str) -> str:
    """複数候補キーから最初の非空文字列を返す (endpoint 間のキー差異を吸収)。"""
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_incident(
    raw: dict[str, object], normalizer: TaxonomyNormalizer
) -> RansomwareIncident | None:
    """API の生 dict を ``RansomwareIncident`` に正規化する。

    recentvictims (victim/group/url) と countryvictims (post_title/group_name/post_url) で
    キー名が異なるため両対応。被害組織名が取れない record は ``None`` で skip。
    """
    victim = _first_str(raw, "victim", "post_title")
    if not victim:
        return None
    group = _first_str(raw, "group", "group_name")
    country_raw = _first_str(raw, "country")
    sector_raw = _first_str(raw, "activity", "sector")
    discovered = _first_str(raw, "discovered", "published", "attackdate")
    description = _first_str(raw, "description")
    url = _first_str(raw, "claim_url", "post_url", "url", "website")

    country_iso, _ = normalizer.normalize_country(country_raw) if country_raw else (None, None)
    sector_canonical, _ = normalizer.normalize_sector(sector_raw) if sector_raw else (None, None)

    return RansomwareIncident(
        victim=victim,
        group=group,
        country_iso=country_iso,
        country_raw=country_raw,
        sector_canonical=sector_canonical,
        sector_raw=sector_raw,
        discovered=discovered,
        description=description,
        url=url,
    )


def dedup_key(inc: RansomwareIncident) -> str:
    """安定 dedup キー (group + victim + 発覚日)。

    同一被害が複数 endpoint / 複数 run で重複しても 1 件に収斂させる。日付は date 部分のみ
    使い (timestamp の揺らぎを無視)、source プレフィクスで他 source と衝突しない名前空間にする。
    """
    day = (inc.discovered or "")[:10]
    basis = f"ransomware.live|{inc.group.lower()}|{inc.victim.lower()}|{day}"
    return "rl_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]  # noqa: S324


def parse_incidents(
    rows: list[dict[str, object]], normalizer: TaxonomyNormalizer | None = None
) -> list[RansomwareIncident]:
    """生 dict のリストを正規化し、被害組織名が取れたものだけ返す。"""
    norm = normalizer or load_normalizer()
    out: list[RansomwareIncident] = []
    for r in rows:
        inc = parse_incident(r, norm)
        if inc is not None:
            out.append(inc)
    return out


def _fetch_json(path: str, client: httpx.Client | None = None) -> list[dict[str, object]]:
    """API から JSON リストを取得 (失敗時は空リストに fail-safe)。"""
    url = f"{_API_BASE}{path}"
    owns = client is None
    cli = client or httpx.Client(timeout=_TIMEOUT_SECONDS, headers={"user-agent": _USER_AGENT})
    try:
        resp = cli.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        _log.warning("ransomware_live_unexpected_payload", path=path, type=type(data).__name__)
        return []
    except Exception as e:  # noqa: BLE001 — 外部 API 障害は空で継続 (pipeline を止めない)
        _log.warning("ransomware_live_fetch_failed", path=path, error=str(e))
        return []
    finally:
        if owns:
            cli.close()


def fetch_recent(client: httpx.Client | None = None) -> list[dict[str, object]]:
    """直近の被害者 (global)。日次 ongoing 取込用。"""
    return _fetch_json("/recentvictims", client)


def fetch_country(iso: str, client: httpx.Client | None = None) -> list[dict[str, object]]:
    """国別の被害者 (履歴付き)。JP backfill 等に使う。"""
    code = (iso or "").strip().upper()
    if not (len(code) == 2 and code.isalpha()):
        return []
    return _fetch_json(f"/countryvictims/{code}", client)
