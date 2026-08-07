"""切り株記事の全文再取得 + 再エンリッチ (2026-07-27, A4)。

docs/body_extraction_and_entity_integrity_redesign.md §2.4。

全文取得に失敗して feed 抜粋 (切り株) で保存された記事を、UA 修正後に再取得して全文化し、
**Discord 再投稿せず** に web 側 record を silent に再エンリッチする:
  - body 差し替え + body_source 更新 (切り株 → 全文)
  - 全 entity を **replace** (delete → 再抽出、切り株由来の痩せた/誤 entity を残さない)
  - 主題判定・分析列を再導出 (要約の幻覚是正含む)
  - body_ja 無効化 (再翻訳キューへ)

alert は既に発火済みのため再通知しない (「警告=Discord / 状況認識=web」原則)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.cti.actor_normalizer import ActorAliasRegistry
from src.logging_config import get_logger
from src.pipeline.body_limits import MAX_STORED_BODY_CHARS
from src.pipeline.briefing import _summarize_and_build, body_source_for_extraction
from src.pipeline.persistence import _persist_article_entities
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.content_extractor import ContentExtractor
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 再取得時に replace する entity type (本文/要約から再導出できるもの)。
# 主題 (subject_actor_ids 列) と victim (articles 列) は別経路のため含めない。
_REPLACE_ENTITY_TYPES: list[str] = [
    "actor",
    "actor_provisional",
    "cve",
    "ttp",
    "malware_family",
    # malware_family から persistence が導出付与するため、replace 対象に含めないと
    # 再取得で family が訂正されても旧 malware_type が add-only で残り二重登録になる (B5)。
    "malware_type",
    "tool",
    "affected_vendor",
    "affected_product",
    "victim_org",
    "victim_city",
    "campaign",
    "mentioned_country",
    "involved_country",
    "pir",
    "ioc_ip",
    "ioc_domain",
    "ioc_sha256",
    "ioc_sha1",
    "ioc_md5",
    "ioc_url",
]


async def reprocess_article_body(
    repo: RunHistoryRepository,
    article_id: str,
    url: str,
    *,
    extractor: ContentExtractor,
    llm: LLMClient,
    template: object,
    enrichment: object | None = None,
) -> str:
    """1 記事を全文再取得 → 再エンリッチ。戻り値 = 結果コード。

    'refetched_full' = 全文化して再エンリッチ成功 / 'still_stump' = 再取得も失敗 (切り株のまま) /
    'skipped' = 対象行が無い/本文不十分。**Discord 投稿・routing・dedup は一切触らない。**
    """
    row = repo.get_article(article_id)
    if row is None:
        return "skipped"

    extraction = await extractor.extract(url)
    if not (extraction.success and (extraction.text or "").strip()):
        # 再取得も失敗 → body は触らず、試行回数 +1 で記録する (B3b)。上限 (max_attempts) に
        # 達すると list_articles_needing_refetch から除外され、恒久失敗の無限リトライを止める。
        repo.record_refetch_failure(article_id, reason=extraction.failure_reason)
        _log.info(
            "reprocess_refetch_failed",
            article_id=article_id,
            reason=extraction.failure_reason,
        )
        return "still_stump"

    # 保存 ⊇ 検出の保証: briefing と同じ保存上限で切ってから再エンリッチする
    # (2026-08-06 に 20k → 100k、body_limits.py 参照)。
    body = (extraction.text or "")[:MAX_STORED_BODY_CHARS]
    source = body_source_for_extraction(extraction)

    # 全文本文で再エンリッチ (Article を DB 行から復元し body_text に全文を注入 = 再 fetch しない)。
    article = Article(
        id=article_id,
        title=row.title,
        url=url,
        summary_html="",
        published=row.published_at or row.created_at or datetime.now(UTC),
        feed_title=row.feed_title or "",
        feed_url=row.feed_url or "",
        body_text=body,
    )
    import jinja2

    if not isinstance(template, jinja2.Template):
        raise TypeError("template must be a jinja2.Template")
    msg = await _summarize_and_build(
        article,
        body,
        llm,
        template,
        enrichment=enrichment,
        body_source=source,
    )

    # 1) body 差し替え (source=全文) — seam 経由で body_source も確定。
    repo.update_article_body(article_id, body, source=source)
    # 2) body_ja 無効化 (再翻訳キューへ)。
    repo.clear_article_body_ja(article_id)

    # 3) 主題判定を再導出 (全文 + 新 named_primary_actor)。
    registry = _load_registry()
    rf = msg.metadata.get("routing_flags")
    rf = rf if isinstance(rf, dict) else {}
    detected = msg.metadata.get("detected_actor_ids")
    detected_ids = [str(x) for x in detected] if isinstance(detected, list) else []
    subject_ids_csv = ""
    subject_source = None
    subject_conf = None
    try:
        from src.cti.subject_actor import determine_subject_actors

        subj = determine_subject_actors(
            titles=(row.title or "", str(msg.metadata.get("original_title") or "")),
            detected_actor_ids=detected_ids,
            llm_primary_actor_id=str(rf.get("primary_actor_id") or ""),
            llm_confidence=str(rf.get("confidence") or "low"),
            category=row.category,
            registry=registry,
        )
        subject_ids_csv = ",".join(subj.ids)
        subject_source = subj.source
        subject_conf = subj.confidence
        repo.update_subject_actor_fields(
            article_id, ids_csv=subject_ids_csv, source=subject_source, confidence=subject_conf
        )
    except Exception as e:  # noqa: BLE001 — 主題判定失敗で再処理を止めない
        _log.warning("reprocess_subject_failed", article_id=article_id, error=str(e))

    # 4) entity を replace (delete → 再抽出)。切り株由来の痩せた/誤 entity を残さない。
    repo.delete_article_entities(article_id, _REPLACE_ENTITY_TYPES)
    _persist_article_entities(
        dedup_repo=repo,
        article_id=article_id,
        msg=msg,
        feed_title=row.feed_title or "",
        subject_actor_ids=subject_ids_csv,
        subject_actor_source=subject_source,
    )

    # 5) 分析列を上書き (要約の幻覚是正含む)。allowlist 検証は repo 側。
    repo.update_article_enrichment(article_id, _enrichment_fields(msg, rf))

    _log.info("reprocess_refetched_full", article_id=article_id, body_len=len(body), source=source)
    return "refetched_full"


def _load_registry() -> ActorAliasRegistry:
    from src.cti.actor_normalizer import load_actor_aliases

    return load_actor_aliases()


def _enrichment_fields(msg: object, rf: dict[str, object]) -> dict[str, object]:
    """BriefingMessage の metadata から分析列の更新値を組む (allowlist は repo 側で検証)。"""
    meta = getattr(msg, "metadata", {}) or {}
    axes = meta.get("pmesii_axes")
    axes_set = {a for a in axes if isinstance(a, str)} if isinstance(axes, list) else set()

    def _s(key: str) -> object:
        v = meta.get(key)
        return v if isinstance(v, str) and v else None

    fields: dict[str, object] = {
        "summary": getattr(msg, "summary", None) or None,
        "editorial_stance": (rf.get("editorial_stance") if isinstance(rf, dict) else None) or None,
        "socio_political_intent": _s("socio_political_intent"),
        "intent_confidence": _s("intent_confidence"),
        "socio_political_rationale": _s("socio_political_rationale"),
        "technical_axis_summary": _s("technical_axis_summary"),
        "remediation": _s("remediation"),
        "event_date": _s("event_date"),
        "event_date_basis": _s("event_date_basis"),
        "compromise_date": _s("compromise_date"),
        "article_type": _s("article_type"),
        "is_ransomware": 1 if meta.get("is_ransomware") else 0,
        "llm_primary_actor_raw": str(rf.get("named_primary_actor") or "").strip() or None,
        "llm_primary_confidence": str(rf.get("confidence") or "").strip() or None,
        "victim_sector_canonical": _s("victim_sector_canonical"),
        "victim_sector_raw": _s("victim_sector_raw"),
        "victim_country_iso": _s("victim_country_iso"),
        "victim_country_raw": _s("victim_country_raw"),
        "victim_country_scope": _s("victim_country_scope"),
        "pmesii_p": 1 if "P" in axes_set else 0,
        "pmesii_m": 1 if "M" in axes_set else 0,
        "pmesii_e": 1 if "E" in axes_set else 0,
        "pmesii_s": 1 if "S" in axes_set else 0,
        "pmesii_i_infra": 1 if "I-infra" in axes_set else 0,
        "pmesii_i_cyber": 1 if "I-cyber" in axes_set else 0,
        "pmesii_p_env": 1 if "P-env" in axes_set else 0,
        "pmesii_t": 1 if "T" in axes_set else 0,
    }
    return fields
