"""Grok レポート (JSONL) → BriefingMessage 変換ヘルパ (src.main から分割)。"""

from __future__ import annotations

import re

import jinja2

from src.logging_config import get_logger
from src.pipeline.briefing import _summarize_and_build
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)


def _grok_subarticle_id(parent_article_id: str, msg: BriefingMessage, index: int) -> str:
    """Grok レポートを tweet 単位に展開する際の **per-tweet 一意 article_id**。

    2026-06-17 修正: 親 (レポート) の article_id を全 tweet で共有していたため、
    (1) ``update_article_body`` が ``WHERE article_id=?`` で **同 id の兄弟行を全て上書き**
    し最後の tweet の body が全行に残る、(2) ``article_entities`` (victim_org / IoC / actor)
    が同一 id に混載される、というデータ破損が起きていた。tweet_id (なければ index) を
    付与して **1 tweet = 1 article_id** にする。tweet_id は安定なので再処理でも同一 id。
    """
    tid = str(msg.metadata.get("tweet_id") or "").strip()
    suffix = tid if tid else f"i{index}"
    return f"{parent_article_id}#{suffix}"


def _is_grok_article(article: Article) -> bool:
    """Article ID プレフィックス ("grok:") で Grok 経路を判定する。"""
    return article.id.startswith("grok:")


def _strip_html_keep_newlines(raw: str) -> str:
    """HTML タグを除去 (改行は保持)。Grok の DOM 抽出本文に使う。"""
    if "<" in raw and ">" in raw:
        return re.sub(r"<[^>]+>", "", raw)
    return raw


def grok_report_is_quiet(article: Article) -> bool:
    """レポートがハートビートのみ (事象ゼロの静穏) か。

    orchestrator が「静穏 (記事 row 不生成)」と「本当の抽出失敗 (extract_failed)」を
    判別するために使う。本文の導出は本 module の展開経路 (``summary_html`` →
    ``_strip_html_keep_newlines``) と**同一**にする — 2026-08-15 に orchestrator 側が
    ``body_text`` (grok 記事では常に空) を見て判定が発火しない不具合があった。
    """
    from src.grok.jsonl_parser import parse_jsonl

    raw = _strip_html_keep_newlines(article.summary_html)
    if not raw.strip():
        return False
    try:
        result = parse_jsonl(raw)
    except Exception:  # noqa: BLE001 — 判定不能は「静穏でない」side に倒す (失敗扱い維持)
        return False
    return result.heartbeat_count > 0


async def _grok_article_to_briefings(
    article: Article,
    *,
    enrichment: object | None = None,  # LlmEnrichment, untyped to keep call sites flexible
    llm: LLMClient | None = None,  # full enrichment 用 (summarizer LLM)
    template: jinja2.Template | None = None,  # briefing/summarizer.j2
    brief_count_24h: int = 0,
) -> list[BriefingMessage]:
    """Grok レポート (JSONL output) を tweet 単位の BriefingMessage に展開する。

    Phase Diamond Grok 再設計 (slot 1 X Native Signal / slot 2 JP East Asia) で
    Grok task の output は JSONL に統一済。record の matched_theme で routing を
    決め、以降は通常記事と同様に enrichment / 配信される。

    旧 markdown レポート経路 (parser + 分類器 + section 投稿 + grok_daily 集約) は
    数週間の検証を経て 2026-06-13 に完全撤去。JSONL でないメールは取り込まない
    (warning を出して skip。メールは未読のまま残り、lookback 窓を抜けると対象外になる)。
    """
    raw = _strip_html_keep_newlines(article.summary_html)
    if not raw.strip():
        return []

    from src.grok.jsonl_parser import is_jsonl_output

    if not is_jsonl_output(raw):
        _log.warning(
            "grok_non_jsonl_email_skipped",
            article_id=article.id,
            sender=article.author,
            title=(article.title or "")[:80],
        )
        return []

    return await _grok_jsonl_to_briefings(
        raw,
        article,
        llm=llm,
        template=template,
        enrichment=enrichment,
        brief_count_24h=brief_count_24h,
    )


def _merge_grok_overlay(
    enriched: BriefingMessage,
    grok_msg: BriefingMessage,
) -> BriefingMessage:
    """full-enrichment した briefing に Grok の source/dedup/theme signal を overlay する。

    enriched 側の内容 (title_ja / summary / importance / category / PMESII / Victim /
    Diamond 2 軸 / IOC / editorial_stance) を保持しつつ、Grok 機械変換版 ``grok_msg`` が
    持つ tweet 由来信号 (dedup_key / engagement / matched_theme / tweet_id) と sources
    (tweet permalink) を overlay する。

    B1 (2026-06-16): **channel routing は content engine (enriched 側 = route() の決定) を
    採用し、Grok の theme→channel で上書きしない**。「DPRK/中国/teaser だから alert」と
    いう source 依存の粗いルーティングを廃し、KEV/0day/active-exploitation/日本標的/深刻度
    等の content シグナルで他ソースと同一基準で振り分ける (完全 content-based・source 非依存)。
    matched_theme は分類ラベル/badge 用に metadata に残すが routing 権威ではない。
    """
    # target_channel / routing_reason / routing_rule_id は enriched (content engine) を優先。
    routing_keys = ("target_channel", "routing_reason", "routing_rule_id")
    grok_meta = {k: v for k, v in grok_msg.metadata.items() if k not in routing_keys}
    merged_meta = {**enriched.metadata, **grok_meta}
    return enriched.model_copy(
        update={"sources": grok_msg.sources, "metadata": merged_meta},
    )


