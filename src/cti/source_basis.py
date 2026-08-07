"""出典基盤の算出 (S1: briefing の信頼度可視化)。

briefing (synthesis / spotlight) の各 key 主張について、**信頼度を LLM に判定させず
source メタから決定的に算出**する。NATO Admiralty Code の 2 軸 (信頼度=source / 確度=
裏付け) を、客観的で論争の少ない信号で近似する:

- corroboration: 裏付ける distinct source 数 (ランキング不要・公平)
- 公式権威: 一次 advisory / CISA KEV 登録 CVE (事実ベース)
- SNS 単一フラグ: Grok / X 等の最も明確な低信頼ケース

注意 (レイヤ分離): 本モジュールは **briefing 評価層の表示用**であり routing には使わない。
routing の信頼性判定は Phase B-cal で content-based (editorial_stance) に一本化済で、
source 識別子による hierarchy / 抑制は意図的に撤去されている (source_quality.yaml 参照)。
ここでの source tier は「各主張の足場を読者に提示する」目的に限定し、抑制・降格はしない。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

SourceTier = Literal["official", "research", "news", "social", "state_media", "unknown"]
Confidence = Literal["high", "medium", "low"]

# S4 (cross-source 検証): corroboration に使う entity 種別。
# **CVE のみ** に限定する。CVE は「同一脆弱性を複数 source が報告」= 真の裏取りだが、
# actor (例「APT41」「中国」) の共有は「同じアクターに言及」でしかなく、別事象まで
# 裏取りと誤カウントする (政策ニュースが src=15 に誤膨張する実害を確認、2026-06-10)。
_CORROBORATION_ENTITY_TYPES = ("cve",)
_MAX_CORROBORATION_ENTITIES = 6  # 1 主張あたりの逆引き entity 数上限 (コスト境界)
_CORROBORATION_FETCH_LIMIT = 200  # 1 entity あたりの逆引き article 数上限

# 信頼度ティアは **host suffix** で判定する (substring だと "t.co" が "mandiant.com" の
# "t.com" に誤一致する等の事故が起きるため)。feed_url が無い source は title keyword で補う。

# SNS / 単一投稿 (信頼度 低・未裏取り: Admiralty D-F 相当)
_SOCIAL_HOSTS = (
    "grok.com",
    "x.com",
    "twitter.com",
    "t.co",
    "mastodon.social",
    "bsky.app",
    "reddit.com",
)
_SOCIAL_TITLE = ("grok",)
# 一次 advisory / 公式機関 (信頼度 高: Admiralty A 相当)
_OFFICIAL_HOSTS = (
    "cisa.gov",
    "jpcert.or.jp",
    "ncsc.gov.uk",
    "ncsc.govt.nz",
    "bsi.bund.de",
    "cyber.gov.au",
    "csa.gov.sg",
    "enisa.europa.eu",
    "ipa.go.jp",
    "nvd.nist.gov",
    "cyber.gc.ca",
    "us-cert.gov",
)
_OFFICIAL_TITLE = ("cisa", "jpcert", "enisa")
# 確立した脅威リサーチ (信頼度 高め: Admiralty B 相当)
_RESEARCH_HOSTS = (
    "mandiant.com",
    "dragos.com",
    "eset.com",
    "paloaltonetworks.com",
    "talosintelligence.com",
    "cisco.com",
    "sentinelone.com",
    "crowdstrike.com",
    "kaspersky.com",
    "trendmicro.com",
    "checkpoint.com",
    "microsoft.com",
    "secureworks.com",
    "volexity.com",
    "recordedfuture.com",
    "claroty.com",
    "nozominetworks.com",
    "huntress.com",
    "redcanary.com",
    "merics.org",
    "rand.org",
)
_RESEARCH_TITLE = (
    "mandiant",
    "dragos",
    "eset",
    "unit 42",
    "unit42",
    "talos",
    "sentinelone",
    "crowdstrike",
    "kaspersky",
    "trend micro",
    "trendmicro",
    "check point",
    "checkpoint",
    "secureworks",
    "volexity",
    "recorded future",
    "team82",
    "claroty",
    "nozomi",
    "huntress",
    "red canary",
    "merics",
)
# 国営メディア / 影響工作 (信頼度の軸でなく framing バイアスを示す。事実は報じうるが
# 意図・責任帰属の framing をそのまま採らない。収集は維持し synthesis で割引)。
# seed のみ。判定は config/source_reliability.yaml + UI 上書きで自由に再分類できる
# (国籍でなく国家統制/出資で分類。独立系の The Insider / NK News 等は含めない)。
_STATE_MEDIA_HOSTS = (
    "rt.com",
    "sputnikglobe.com",
    "sputniknews.com",
)
_STATE_MEDIA_TITLE = (
    "russia today",
    "sputnik",
    "pravda",  # Pravda/Portal Kombat 影響工作網 (偽装ローカル名)
)


def _host_match(host: str, suffixes: tuple[str, ...]) -> bool:
    return bool(host) and any(host == s or host.endswith("." + s) for s in suffixes)


def _title_match(title: str, kws: tuple[str, ...]) -> bool:
    return any(k in title for k in kws)


# ── managed config (config/source_reliability.yaml) ──
# コード内の上記 tuple は built-in default (config 欠落時の fallback)。config が存在すれば
# その host/title を **追加マージ** する (実 feed 群を網羅。news catch-all 誤分類の是正)。
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_RELIABILITY_CONFIG_PATH = _CONFIG_DIR / "source_reliability.yaml"
# per-source 上書き (UI 専用・機械管理)。手編集せず SubscriptionsPage から編集する。
# patterns (curated default、コメント付き) と分離し、こちらは comment 無しで安全に rewrite。
_OVERRIDES_PATH = _CONFIG_DIR / "source_reliability_overrides.yaml"

# UI で選べる明示ティア (override 値の検証用)。
ASSIGNABLE_TIERS: tuple[SourceTier, ...] = (
    "official",
    "research",
    "news",
    "social",
    "state_media",
)


@dataclass(frozen=True)
class _ReliabilityPatterns:
    official_hosts: tuple[str, ...]
    official_titles: tuple[str, ...]
    research_hosts: tuple[str, ...]
    research_titles: tuple[str, ...]
    social_hosts: tuple[str, ...]
    social_titles: tuple[str, ...]
    state_media_hosts: tuple[str, ...]
    state_media_titles: tuple[str, ...]
    overrides: dict[str, str]  # source key (feed_url or title) -> tier (UI 上書き)


_patterns_cache: _ReliabilityPatterns | None = None


def _merge(defaults: tuple[str, ...], extra: object) -> tuple[str, ...]:
    """default tuple に config の list を union (重複排除、小文字化)。"""
    out = list(defaults)
    if isinstance(extra, list):
        for v in extra:
            s = str(v).strip().lower()
            if s and s not in out:
                out.append(s)
    return tuple(out)


def load_reliability_patterns(*, force_reload: bool = False) -> _ReliabilityPatterns:
    """信頼度パターンを built-in default + config のマージで返す (module cache)。"""
    global _patterns_cache
    if _patterns_cache is not None and not force_reload:
        return _patterns_cache

    cfg: dict[str, object] = {}
    try:
        if _RELIABILITY_CONFIG_PATH.exists():
            loaded = yaml.safe_load(_RELIABILITY_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
    except (OSError, yaml.YAMLError) as e:  # config 不正でも built-in default で動作継続
        _log.warning("source_reliability_config_load_failed", error=str(e))

    def _tier(name: str, field: str) -> object:
        tier = cfg.get(name)
        return tier.get(field) if isinstance(tier, dict) else None

    _patterns_cache = _ReliabilityPatterns(
        official_hosts=_merge(_OFFICIAL_HOSTS, _tier("official", "hosts")),
        official_titles=_merge(_OFFICIAL_TITLE, _tier("official", "titles")),
        research_hosts=_merge(_RESEARCH_HOSTS, _tier("research", "hosts")),
        research_titles=_merge(_RESEARCH_TITLE, _tier("research", "titles")),
        social_hosts=_merge(_SOCIAL_HOSTS, _tier("social", "hosts")),
        social_titles=_merge(_SOCIAL_TITLE, _tier("social", "titles")),
        state_media_hosts=_merge(_STATE_MEDIA_HOSTS, _tier("state_media", "hosts")),
        state_media_titles=_merge(_STATE_MEDIA_TITLE, _tier("state_media", "titles")),
        overrides=_load_overrides(),
    )
    return _patterns_cache


def _load_overrides() -> dict[str, str]:
    """per-source 上書き (UI 管理) を読む。key=feed_url or title、value=tier。"""
    try:
        if not _OVERRIDES_PATH.exists():
            return {}
        loaded = yaml.safe_load(_OVERRIDES_PATH.read_text(encoding="utf-8"))
        raw = loaded.get("overrides") if isinstance(loaded, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v).strip().lower()
            for k, v in raw.items()
            if str(v).strip().lower() in ASSIGNABLE_TIERS
        }
    except (OSError, yaml.YAMLError) as e:
        _log.warning("source_reliability_overrides_load_failed", error=str(e))
        return {}


def invalidate_reliability_cache() -> None:
    """override 編集後に cache を破棄する (次回 classify で再読込)。"""
    global _patterns_cache
    _patterns_cache = None


def _write_overrides(overrides: dict[str, str]) -> None:
    """overrides を atomic write (tempfile → os.replace)。機械管理ファイル。"""
    payload = {
        "_comment": "UI (SubscriptionsPage) 管理・手編集しない。pattern=source_reliability.yaml",
        "overrides": dict(sorted(overrides.items())),
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_OVERRIDES_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _OVERRIDES_PATH)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_source_reliability_override(key: str, tier: str | None) -> str:
    """per-source 上書きを設定/解除する (UI 専用)。

    tier が ASSIGNABLE_TIERS なら上書き、None/空/"auto" なら解除 (pattern default に戻す)。
    戻り値は適用後の実効状態 ("auto" = 上書きなし)。atomic write + cache invalidate。
    """
    key = key.strip()
    if not key:
        raise ValueError("source key (feed_url or title) が空です")
    norm = (tier or "").strip().lower()
    current = _load_overrides()
    result: str
    if norm in ASSIGNABLE_TIERS:
        current[key] = norm
        result = norm
    else:
        current.pop(key, None)
        result = "auto"
    _write_overrides(current)
    invalidate_reliability_cache()
    return result


# tier 序列 (高 → 低)
_TIER_RANK: dict[SourceTier, int] = {
    "official": 4,
    "research": 3,
    "news": 2,
    "social": 1,
    # 国営/影響工作は framing 不信頼ゆえ低 rank (best_tier=最権威 source に昇格させない)。
    "state_media": 1,
    "unknown": 0,
}

_TIER_LABEL: dict[SourceTier, str] = {
    "official": "一次",
    "research": "研究",
    "news": "ニュース",
    "social": "SNS",
    "state_media": "国営",
    "unknown": "不明",
}


def classify_source_tier(feed_title: str, feed_url: str) -> SourceTier:
    """source の信頼度ティアを feed_title / feed_url から決定的に分類 (briefing 評価層)。

    判定順: UI 上書き (override) → host suffix → title keyword → news(default)。
    """
    p = load_reliability_patterns()
    # UI 上書きを最優先 (SubscriptionsPage から編集、feed_url or title で照合)
    override = p.overrides.get(feed_url) or p.overrides.get(feed_title)
    if override in ASSIGNABLE_TIERS:
        return override  # type: ignore[return-value]

    title = feed_title.lower()
    try:
        host = (urlparse(feed_url).hostname or "").lower()
    except ValueError:
        host = ""
    if _title_match(title, p.social_titles) or _host_match(host, p.social_hosts):
        return "social"
    if _title_match(title, p.official_titles) or _host_match(host, p.official_hosts):
        return "official"
    if _title_match(title, p.research_titles) or _host_match(host, p.research_hosts):
        return "research"
    # 国営/影響工作 (news default の手前で判定。事実は報じうるが framing を割引対象に)。
    if _title_match(title, p.state_media_titles) or _host_match(host, p.state_media_hosts):
        return "state_media"
    if title.strip() or feed_url.strip():
        return "news"
    return "unknown"


@dataclass(frozen=True)
class SourceBasis:
    """1 つの key 主張の出典基盤 (読者向け信頼度表示)。"""

    source_count: int  # 裏付ける distinct source 数 (corroboration)
    best_tier: SourceTier  # 最も権威ある source のティア
    has_official_authority: bool  # 一次 source or KEV 登録 CVE
    kev_cves: tuple[str, ...] = field(default_factory=tuple)  # KEV 該当 CVE
    social_only: bool = False  # 全 source が SNS/未裏取り
    confidence: Confidence = "medium"
    reason: str = ""


def _derive_confidence(
    *, best_tier: SourceTier, source_count: int, has_official: bool, social_only: bool
) -> Confidence:
    if has_official or (best_tier in ("official", "research") and source_count >= 2):
        return "high"
    if social_only and source_count <= 1:
        return "low"
    return "medium"


def _build_reason(
    *, source_count: int, best_tier: SourceTier, kev: bool, social_only: bool, corrob_added: int = 0
) -> str:
    parts: list[str] = []
    if source_count:
        parts.append(f"{_TIER_LABEL[best_tier]}を含む{source_count}source")
    if corrob_added:
        parts.append(f"うち{corrob_added}は同一CVEの別報 (裏取りあり)")
    if kev:
        parts.append("CISA KEV 登録")
    if social_only and source_count <= 1:
        parts.append("SNS 単一・要裏取り")
    return " / ".join(parts) if parts else "出典なし"


# 裏取りとして数える source の最低ティア。ニュース/SNS の CVE 併記は wire 再掲が多く
# 「同一脆弱性を独立に検証した」とは言えないため、一次/研究層のみを裏取りに数える
# (これを緩めると、人気 CVE を併記しただけの dubious 主張が誤って確度上昇する実害がある)。
_CORROBORATION_MIN_TIER: tuple[SourceTier, ...] = ("official", "research")


def _corroborating_sources(
    repo: RunHistoryRepository,
    key_entities: list[tuple[str, str]],
    *,
    since_iso: str,
    exclude_ids: set[str],
) -> dict[str, SourceTier]:
    """S4: 共有 CVE を持つ **一次/研究層の** 別 article の source を集める (cross-source 検証)。

    主張自身の article_ids (exclude_ids) は除外し、権威ある別 source の裏取りのみを数える。
    ニュース/SNS の併記は裏取りに数えない (wire 再掲による誤確度上昇を防ぐ)。
    """
    corrob_ids: set[str] = set()
    seen_entities: set[tuple[str, str]] = set()
    for etype, val in key_entities:
        if (etype, val) in seen_entities:
            continue
        seen_entities.add((etype, val))
        if len(seen_entities) > _MAX_CORROBORATION_ENTITIES:
            break
        for aid in repo.find_articles_by_entity(
            etype, val, since_iso=since_iso, limit=_CORROBORATION_FETCH_LIMIT
        ):
            if aid not in exclude_ids:
                corrob_ids.add(aid)
    if not corrob_ids:
        return {}
    out: dict[str, SourceTier] = {}
    for rec in repo.get_articles_by_ids(list(corrob_ids)).values():
        skey = rec.feed_url or rec.feed_title or ""
        tier = classify_source_tier(rec.feed_title or "", rec.feed_url or "")
        if not skey or tier not in _CORROBORATION_MIN_TIER:
            continue  # 一次/研究層のみを裏取りに数える
        if skey not in out or _TIER_RANK[tier] > _TIER_RANK[out[skey]]:
            out[skey] = tier
    return out


def compute_source_basis(
    repo: RunHistoryRepository,
    article_ids: list[str],
    *,
    kev_set: frozenset[str] | None = None,
    corroborate_window_days: int = 0,
) -> SourceBasis:
    """article_ids 群から出典基盤を決定的に算出する。

    kev_set 未指定時は cache から KEV を読む (hot-path 安全、fetch しない)。
    corroborate_window_days > 0 のとき、S4 cross-source 検証として共有 entity
    (CVE) を持つ別 source を逆引きし、裏取り source として source_count に合算する。
    """
    ids = list(dict.fromkeys([a for a in article_ids if a]))
    if not ids:
        return SourceBasis(0, "unknown", False, confidence="low", reason="出典なし")

    arts = repo.get_articles_by_ids(ids)
    sources: dict[str, SourceTier] = {}  # source key -> tier
    cves: set[str] = set()
    key_entities: list[tuple[str, str]] = []  # S4: corroboration 逆引き用 (cve のみ)
    for aid, rec in arts.items():
        skey = rec.feed_url or rec.feed_title or aid
        tier = classify_source_tier(rec.feed_title or "", rec.feed_url or "")
        # 同一 source の tier は最高位を保持
        if skey not in sources or _TIER_RANK[tier] > _TIER_RANK[sources[skey]]:
            sources[skey] = tier
        for etype, val in repo.get_entities_by_article(aid):
            if etype == "cve" and val:
                cves.add(val.upper())
            if etype in _CORROBORATION_ENTITY_TYPES and val:
                key_entities.append((etype, val))

    direct_count = len(sources)
    # S4: 共有 CVE を持つ別 source を裏取りとして合算 (LLM でなく逆引きで機械的に検証)。
    # ただし「記事の entity」≠「この主張」の誤帰属を防ぐため、以下の場合は corroboration しない:
    #  (1) primary が social-only (Grok/X 等の多トピック束ね記事は entity を特定主張に帰属不可)
    #  (2) 単一 CVE でない (複数 CVE 併記 = roundup/bundle で、どの CVE が主張か曖昧)
    distinct_cves = {v for _, v in key_entities}
    direct_social_only = bool(sources) and set(sources.values()) <= {"social", "unknown"}
    can_corroborate = (
        corroborate_window_days > 0 and len(distinct_cves) == 1 and not direct_social_only
    )
    if can_corroborate:
        since_iso = (datetime.now(UTC) - timedelta(days=corroborate_window_days)).isoformat()
        for skey, tier in _corroborating_sources(
            repo, key_entities, since_iso=since_iso, exclude_ids=set(ids)
        ).items():
            if skey not in sources or _TIER_RANK[tier] > _TIER_RANK[sources[skey]]:
                sources[skey] = tier
    corrob_added = len(sources) - direct_count

    if kev_set is None:
        from src.tools.kev_client import get_kev_cve_set

        kev_set = get_kev_cve_set()
    kev_hit = tuple(sorted(cves & kev_set))

    tiers = set(sources.values())
    best_tier: SourceTier = max(tiers, key=lambda t: _TIER_RANK[t]) if tiers else "unknown"
    has_official = ("official" in tiers) or bool(kev_hit)
    social_only = bool(tiers) and tiers <= {"social", "unknown"}
    source_count = len(sources)
    confidence = _derive_confidence(
        best_tier=best_tier,
        source_count=source_count,
        has_official=has_official,
        social_only=social_only,
    )
    reason = _build_reason(
        source_count=source_count,
        best_tier=best_tier,
        kev=bool(kev_hit),
        social_only=social_only,
        corrob_added=corrob_added,
    )
    return SourceBasis(
        source_count=source_count,
        best_tier=best_tier,
        has_official_authority=has_official,
        kev_cves=kev_hit,
        social_only=social_only,
        confidence=confidence,
        reason=reason,
    )
