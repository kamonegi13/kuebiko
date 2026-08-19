"""投稿直前 dedup ゲート (2026-08-19 統合)。

``run_pipeline`` (``src/pipeline/orchestrator.py``) の投稿ループが記事ごとに順に
呼んでいた 4 層の dedup チェックを 1 つのゲート関数 ``check_pre_post_dedup`` に集約する:

1. dedup_key 完全一致 (in-run + 48h 履歴、Phase 5L-4) — ``check_dedup_key_duplicate``
2. CVE-ID 正規化 (in-run + 48h 履歴、Phase 5T-V-2) — ``check_cve_duplicate``
3. content 署名 Jaccard (24h/72h、Phase B) — ``check_content_duplicate``
4. victim_org 完全一致 (24h、Step 3 2026-08-19) — ``check_victim_org_duplicate``

呼出順序・失敗理由文言・ログイベント名は統合前 (orchestrator.py) と同一に保つ
(1-3 は機械的な抽出であり挙動不変、4 のみ新規)。各層のアルゴリズム自体は元の実装
(dedup_key/CVE 正規化はここに移設、content は ``src.cti.content_dedup``、victim_org は
``src.cti.entity_dedup``) のまま変更していない — 混ぜない。

層 1-3 (URL 既読 / embedding、``src/pipeline/filters.py``) はここに含まれない — 生
Article 段階・LLM 呼出前の早期 skip であり、本ゲート (LLM 生成後・投稿直前) とは別の
関心事 (LLM コスト回避) を持つため意図的に対象外。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.cti.content_dedup import find_recent_content_duplicate
from src.cti.dedup_key import extract_cve_id
from src.cti.entity_dedup import find_recent_victim_org_duplicate, victim_org_dedup_enabled
from src.cti.victim_org_filter import PROTECTED_CATEGORIES
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

# Step 3: victim_org × 日付の dedup 窓。ransomware_ingest の ±60日窓 (続報許容の設計)
# とは別 — ニュースの続報 (24h 超) は正当な情報のため、24h 以内のみ強制 skip する
# (memory [[dedup_window_48h_design_intent]] / layer4-5 の 48h 窓と同じ思想)。
_VICTIM_ORG_DEDUP_WITHIN_HOURS = 24


@dataclass(frozen=True)
class DedupGateResult:
    """gate が skip を判定したときの結果 (失敗理由のみ、ログは各層内で完結)。"""

    failure_reason: str


def check_dedup_key_duplicate(
    *,
    msg: BriefingMessage,
    art_id: str,
    channel: str,
    dedup_repo: RunHistoryRepository | None,
    cross_channel_seen_keys: set[str],
) -> DedupGateResult | None:
    """Phase 5L-4: dedup_key 完全一致 (in-run + 48h 履歴) の ch 横断 dedup。

    ``src/pipeline/orchestrator.py`` から機械的に抽出 (Step 1, 挙動不変)。skip 不要
    なら ``cross_channel_seen_keys`` に ``dedup_key`` を追加する副作用を持つ (元実装の
    まま — 同一 run 内の後続記事が同 key を検出できるようにするため)。
    """
    msg_dedup_key = msg.metadata.get("dedup_key")
    if not (isinstance(msg_dedup_key, str) and msg_dedup_key):
        return None
    if msg_dedup_key in cross_channel_seen_keys:
        _log.info(
            "cross_channel_dedup_skipped",
            article_id=art_id,
            requested_channel=channel,
            dedup_key=msg_dedup_key,
            reason="same_run_higher_priority_already_posted",
        )
        return DedupGateResult(f"cross-ch dedup: same key in this run ({msg_dedup_key})")
    if dedup_repo is not None:
        prior = dedup_repo.find_recent_article_by_dedup_key(msg_dedup_key, within_hours=48)
        if prior is not None:
            _log.info(
                "cross_channel_dedup_skipped",
                article_id=art_id,
                requested_channel=channel,
                dedup_key=msg_dedup_key,
                reason="prior_post_within_48h",
                prior_channel=prior.posted_channel,
            )
            return DedupGateResult(f"cross-ch dedup: prior post 48h ({prior.posted_channel})")
    cross_channel_seen_keys.add(msg_dedup_key)
    return None


def check_cve_duplicate(
    *,
    msg: BriefingMessage,
    art_id: str,
    channel: str,
    dedup_repo: RunHistoryRepository | None,
    cross_channel_seen_cves: set[str],
) -> DedupGateResult | None:
    """Phase 5T-V-2: 正規化 CVE-ID による 48h 以内の強制 skip。

    ``src/pipeline/orchestrator.py`` から機械的に抽出 (Step 1, 挙動不変)。同 CVE で
    別 dedup_key になる LLM 出力のばらつきを吸収する。48h 超は続報として許容
    (memory [[dedup_window_48h_design_intent]])。
    """
    if dedup_repo is None:
        return None
    msg_dedup_key = msg.metadata.get("dedup_key")
    cve_id = extract_cve_id(
        dedup_key=msg_dedup_key if isinstance(msg_dedup_key, str) else None,
        title=msg.title,
    )
    if cve_id and cve_id in cross_channel_seen_cves:
        _log.info(
            "cve_normalized_dedup_skipped",
            article_id=art_id,
            requested_channel=channel,
            cve_id=cve_id,
            reason="same_cve_in_this_run",
        )
        return DedupGateResult(f"cve dedup: same CVE in this run ({cve_id})")
    if cve_id:
        prior_cve = dedup_repo.find_recent_post_by_cve(cve_id, within_hours=48)
        if prior_cve is not None:
            _log.info(
                "cve_normalized_dedup_skipped",
                article_id=art_id,
                requested_channel=channel,
                cve_id=cve_id,
                reason="prior_post_within_48h",
                prior_channel=prior_cve.posted_channel,
                prior_dedup_key=prior_cve.dedup_key,
            )
            return DedupGateResult(
                f"cve dedup: prior post 48h ({prior_cve.posted_channel}, key={prior_cve.dedup_key})"
            )
        cross_channel_seen_cves.add(cve_id)
    return None


def check_content_duplicate(
    *,
    msg: BriefingMessage,
    art_id: str,
    channel: str,
    dedup_repo: RunHistoryRepository | None,
    article: Article | None,
) -> DedupGateResult | None:
    """Phase B-content-dedup: cross-source 同 advisory dedup (title signature Jaccard)。

    ``src/pipeline/orchestrator.py`` から呼出制御 (skip 判定 / ログ) のみを集約
    (Step 2)。アルゴリズム自体は ``src.cti.content_dedup.find_recent_content_duplicate``
    のまま変更していない。
    """
    if dedup_repo is None:
        return None
    try:
        content_dup = find_recent_content_duplicate(
            repo=dedup_repo,
            title=msg.title or "",
            summary=msg.summary or "",
            # URL 中の advisory id (JVNVU 等) が唯一の決定論的同一性キーになるケース
            # (JVN 日英別タイトル) があるため URL も渡す
            url=(article.url if article is not None else ""),
            candidate_article_id=art_id,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "content_dedup_lookup_failed",
            article_id=art_id,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    if content_dup is None:
        return None
    _log.info(
        "content_dedup_skipped",
        article_id=art_id,
        requested_channel=channel,
        prior_article_id=content_dup.article_id,
        prior_feed=content_dup.feed_title,
    )
    return DedupGateResult(
        f"content_dedup: cross-source match "
        f"(prior={content_dup.feed_title}, key={content_dup.dedup_key})"
    )


def check_victim_org_duplicate(
    *,
    msg: BriefingMessage,
    art_id: str,
    channel: str,
    dedup_repo: RunHistoryRepository | None,
) -> DedupGateResult | None:
    """Step 3 (2026-08-19): victim_org × 24h の双方向 dedup。

    ``src/sources/ransomware_ingest.py`` の「ニュースが同一 victim_org を扱っていれば
    ransomware.live 側を重複扱いする」一方向チェックの逆方向を埋める — RSS/Grok の
    breach/incident 記事から「同一 victim_org が直近で既に posted 済みか」(ransomware.live
    含む全 source) を能動的に確認する。

    対象は category が breach/incident の記事のみ (``src.cti.victim_org_filter.
    PROTECTED_CATEGORIES`` — victim_org のベンダ誤混入対策で使う「実被害カテゴリ」の
    既存境界をそのまま再利用し、複製定義を避ける)。窓は 24h 限定 (ransomware_ingest
    の ±60日窓とは別物) — 24h 超の続報は正当な情報のため殺さない。victim_org は完全
    一致 (lower/trim) のみで判定し、部分一致・fuzzy はしない (誤爆で別組織の事案を
    潰す方が害が大きい)。``VICTIM_ORG_DEDUP=0`` で無効化できる (既定 ON)。
    """
    if dedup_repo is None or not victim_org_dedup_enabled():
        return None
    if msg.category not in PROTECTED_CATEGORIES:
        return None
    victim_orgs = msg.metadata.get("victim_orgs") or []
    if not isinstance(victim_orgs, list):
        return None
    try:
        match = find_recent_victim_org_duplicate(
            dedup_repo,
            victim_orgs,
            within_hours=_VICTIM_ORG_DEDUP_WITHIN_HOURS,
            exclude_article_id=art_id,
        )
    except Exception as e:  # noqa: BLE001 — DB 障害等でも投稿ループ自体は継続する
        _log.warning(
            "victim_org_dedup_lookup_failed",
            article_id=art_id,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    if match is None:
        return None
    matched_org, prior = match
    _log.info(
        "victim_org_dedup_skipped",
        article_id=art_id,
        requested_channel=channel,
        victim_org=matched_org,
        prior_article_id=prior.article_id,
        prior_channel=prior.posted_channel,
    )
    return DedupGateResult(
        f"victim_org dedup: prior post {_VICTIM_ORG_DEDUP_WITHIN_HOURS}h "
        f"(org={matched_org}, article={prior.article_id})"
    )


def check_pre_post_dedup(
    *,
    msg: BriefingMessage,
    art_id: str,
    channel: str,
    dedup_repo: RunHistoryRepository | None,
    article: Article | None,
    cross_channel_seen_keys: set[str],
    cross_channel_seen_cves: set[str],
) -> DedupGateResult | None:
    """投稿直前 dedup 4 層を順に評価する統合ゲート (Step 2)。

    最初に一致した層で即座に skip 判定を返す (short-circuit、統合前と同じ順序:
    dedup_key → CVE → content → victim_org)。呼び出し側 (orchestrator) は結果が
    非 None なら outcome を ``skipped_duplicate`` にし、``skipped_for_mark_read`` へ
    追加して continue する。
    """
    dedup_key_result = check_dedup_key_duplicate(
        msg=msg,
        art_id=art_id,
        channel=channel,
        dedup_repo=dedup_repo,
        cross_channel_seen_keys=cross_channel_seen_keys,
    )
    if dedup_key_result is not None:
        return dedup_key_result

    cve_result = check_cve_duplicate(
        msg=msg,
        art_id=art_id,
        channel=channel,
        dedup_repo=dedup_repo,
        cross_channel_seen_cves=cross_channel_seen_cves,
    )
    if cve_result is not None:
        return cve_result

    content_result = check_content_duplicate(
        msg=msg,
        art_id=art_id,
        channel=channel,
        dedup_repo=dedup_repo,
        article=article,
    )
    if content_result is not None:
        return content_result

    return check_victim_org_duplicate(
        msg=msg,
        art_id=art_id,
        channel=channel,
        dedup_repo=dedup_repo,
    )


__all__ = [
    "DedupGateResult",
    "check_content_duplicate",
    "check_cve_duplicate",
    "check_dedup_key_duplicate",
    "check_pre_post_dedup",
    "check_victim_org_duplicate",
]
