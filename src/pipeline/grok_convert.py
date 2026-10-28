"""Grok レポート (JSONL) → BriefingMessage 変換ヘルパ (src.main から分割)。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

import jinja2

from src.grok.jsonl_parser import TweetRecord
from src.grok.link_fetch import (
    LinkedPage,
    LinkFetchConfig,
    LinkFetcher,
    build_link_section,
    link_fetch_mode,
    linked_context_json,
)
from src.grok.link_relevance import apply_relevance_gate, link_relevance_gate_enabled
from src.logging_config import get_logger
from src.pipeline.briefing import _summarize_and_build
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage
from src.tools.llm_client import LLMClient
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)

#: run 横断の既知判定 (dedup_repo.filter_seen_and_touch と同じ形: hash 群 → 既知の hash 集合)
SeenFilter = Callable[[Sequence[str]], set[str]]


@dataclass(frozen=True)
class GrokExpansion:
    """Grok レポート 1 本の展開結果。

    ``deferred`` は時間切れ (soft deadline) で着手しなかった投稿の数。1 以上なら
    レポートは**未完了**で、呼び手は通知メールを既読にしてはいけない (次の run が
    同じレポートを取り直し、処理済みの投稿は run 横断の既知判定で要約前に落ちる)。
    """

    briefings: list[BriefingMessage]
    deferred: int = 0

    @property
    def is_complete(self) -> bool:
        return self.deferred == 0


_EMPTY_EXPANSION = GrokExpansion(briefings=[])


def _grok_subarticle_id(parent_article_id: str, msg: BriefingMessage, index: int) -> str:
    """Grok レポートを tweet 単位に展開する際の **per-tweet 一意 article_id**。

    2026-06-17 修正: 親 (レポート) の article_id を全 tweet で共有していたため、
    (1) ``update_article_body`` が ``WHERE article_id=?`` で **同 id の兄弟行を全て上書き**
    し最後の tweet の body が全行に残る、(2) ``article_entities`` (victim_org / IoC / actor)
    が同一 id に混載される、というデータ破損が起きていた。tweet_id (なければ index) を
    付与して **1 tweet = 1 article_id** にする。tweet_id は安定なので再処理でも同一 id。
    tweet_id が無ければ投稿の中身から決まる ``post_key`` (2026-10-29。index は取り直すたびに
    ずれる)、それも無い経路 (機械変換のみ) だけ index。
    """
    tid = str(msg.metadata.get("tweet_id") or "").strip()
    key = str(msg.metadata.get(POST_KEY_FIELD) or "").strip()
    suffix = tid or key or f"i{index}"
    return f"{parent_article_id}#{suffix}"


def _is_grok_article(article: Article) -> bool:
    """Article ID プレフィックス ("grok:") で Grok 経路を判定する。"""
    return article.id.startswith("grok:")


def _strip_html_keep_newlines(raw: str) -> str:
    """HTML タグを除去 (改行は保持)。Grok の DOM 抽出本文に使う。"""
    if "<" in raw and ">" in raw:
        return re.sub(r"<[^>]+>", "", raw)
    return raw


def filter_expanded_by_seen(
    expanded: list[BriefingMessage],
    seen_filter: Callable[[Sequence[str]], set[str]] | None,
) -> tuple[list[BriefingMessage], int]:
    """run 横断の tweet 重複を ledger 照会で弾く (2026-09-08 利用者発見)。

    親レポート URL は毎回新規のため fetch 段の未見選別 (seen_hash_filter) を素通りし、
    別レポートに含まれる同一 tweet が run を跨いで別 sub-article 化していた
    (30 日実測: X 投稿 191 URL 重複 / 余剰 209 記事。dedup_seen_urls は tweet URL を
    既読化済みなのに、展開経路に照会する関門が無かった)。展開直後に照会して落とす。
    dedup_key 無しの msg は落とさない (fail-open — 落とし過ぎは収集喪失)。
    """
    if seen_filter is None or not expanded:
        return expanded, 0
    keys: list[str] = []
    for msg in expanded:
        raw = str(msg.metadata.get("dedup_key") or "")
        if not raw and msg.sources:
            raw = msg.sources[0].url or ""
        keys.append(url_hash(raw) if raw else "")
    seen = seen_filter([k for k in keys if k])
    kept = [m for m, k in zip(expanded, keys, strict=True) if not k or k not in seen]
    return kept, len(expanded) - len(kept)


def drop_seen_records(
    records: Sequence[TweetRecord],
    seen_filter: SeenFilter | None,
) -> tuple[list[TweetRecord], int]:
    """**要約の前に**、run 横断で既知の投稿を落とす (2026-10-29)。

    投稿の URL (処理済みの投稿は投稿・web-only・重複のどの終端でも ``_register_seen`` 等で
    投稿 URL を既読に登録する) と dedup_key (外部 URL 優先、filter_expanded_by_seen と同じ鍵)
    のどちらかが既知なら落とす。巨大なレポートを複数の run に分けて処理するとき、
    前の run で済んだ投稿を要約し直さないための関門 (要約後の filter_expanded_by_seen は
    LLM を使い切った後なので、繰り越しの度に先頭から要約し直して進まなくなる)。
    """
    if seen_filter is None or not records:
        return list(records), 0
    from src.grok.jsonl_to_briefings import _make_dedup_key

    keys = [
        tuple(dict.fromkeys(url_hash(u) for u in (record_identity_url(r), _make_dedup_key(r)) if u))
        for r in records
    ]
    seen = seen_filter(list(dict.fromkeys(h for ks in keys for h in ks)))
    kept = [r for r, ks in zip(records, keys, strict=True) if not any(h in seen for h in ks)]
    return kept, len(records) - len(kept)


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


# X の permalink: https://x.com/<handle>/status/<id>
_X_PERMALINK = re.compile(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/status/\d+")
#: X の別名ホスト (twitter.com / mobile.x.com 等) を x.com に揃える
_X_HOST_ALIASES = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/", re.IGNORECASE)
#: 匿名の permalink (投稿者が URL から取れない形)
_ANON_HANDLE = "i"
#: 投稿の URL も tweet id も無いときの、投稿ごとの合成 URL (レポートの path とは重ならない)
_SYNTHETIC_POST_BASE = "https://grok.com/post/"
#: 投稿の安定した鍵を BriefingMessage.metadata で運ぶ欄
POST_KEY_FIELD = "post_key"
_POST_KEY_DIGEST_CHARS = 16


def record_post_key(record: TweetRecord) -> str:
    """投稿の安定した鍵: tweet id、無ければ投稿者と本文の hash (取り直しても同じ値)。"""
    tid = record.tweet_id.strip()
    if tid:
        return tid
    raw = f"{record.author_handle.strip()}\n{record.text.strip()}".encode()
    return "h" + hashlib.sha256(raw).hexdigest()[:_POST_KEY_DIGEST_CHARS]


def synthetic_post_url(post_key: str) -> str:
    """投稿の URL も tweet id も無いときの、投稿ごとの URL (既読の登録と照合で共用)。"""
    # 断片 (#) は URL の正規化で落ちて親と同じ hash になるため path に入れる
    return f"{_SYNTHETIC_POST_BASE}{quote(post_key, safe='')}"


def record_identity_url(record: TweetRecord) -> str:
    """要約前の照合に使う投稿の URL。登録側 (grok_subarticle_source) と同じ規則で作る。"""
    return canonical_post_url(record.url, record.tweet_id) or synthetic_post_url(
        record_post_key(record)
    )


def canonical_post_url(url: str, tweet_id: str = "") -> str:
    """投稿の識別子としての URL (既読の登録と照合の両方で使う、2026-10-29)。

    twitter.com / mobile.x.com は x.com に揃える。permalink にならなければ tweet id から
    ``https://x.com/i/status/<id>`` を作る。どちらも無ければ空 (投稿の識別子にならない URL を
    識別子に使わない — 親レポートの URL が入っていると親ごと既読になる)。
    """
    normalized = _X_HOST_ALIASES.sub("https://x.com/", url.strip())
    if _X_PERMALINK.match(normalized):
        return normalized
    tid = tweet_id.strip()
    if tid.isdigit():
        return f"https://x.com/{_ANON_HANDLE}/status/{tid}"
    return ""


def grok_subarticle_source(
    parent: Article,
    tweet_url: str,
    *,
    account_class: str = "",
    tweet_id: str = "",
    fallback_key: str = "",
) -> Article:
    """per-tweet の source identity を持つ Article を返す (永続化・既読化用)。

    **なぜ要るか (2026-08-24)**: Grok は 1 ツイート = sub-article に展開されるが、
    orchestrator の ``articles_by_id`` は sub-id → **親 Article** を指していた。
    その結果:

    - 既読化・埋込が親レポートの URL で行われ、ツイート URL は ``dedup_seen_urls`` に
      1 件も入らない (実測 x.com: articles 906 / dedup 0) → **意味的 dedup にも
      事象の群化にも一度も参加できない**
    - ``feed_title`` が全件 'Grok' / ``feed_url`` が全件 'https://grok.com/' となり、
      **何人が報じても独立 1 媒体**として数えられる (実測 102 アカウント)

    ここでは permalink から投稿者を取り、投稿者を媒体 (source key) にする。
    tier は ``classify_source_tier`` が x.com を 'social' に分類するため、
    「独立 3 媒体」がニュース媒体 3 社の裏取りと混同されることはない。

    ⚠ 2026-10-29: 親 Article を**そのまま返さない**。親の URL で既読に登録されると、
    繰り越し中のレポートが次の run の URL 重複判定で親ごと落ち、残りの投稿が消える。
    permalink にならない URL でも tweet id (無ければ ``fallback_key``) で投稿ごとの URL にする。
    """
    post_url = canonical_post_url(tweet_url, tweet_id)
    m = _X_PERMALINK.match(post_url)
    if not m or m.group(1) == _ANON_HANDLE:
        # 投稿者は URL から取れない: 媒体は親のまま、識別子 (URL) だけ投稿ごとにする
        return parent.model_copy(
            update={
                "url": post_url or synthetic_post_url(fallback_key),
                "account_class": account_class,
                "has_title": False,
            }
        )
    handle = m.group(1)
    return parent.model_copy(
        update={
            "url": post_url,
            # 媒体 = 投稿者。feed_url が source key の第一候補 (Source Identity Decoupling)
            "feed_url": f"https://x.com/{handle}",
            "feed_title": f"@{handle}",
            "account_class": account_class,
            # 親 (Grok レポート) は has_title=True だが、per-tweet は本文しか無い
            "has_title": False,
        }
    )


async def _grok_article_to_briefings(
    article: Article,
    *,
    enrichment: object | None = None,  # LlmEnrichment, untyped to keep call sites flexible
    llm: LLMClient | None = None,  # full enrichment 用 (summarizer LLM)
    template: jinja2.Template | None = None,  # briefing/summarizer.j2
    brief_count_24h: int = 0,
    should_stop: Callable[[], bool] | None = None,
    seen_filter: SeenFilter | None = None,
) -> GrokExpansion:
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
        return _EMPTY_EXPANSION

    from src.grok.jsonl_parser import is_jsonl_output

    if not is_jsonl_output(raw):
        _log.warning(
            "grok_non_jsonl_email_skipped",
            article_id=article.id,
            sender=article.author,
            title=(article.title or "")[:80],
        )
        return _EMPTY_EXPANSION

    return await _grok_jsonl_to_briefings(
        raw,
        article,
        llm=llm,
        template=template,
        enrichment=enrichment,
        brief_count_24h=brief_count_24h,
        should_stop=should_stop,
        seen_filter=seen_filter,
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
    should_stop: Callable[[], bool] | None = None,
    seen_filter: SeenFilter | None = None,
) -> GrokExpansion:
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

    ``should_stop`` (soft deadline) が真になったら**新しい投稿の要約に着手しない**
    (2026-10-29)。1 レポートの投稿数に上限は無く (実測 392 行)、1 投稿 ~15 秒の要約を
    全件回すと段の上限を超えて子が kill され、成果が全損していた (run 8732)。
    着手しなかった件数を ``deferred`` で返し、レポートを未完了として扱わせる。
    ``seen_filter`` は要約の前の run 横断の既知判定 (``drop_seen_records``)。
    """
    from src.grok.jsonl_parser import filter_records, parse_jsonl
    from src.grok.jsonl_to_briefings import records_to_briefings

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
        return _EMPTY_EXPANSION

    filtered, known = drop_seen_records(filter_records(parse_result.records), seen_filter)
    if known:
        _log.info("grok_records_known_before_summary", article_id=article.id, dropped=known)

    # llm / template が無ければ従来の機械変換に degrade (テスト/設定欠落時)。
    if llm is None or template is None:
        briefings = records_to_briefings(filtered)
        _log.info(
            "grok_jsonl_briefings_generated",
            article_id=article.id,
            briefing_count=len(briefings),
            enriched=False,
        )
        return GrokExpansion(briefings=briefings)

    out: list[BriefingMessage] = []
    enriched_n = 0
    link_mode = link_fetch_mode()
    fetcher = LinkFetcher(LinkFetchConfig.from_env()) if link_mode != "off" else None
    deferred = 0
    try:
        for done, record in enumerate(filtered):
            if should_stop is not None and should_stop():
                deferred = len(filtered) - done
                break
            message, is_enriched = await _grok_record_to_message(
                record,
                article,
                llm=llm,
                template=template,
                enrichment=enrichment,
                brief_count_24h=brief_count_24h,
                fetcher=fetcher,
                use_links=link_mode == "on",
            )
            if message is None:
                continue
            enriched_n += int(is_enriched)
            # 発信者種別は enrichment の成否に依らず確定しているので必ず載せる。
            # 投稿の安定した鍵も載せる (sub_id と既読の登録 URL を取り直しても同じにする)
            out.append(
                message.model_copy(
                    update={
                        "account_class": record.account_class,
                        "metadata": {
                            **message.metadata,
                            POST_KEY_FIELD: record_post_key(record),
                        },
                    }
                )
            )
    finally:
        if fetcher is not None:
            await fetcher.aclose()

    _log.info(
        "grok_jsonl_briefings_generated",
        article_id=article.id,
        briefing_count=len(out),
        enriched=enriched_n,
        deferred=deferred,
    )
    if deferred:
        _log.warning("grok_report_deferred_posts", article_id=article.id, deferred=deferred)
    return GrokExpansion(briefings=out, deferred=deferred)