async def _grok_jsonl_to_briefings(
    body: str,
    article: Article,
    *,
    llm: LLMClient | None = None,
    template: jinja2.Template | None = None,
    enrichment: object | None = None,
    brief_count_24h: int = 0,
) -> list[BriefingMessage]:
    """Grok JSONL output → 各 tweet を **他ソースと同等に full enrichment** した briefing 群へ。

    旧設計は「Grok は既に要約済」前提で機械変換のみ (翻訳/要約/分類なし) だった。
    しかし現 Grok は **生の X 投稿** を返すため、その前提が崩れている。よって本関数は
    各 tweet を Article 化し ``_summarize_and_build`` に通して、他ソースと同じ
    enrichment (日本語翻訳 / importance・category / PMESII / Victim / Diamond 2 軸 /
    IOC・actor 抽出 / editorial_stance) を付与する。

    一方で **routing は Grok の theme→channel を保持** する: matched_theme は Grok が
    分類したキュレーション信号であり、enriched briefing に ``target_channel`` (+ engagement /
    tweet_id ベース dedup_key / sources) を overlay して上書きする (LLM routing には委ねない)。

    llm / template が無い (テスト/設定欠落) 場合や 1 tweet の enrichment 失敗時は、
    従来の機械変換 (``tweet_to_briefing``) に graceful degradation する。
    """
    from src.grok.jsonl_parser import filter_records, parse_jsonl
    from src.grok.jsonl_to_briefings import records_to_briefings, tweet_to_briefing

    parse_result = parse_jsonl(body)
    _log.info(
        "grok_jsonl_parsed",
        article_id=article.id,
        total_lines=parse_result.total_lines,
        parsed_count=parse_result.parsed_count,
        skipped_count=len(parse_result.skipped_lines),
    )
    if not parse_result.records:
        _log.warning(
            "grok_jsonl_no_records",
            article_id=article.id,
            skipped_count=len(parse_result.skipped_lines),
        )
        return []

    filtered = filter_records(parse_result.records)

    # llm / template が無ければ従来の機械変換に degrade (テスト/設定欠落時)。
    if llm is None or template is None:
        briefings = records_to_briefings(filtered)
        _log.info(
            "grok_jsonl_briefings_generated",
            article_id=article.id,
            briefing_count=len(briefings),
            enriched=False,
        )
        return briefings

    out: list[BriefingMessage] = []
    enriched_n = 0
    for record in filtered:
        # 機械変換版: Grok の routing/sources/dedup の ground truth + enrichment 失敗時の fallback。
        grok_msg = tweet_to_briefing(record)
        if grok_msg is None:
            continue  # unknown theme は skip
        try:
            handle = (
                record.author_handle
                if record.author_handle.startswith("@")
                else f"@{record.author_handle}"
            )
            feed_title = f"{record.author_name} ({handle})" if record.author_name else handle
            tweet_article = Article(
                id=f"grok-x-{record.tweet_id}",
                title=(record.text[:120] or feed_title),
                url=record.url,
                summary_html=record.text,
                author=handle,
                published=record.posted_at_dt or article.published,
                feed_title=feed_title,
                feed_url=record.url,
            )
            # summarizer に渡す本文 (引用 / 外部 URL でコンテキスト補強 → IOC 抽出にも寄与)
            body_parts = [record.text]
            if record.quoted_text:
                body_parts.append(f"[引用] {record.quoted_text}")
            if record.external_urls:
                body_parts.append("参考 URL: " + " ".join(record.external_urls[:3]))
            tweet_body = "\n".join(p for p in body_parts if p)

            enriched = await _summarize_and_build(
                tweet_article,
                tweet_body,
                llm,
                template,
                think=False,
                enrichment=enrichment,
                brief_count_24h=brief_count_24h,
                body_source="grok",
            )
            out.append(_merge_grok_overlay(enriched, grok_msg))
            enriched_n += 1
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "grok_jsonl_enrichment_failed_fallback",
                tweet_id=record.tweet_id,
                error_type=type(e).__name__,
                error=str(e)[:200],
            )
            out.append(grok_msg)  # 機械変換版で graceful degradation

    _log.info(
        "grok_jsonl_briefings_generated",
        article_id=article.id,
        briefing_count=len(out),
        enriched=enriched_n,
    )
    return out