async def _fetch_linked_pages(
    record: TweetRecord, fetcher: LinkFetcher | None
) -> tuple[LinkedPage, ...]:
    """投稿の外部リンクを取りに行く。失敗しても投稿の処理は止めない (空で返す)。"""
    if fetcher is None or not record.external_urls:
        return ()
    try:
        pages = await fetcher.fetch(record.external_urls)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "grok_link_fetch_failed", tweet_id=record.tweet_id, error_type=type(e).__name__
        )
        return ()
    if link_relevance_gate_enabled():
        # 主題に触れない参照先 (被害組織のトップページ等) は材料に入れない (本文は保存する)
        pages = apply_relevance_gate(f"{record.text}\n{record.quoted_text or ''}", pages)
    _log.info(
        "grok_link_fetch",
        tweet_id=record.tweet_id,
        attempted=len(pages),
        succeeded=sum(1 for p in pages if p.ok),
        off_topic=sum(1 for p in pages if p.skip_reason is not None),
        elapsed_ms=sum(p.elapsed_ms for p in pages),
        reasons=[p.reason for p in pages if not p.ok],
    )
    return pages


async def _grok_record_to_message(
    record: TweetRecord,
    article: Article,
    *,
    llm: LLMClient,
    template: jinja2.Template,
    enrichment: object | None,
    brief_count_24h: int,
    fetcher: LinkFetcher | None,
    use_links: bool,
) -> tuple[BriefingMessage | None, bool]:
    """1 tweet を full enrichment した BriefingMessage にする (失敗時は機械変換版)。"""
    from src.grok.jsonl_to_briefings import tweet_to_briefing

    # 機械変換版: Grok の routing/sources/dedup の ground truth + enrichment 失敗時の fallback。
    grok_msg = tweet_to_briefing(record)
    if grok_msg is None:
        return None, False  # unknown theme は skip
    # per-tweet の source identity は **enrichment の成否に関わらず** 必要なので
    # try の外で組み立てる (失敗しても媒体 = 投稿者・発信者種別は確定している)。
    handle = (
        record.author_handle if record.author_handle.startswith("@") else f"@{record.author_handle}"
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
        # 発信者種別を下流へ運ぶ (従来は収集フィルタの判定にだけ使って捨てていた)
        account_class=record.account_class,
        # X の投稿にタイトルは無い。``title`` は本文の先頭を入れているだけなので、
        # 見出しは生成に委ねる (そうしないと日本語投稿は本文が見出しになる)。
        has_title=False,
    )
    try:
        # summarizer に渡す本文 (引用 / 外部 URL でコンテキスト補強 → IOC 抽出にも寄与)
        body_parts = [record.text]
        if record.quoted_text:
            body_parts.append(f"[引用] {record.quoted_text}")
        if record.external_urls:
            body_parts.append("参考 URL: " + " ".join(record.external_urls[:3]))
        tweet_body = "\n".join(p for p in body_parts if p)
        # 参照先の本文: 取得結果は常に保存し、要約の材料に足すのは GROK_LINK_FETCH=on のときだけ
        pages = await _fetch_linked_pages(record, fetcher)

        enriched = await _summarize_and_build(
            tweet_article,
            tweet_body,
            llm,
            template,
            think=False,
            enrichment=enrichment,
            brief_count_24h=brief_count_24h,
            body_source="grok",
            link_text=build_link_section(pages) if use_links else "",
            linked_context=linked_context_json(pages),
        )
        return _merge_grok_overlay(enriched, grok_msg), True
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "grok_jsonl_enrichment_failed_fallback",
            tweet_id=record.tweet_id,
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        return grok_msg, False  # 機械変換版で graceful degradation
