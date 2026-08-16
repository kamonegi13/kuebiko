"""1 記事 → BriefingMessage 化のコア処理 (src.main から分割)。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import jinja2

from src.logging_config import get_logger
from src.pipeline.body_limits import MAX_STORED_BODY_CHARS
from src.pipeline.persistence import _relevant_actors
from src.pipeline.summary import (
    SummaryOutput,
    _cap_vuln_importance,
    _drop_self_url,
    _has_japanese_kana,
    _normalize_temporal,
)
from src.tools.article_model import Article
from src.tools.content_extractor import ContentExtractor, ExtractionResult
from src.tools.discord_publisher import BriefingMessage, Source
from src.tools.llm_client import LLMClient
from src.tools.text_sanitizer import has_html_residue, sanitize_for_display
from src.tools.text_utils import strip_html as _strip_html

# LLM が自由記述で埋める metadata キー (HTML 残渣の一括除去対象)。
# 新しい自由記述フィールドを metadata に足したらここにも 1 行足す
# (個別 sanitize は漏れる — 2026-08-15 に remediation で実害が出た)。
LLM_FREE_TEXT_METADATA_KEYS: tuple[str, ...] = (
    "remediation",
    "technical_axis_summary",
    "socio_political_rationale",
    "victim_city",
    "subject_rationale",
)

_log = get_logger(__name__)

# LLM 要約/判定プロンプトに渡す本文の上限 (2026-07-29)。CISA/Siemens の ICS advisory は
# 1 ページに多数の製品/CVE を列挙し本文が 700KB 超 (実測 757,657 字 ≈ 21.6 万トークン) に
# なることがあり、切り詰めないと Ollama 推論が 300s timeout して RSS run が partial_failure 化
# していた (毎 run 同一記事を再試行)。BLUF 要約には冒頭の概要+主要 CVE で十分なため上限を
# 設ける。IoC/actor 検出 (regex) は保存上限 MAX_STORED_BODY_CHARS (100k) と同じ範囲で
# 走らせる — 保存 ⊇ 検出の保証 (2026-08-06、body_limits.py 参照)。
MAX_LLM_BODY_CHARS = 40_000

# 抽出した公開日の妥当域 (_is_plausible_published)。
# 未来側は時計ずれ / TZ 表記ゆれの吸収分だけ許す。
FUTURE_TOLERANCE = timedelta(days=1)
# 過去側は「一覧に載る = 新着」という前提の緩い上限。実測の誤読は 1,863 日前だった。
MAX_BACKDATE_DAYS = 365


async def _process_article(
    article: Article,
    extractor: ContentExtractor,
    llm: LLMClient,
    template: jinja2.Template,
    *,
    think: bool = False,
    enrichment: object | None = None,  # LlmEnrichment (Phase 5T-N で LLM verifier 制御に使用)
    brief_count_24h: int = 0,  # Phase 5T-V: R6 cap 判定 snapshot
) -> BriefingMessage:
    """1 記事を抽出 → 要約 → BriefingMessage 化する。

    ``think``: 既定 False (要約は圧縮タスクなので thinking 不要)。True にすると
    深い推論を有効化 (Gemma 4 等で時間と引き換えに品質が向上することがある)。
    """
    # a. 本文抽出 (URL から trafilatura、失敗時は summary_html fallback)
    # Phase 3: triage 前に先行抽出済み (thin feed) なら再抽出せず再利用する。
    if article.body_text:
        body = article.body_text
        # thin prefetch (filters._prefetch_thin_bodies) は extractor.extract 由来の全文。
        body_source: str = "prefetch"
        extraction_failure_reason: str | None = None
    else:
        extraction = await extractor.extract(article.url)
        body, body_source, extraction_failure_reason = _resolve_body(article, extraction)
        article = _resolve_published(article, extraction)
    degenerate = _degenerate_body_reason(body)
    if degenerate is not None:
        # 取得側の失敗理由 (http_error_403 等) を運ぶ — raise で破棄すると初回
        # extract_failed の理由が DB に一切残らない (監査 2026-08-01)。
        raise DegenerateBodyError(degenerate, extraction_reason=extraction_failure_reason)
    return await _summarize_and_build(
        article,
        body,
        llm,
        template,
        think=think,
        enrichment=enrichment,
        brief_count_24h=brief_count_24h,
        body_source=body_source,
        extraction_failure_reason=extraction_failure_reason,
    )


async def _summarize_and_build(
    article: Article,
    body: str,
    llm: LLMClient,
    template: jinja2.Template,
    *,
    think: bool = False,
    enrichment: object | None = None,
    brief_count_24h: int = 0,
    body_source: str = "unknown",
    extraction_failure_reason: str | None = None,
) -> BriefingMessage:
    """抽出済み本文 ``body`` から 要約 → IOC 抽出 → enrich → BriefingMessage 化する。

    ``_process_article`` から本文抽出を分離したコア。Grok tweet のように URL から
    本文を抽出する必要がない (本文が既に手元にある) ソースが直接呼べるようにするため。
    enrichment (翻訳 / PMESII / Victim / Diamond 2 軸 / IOC / editorial_stance) は
    すべて ``_build_briefing`` 内で付与される。
    """
    from src.config_loader import LlmEnrichment as _LlmEnrichment

    enrich = enrichment if isinstance(enrichment, _LlmEnrichment) else _LlmEnrichment()
    # 保存 ⊇ 検出の保証 (2026-08-06): 以降の全処理 (IoC/actor 検出・LLM・保存) を
    # 保存上限と同じ範囲に揃える。ここで切らないと「表示本文に存在しない entity」が
    # 生まれる (幽霊 actor 監査)。詳細は src/pipeline/body_limits.py。
    body = body[:MAX_STORED_BODY_CHARS]
    # 巨大 advisory (757KB 実測) が要約/判定 LLM プロンプトを溢れさせ Ollama 推論 timeout を
    # 起こすのを防ぐ。LLM に渡す本文はさらに小さい上限で切り詰める (消費者側キャップ)。
    llm_body = body[:MAX_LLM_BODY_CHARS]
    # b. LLM 要約 (Jinja2 テンプレで構築したプロンプト) — **抽出専用**
    prompt = template.render(article=article, body=llm_body)
    summary = await llm.generate_structured(prompt, schema=SummaryOutput, think=think)
    # b2. 統合判断分類器 (2026-07-26 抜本策): 過負荷 summarizer で枯死する判断系
    # (editorial_stance / intent / diamond / event_date / i_infra / article_type /
    # subject_actor) を **1 つの focused 呼び出し**に集約して上書きする。個別 focused 分類器
    # 3 つ (stance / analysis_axes / subject) を統合 — A/B + 実データ目視で正確性を確認
    # (docs/actor_observed_history_design.md)。失敗時は None → summarizer 由来を維持 (正直な欠測)。
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.judgment_classifier import classify_judgment

    _registry = load_actor_aliases()
    # subject 候補 = 本文言及集合 (R-A 済)。二重ゲート (辞書 + 言及所属) を構造内蔵。
    _candidates = _relevant_actors(_registry.find_all(llm_body), summary.category)
    _published = getattr(article, "published", None)
    _j = await classify_judgment(
        llm,
        title=getattr(article, "title", "") or "",
        category=summary.category,
        body=llm_body,
        published=_published.strftime("%Y-%m-%d") if _published else None,
        candidates=_candidates,
        # 本文が空/極短のときの代替入力 (旧 analysis_axes の fallback 復元、監査 2026-08-01)
        summary_text=summary.summary,
    )
    if _j is not None:
        _pmesii = list(summary.pmesii_axes or [])
        if _j.i_infra and "I-infra" not in _pmesii:
            _pmesii.append("I-infra")
        _rf = {**(summary.routing_flags or {})}
        # recap (まとめ/動向レポート/Top N) は多アクターの調査で単一主題を持たない —
        # 主題を付けない (backfill 目視で shinyhunters 等が月次まとめに過剰帰属した対策、
        # 2026-07-27)。title 層の主題は determine_subject_actors が別途評価する。
        if _j.subject_actor_id and _j.article_type != "recap":
            # 主題 = 統合分類器の判定 (title 層は determine_subject_actors が優先評価)
            _rf["primary_actor_id"] = _j.subject_actor_id
            _rf["confidence"] = _j.subject_confidence
        # C1/C2 (2026-07-27): ungated 生アクター名を別キーで保持。辞書外の新顔を
        # harvest (provisional 発見) + llm_primary_actor_raw (D1 監視) へ供給する。
        # recap は多アクター列挙で単一主体を持たないため付けない。
        if _j.named_primary_actor and _j.article_type != "recap":
            _rf["named_primary_actor"] = _j.named_primary_actor
        # 主題判定の根拠 (2026-08-13 可視化): 特に「主題なし」の理由を記事詳細へ届ける
        if _j.subject_rationale and _j.article_type != "recap":
            _rf["subject_rationale"] = _j.subject_rationale
        summary = summary.model_copy(
            update={
                "editorial_stance": _j.editorial_stance,
                "article_type": _j.article_type,  # summarizer で 100% breaking に枯死→救済
                "diamond": _j.to_diamond_dict(),
                "event_date": _j.event_date,
                "event_date_basis": _j.event_date_basis,
                "compromise_date": _j.compromise_date,
                "pmesii_axes": _pmesii,
                "routing_flags": _rf,
            }
        )

    # c. Phase 4: 機械的 IOC 抽出 + アクター名正規化
    from src.cti.actor_candidates import harvest_candidates, harvest_enabled
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.ioc_extractor import (
        extract_iocs,
        filter_benign,
        merge_iocs,
        merge_techniques,
    )

    # Phase 5T-N: source_url を渡して self-ref ドメイン抑制
    extracted = extract_iocs(body, source_url=article.url)
    # Phase 5T-N: Layer 2 (LLM verifier) で context-aware に誤抽出を drop
    if enrich.enabled and enrich.verify_iocs_with_llm:
        from src.cti.ioc_llm_verifier import verify_iocs_with_llm

        extracted = await verify_iocs_with_llm(
            extracted,
            body,
            llm=llm,
            article_id=article.id,
        )
    # Phase 5F: LLM 出力の IOC 配列に出典 URL が混入した場合の二重防御
    # Phase 5J-2: article.url 自身も除外 (LLM が記事 URL を IOC として返す誘発要因の構造的遮断)
    merged_iocs = _drop_self_url(
        filter_benign(merge_iocs(summary.iocs, extracted)),
        article.url,
    )
    # R-B 強化 (2026-06-16): LLM が出す URL は出典/引用 (cve.org/vendor advisory/archive/
    # gov/think-tank 等) が大半で、benign host allowlist では追従不能 (実データで ioc_url の
    # 66% が citation)。URL 型は **本文 regex 抽出 (benign/self-ref 済 = 高精度)** に限定し、
    # LLM 由来の単独 URL を ioc_url から除外する。非 URL 型 (cve/ip/domain/hash) は LLM が
    # 付加価値を持つため従来どおり残す。
    _body_urls = set(extracted.urls)
    merged_iocs = [
        ioc
        for ioc in merged_iocs
        if not ioc.lower().startswith(("http://", "https://")) or ioc in _body_urls
    ]
    merged_techs = merge_techniques(summary.mitre_techniques, extracted)
    # 本文に登場する既知アクターを検出 (後段で metadata に保持)
    actor_registry = load_actor_aliases()
    matched_actors = actor_registry.find_all(body)
    # R-A (存在≠関連): organization/contractor (GRU/FSB/IRGC/i-Soon 等の国家機関・請負) は
    # 地政学・軍事記事で *言及* されるだけで脅威活動の主体ではない (ActorAlias.kind の
    # docstring が「group=活動 / organization=言及」と区別を明記)。cyber カテゴリ記事での
    # み機関帰属を認め、非 cyber では group-kind (実 APT) のみ残す。これで actor entity の
    # 39% を占めた地政学記事への誤帰属 (iran_irgc/russia_gru) を排し、ダッシュボードの
    # 敵対 APT KPI / 国籍 mover の水増しも是正する。
    matched_actors = _relevant_actors(matched_actors, summary.category)
    detected_actors = [a.display_with_aliases() for a in matched_actors]
    detected_actor_ids = [a.id for a in matched_actors]
    # 主題アクターは上流の統合判断分類器 (b2) が既に summary.routing_flags.primary_actor_id へ
    # 判定済み (候補=言及集合の二重ゲート)。ここでは再判定しない (呼び出しは 1 記事 1 回)。
    # F5 alias 使用統計: 実際に本文で発火した名前 (canonical/alias) を記録用に保持。
    # find_all は最初のヒット名で打ち切るため、全ヒット名を別途走査する (対象は
    # マッチ済みアクターのみ = 追加コストは僅少)。
    detected_actor_matches = [
        [a.id, name] for a in matched_actors for name in actor_registry.matched_names_for(a, body)
    ]
    # ランサム識別: 関連アクターに ransom_group family があれば true (category 直交、地図分割用)。
    # ※ ransom_group は ActorAlias.family の値 (kind=group/contractor/organization とは別軸)。
    is_ransomware = any(getattr(a, "family", "") == "ransom_group" for a in matched_actors)
    # 語彙拡張 ①-c: 検出アクターの sponsoring nation (cn/ru/kp/ir 等) を辞書から回収。
    # 新検出ロジックではなく ActorAlias.nation の lookup (Intel Graph で実績あり)。
    # routing/PIR から「中露朝の脅威活動」を条件化できるようにするため metadata に保持。
    detected_actor_nations = sorted({a.nation for a in matched_actors if a.nation})
    # Actor Recall Layer (Part C2): 辞書未収録の新興アクター候補を採取 (flag gate、deploy-dark)。
    # 確定 detected_actor_ids には混ぜず、暗定 (actor_provisional) として別管理する。
    provisional_actor_candidates: list[dict[str, str]] = []
    if harvest_enabled():
        # C2 (2026-07-27): ungated 生アクター名を harvest に渡す。旧実装は gated
        # primary_actor_id (辞書内 id) を渡していたため knows_name=True で常に除外され
        # llm_primary 信号が構造的に死んでいた。named_primary_actor は辞書外の新顔を含む。
        _pid = str((summary.routing_flags or {}).get("named_primary_actor") or "").strip()
        provisional_actor_candidates = [
            {"key": c.key, "name": c.raw_name, "signal": c.signal, "excerpt": c.excerpt}
            for c in harvest_candidates(
                body=body,
                primary_actor_id=_pid,
                registry=actor_registry,
                # カテゴリ混同遮断 (2026-08-13): 同記事で malware/tool として抽出済みの
                # 名前が主体名フィールド経由でアクター候補化されるのを自己整合で防ぐ
                known_non_actor_names=[*summary.malware_families, *summary.tools],
            )
        ]
    if len(merged_iocs) > len(summary.iocs) or len(merged_techs) > len(summary.mitre_techniques):
        _log.info(
            "ioc_extraction_augmented",
            article_id=article.id,
            llm_iocs=len(summary.iocs),
            extracted_iocs=len(extracted.all_iocs),
            merged_iocs=len(merged_iocs),
            llm_techs=len(summary.mitre_techniques),
            merged_techs=len(merged_techs),
        )

    # d. BriefingMessage に整形 (マージ後の IOC / techniques を反映、検出アクターも metadata 保存)
    # Phase 5K: body_text を渡して router の signal 抽出に使う
    return _build_briefing(
        article,
        summary,
        merged_iocs=merged_iocs,
        merged_techniques=merged_techs,
        detected_actors=detected_actors,
        detected_actor_ids=detected_actor_ids,
        detected_actor_nations=detected_actor_nations,
        detected_actor_matches=detected_actor_matches,
        is_ransomware=is_ransomware,
        body_text=body,
        brief_count_24h=brief_count_24h,
        provisional_actor_candidates=provisional_actor_candidates,
        body_source=body_source,
        extraction_failure_reason=extraction_failure_reason,
    )


# 本文が「読めていない」ことの決定論検出 (監査 #10 / 2026-07-05 実測病理)。
# ブロック画面や極短断片から LLM に要約させると、タイトルだけの幻覚がそのまま
# 配信・台帳開設・headline まで到達する。ゴミ入力は境界 (要約前) で止める。
_DEGENERATE_BODY_MIN_CHARS = 80
_BLOCK_PAGE_RE = re.compile(
    r"security service to protect (?:itself|against)|enable javascript and cookies|"
    r"checking your browser|verify you are human|are you a robot|"
    r"just a moment\.\.\.|attention required!|access to this page has been denied|"
    r"please complete the security check|cf-browser-verification",
    re.IGNORECASE,
)
_BLOCK_PAGE_SCAN_CHARS = 600
# ブロック画面は短い。長い本文中に語句が出ても記事本文 (例: Cloudflare についての記事) とみなす
_BLOCK_PAGE_MAX_LEN = 2000


class DegenerateBodyError(Exception):
    """本文が要約に値しない (ブロック画面/極短)。content 起因の恒久 skip。

    ``extraction_reason`` = 全文取得側の失敗理由 (http_error_403/js_challenge/timeout 等、
    _resolve_body 由来)。「なぜ読めない本文になったか」を初回 extract_failed の
    DB 記録まで運ぶ (無いと 取得block/パース不能/閾値棄却 の切り分けが検証不能)。
    """

    def __init__(self, reason: str, *, extraction_reason: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.extraction_reason = extraction_reason


def _degenerate_body_reason(body: str) -> str | None:
    """本文が「読めていない」なら理由文字列、健全なら None (決定論)。"""
    text = (body or "").strip()
    if len(text) < _DEGENERATE_BODY_MIN_CHARS:
        return f"body_too_short:{len(text)}"
    if len(text) <= _BLOCK_PAGE_MAX_LEN and _BLOCK_PAGE_RE.search(text[:_BLOCK_PAGE_SCAN_CHARS]):
        return "block_page"
    return None


def body_source_for_extraction(extraction: ExtractionResult) -> str:
    """成功した抽出の body_source (取得元) を返す。briefing/reprocess で共有 (DRY, B2a)。

    playwright 突破 = playwright_extract、通常 trafilatura = full_extract。この判定を 1 箇所に
    集約し、再取得経路 (reprocess) が主経路と乖離しないようにする。
    """
    return (
        "playwright_extract"
        if extraction.extraction_method == "playwright+trafilatura"
        else "full_extract"
    )


def _resolve_published(article: Article, extraction: ExtractionResult) -> Article:
    """``published`` が代用値の記事に、抽出したページ公開日を充てる (2026-08-16)。

    sitemap(lastmod なし) / HTML 一覧 / Playwright 一覧は取得時点で公開日を知れないため
    取込時刻を入れている。``ContentExtractor`` は ``published_date`` を取っていたが
    **消費者がゼロ (write-only) で、実公開日は捨てられていた**。その結果 published_at =
    取込時刻となり、何年も前の記事が「今日」として台帳・地図・ブリーフに載っていた。

    実公開日が判った時点で置き換え、旗を下ろす。抽出が日付を取れなければ代用値のまま
    (旗も立てたまま) にして、「判らない」を「今日」と偽らない。

    ⚠ **抽出日付を無条件に信じない** — 実測で Push Security の記事が 1,863 日前と
    抽出された (ページ内の著作権表記等の誤読とみられる)。誤った日付を書くと記事が
    時間軸の遥か下に沈んで実質消えるため、妥当性を外れた値は棄却して代用値のまま残す。
    「判らない」を保つ方が、確信をもって誤るより後段が扱いやすい。
    """
    if not article.published_is_placeholder:
        return article
    extracted = extraction.published_date
    if extracted is None:
        return article
    resolved = extracted if extracted.tzinfo else extracted.replace(tzinfo=UTC)
    if not _is_plausible_published(resolved, article.published):
        _log.warning(
            "published_date_rejected",
            article_id=article.id,
            extracted=resolved.isoformat(),
            fallback=article.published.isoformat(),
        )
        return article
    return article.model_copy(update={"published": resolved, "published_is_placeholder": False})


def _is_plausible_published(extracted: datetime, ingested_at: datetime) -> bool:
    """抽出した公開日が採用に値するか (2026-08-16)。

    - 未来 (時計ずれ分の猶予を超える) は誤り。「まだ公開されていない記事」になり
      時間窓やソートから外れる
    - 取込時刻より ``MAX_BACKDATE_DAYS`` 以上前は誤読を疑う。placeholder が付くのは
      一覧/sitemap 由来で、一覧は新着しか載せないため大きく遡ることは通常ない
    """
    if extracted > ingested_at + FUTURE_TOLERANCE:
        return False
    return extracted >= ingested_at - timedelta(days=MAX_BACKDATE_DAYS)


def _resolve_body(article: Article, extraction: ExtractionResult) -> tuple[str, str, str | None]:
    """抽出本文 / 失敗時の summary_html フォールバックを決定する。

    戻り値 = (body, body_source, extraction_failure_reason)。body_source は下流の
    監査・切り株再取得キューの SSoT。全文失敗→feed 抜粋 fallback は無音でなく
    body_source='feed_summary' + failure_reason で構造化して残す
    (docs/body_extraction_and_entity_integrity_redesign.md §2.1)。
    """
    if extraction.success and extraction.text.strip():
        return extraction.text, body_source_for_extraction(extraction), None
    fallback = _strip_html(article.summary_html)
    _log.info(
        "extraction_fallback_to_summary_html",
        article_id=article.id,
        extraction_reason=extraction.failure_reason,
        fallback_length=len(fallback),
    )
    return fallback, "feed_summary", extraction.failure_reason


def _final_pmesii_axes(
    merged: list[str],
    *,
    sector_canonical: str | None,
    title: str,
    summary_text: str,
) -> list[str]:
    """PMESII 軸の最終確定 (2026-07-16 監査対処): T 廃止 + I-infra 決定論フロア。

    - T (Time) 軸は廃止 — 付与率 ~30% で弁別力がなく、6 週間の沈黙に誰も気づかなかった
      (時間次元は event_date / compromise_date が一級フィールドとして担う)。DB 列は
      履歴保全のため残すが新規には書かない。
    - I-infra は NISC セクター写像 (SSoT: nisc_sectors.nisc_sector_for、canonical +
      キーワード層) の決定論フロアで補強 — LLM 供給が枯れても重要インフラ該当は落ちない。
    """
    from src.cti.nisc_sectors import nisc_sector_for

    axes = [a for a in merged if a != "T"]
    if "I-infra" not in axes and nisc_sector_for(sector_canonical, title, summary_text):
        axes.append("I-infra")
    return axes


def _build_briefing(
    article: Article,
    summary: SummaryOutput,
    *,
    merged_iocs: list[str] | None = None,
    merged_techniques: list[str] | None = None,
    detected_actors: list[str] | None = None,
    detected_actor_ids: list[str] | None = None,
    detected_actor_nations: list[str] | None = None,
    detected_actor_matches: list[list[str]] | None = None,
    is_ransomware: bool = False,
    body_text: str = "",
    brief_count_24h: int = 0,  # Phase 5T-V: R6 cap 判定 snapshot (本番のみ非ゼロ)
    provisional_actor_candidates: list[dict[str, str]] | None = None,
    body_source: str = "unknown",
    extraction_failure_reason: str | None = None,
) -> BriefingMessage:
    metadata: dict[str, object] = {
        "feed_url": article.feed_url,
        "published": article.published.isoformat(),
        # Discord embed 側で <t:UNIX:R> / embed.timestamp に使う
        "received_at": article.published.isoformat(),
    }
    # Phase 5E: 原タイトル (英語のことが多い) は metadata に保持し検索性を担保
    if article.title:
        metadata["original_title"] = article.title
    if detected_actors:
        metadata["detected_actors"] = detected_actors
    # Phase 4.5: STIX bundle 添付用に actor.id (canonical 名) も保持
    if detected_actor_ids:
        metadata["detected_actor_ids"] = detected_actor_ids
    # 語彙拡張 ①-c: 検出アクターの nation (routing/PIR の actor_nation 条件で参照)
    if detected_actor_nations:
        metadata["detected_actor_nations"] = detected_actor_nations
    # F5 alias 使用統計: [actor_id, ヒット名] の対 (persistence が actor_alias_usage へ記録)
    if detected_actor_matches:
        metadata["detected_actor_matches"] = detected_actor_matches
    # Actor Recall Layer (Part C2): 新興アクター候補 (暗定)。_persist_article_entities が
    # actor_provisional entity として永続化し、裏取り後に proposal キューへ集計する。
    if provisional_actor_candidates:
        metadata["provisional_actor_candidates"] = provisional_actor_candidates
    # ランサム識別 (category 直交フラグ): 攻撃者が ransom_group のとき true。
    # 地図の「ランサム/その他」分割に使う。breach/malware 等の category は保持する。
    if is_ransomware:
        metadata["is_ransomware"] = True
    # Phase 5K: article_type を metadata に保存 (router の signal 抽出で使用)
    metadata["article_type"] = summary.article_type
    # Phase 5L-3: LLM が出した routing_flags も metadata に保存 (router で primary 信号として使う)
    # editorial_stance は top-level (emission 安定) を優先し routing_flags に注入 (下流は不変)。
    _rf = dict(summary.routing_flags or {})
    if summary.editorial_stance and summary.editorial_stance != "unknown":
        _rf["editorial_stance"] = summary.editorial_stance
    if _rf:
        metadata["routing_flags"] = _rf

    # Phase H: PMESII-PT 軸 + Diamond victim を metadata に保存 (V1/V2/V3 で利用)。
    # LLM 判定 + feed/category default mapping を union (Recall 重視)。
    from src.cti.taxonomy_normalizer import load_normalizer as _load_taxonomy_normalizer

    _taxonomy = _load_taxonomy_normalizer()
    merged_axes = _taxonomy.merge_axes(
        llm_axes=list(summary.pmesii_axes or []),
        feed_title=article.feed_title or "",
        category=summary.category,
    )
    sector_canonical, sector_raw = _taxonomy.normalize_sector(summary.victim_sector)
    country_iso, country_raw = _taxonomy.normalize_country(summary.victim_country)
    # 状態分離監査 2026-07-16: T 軸は廃止 (書かない)、I-infra は NISC 写像の決定論
    # フロアで補強する (決定論供給は枯れない — feed_default だけが生き残った実測の一般化)。
    merged_axes = _final_pmesii_axes(
        merged_axes,
        sector_canonical=sector_canonical,
        title=article.title or "",
        summary_text=summary.summary or "",
    )
    if merged_axes:
        metadata["pmesii_axes"] = merged_axes
    if sector_canonical or sector_raw:
        metadata["victim_sector_canonical"] = sector_canonical
        metadata["victim_sector_raw"] = sector_raw
    if country_iso or country_raw:
        metadata["victim_country_iso"] = country_iso
        metadata["victim_country_raw"] = country_raw
    # 被害国スコープ (監査 2026-08-01 ⑥): ISO2 に解決できない "global"/"EU"/複数国は
    # scope として保持 (従来は正規化器が黙って捨てて iso=NULL 落ちしていた —
    # briefing/summarizer.j2 が指示している語彙なのに受け皿が無かった断線)。
    country_scope: str | None = None
    if country_iso is None and country_raw:
        country_scope, _scope_isos = _taxonomy.normalize_country_scope(country_raw)
        if country_scope:
            metadata["victim_country_scope"] = country_scope
        if _scope_isos:
            # 複数国列挙の構成国は involved_country として保存 (被害国は関与国でもある。
            # 既存の分解パターン :449-458 と同じ消費者に流す)
            _prev_isos = metadata.get("involved_country_isos")
            _cur = [str(x) for x in _prev_isos] if isinstance(_prev_isos, list) else []
            for _iso in _scope_isos:
                if _iso not in _cur:
                    _cur.append(_iso)
            metadata["involved_country_isos"] = _cur
    # #7: 被害都市 (明示時のみ)。entity_type='victim_city' で永続化し都市レベル geocode の対象に。
    if summary.victim_city and summary.victim_city.strip():
        metadata["victim_city"] = summary.victim_city.strip()
    # 地政学情勢ブリッジ: 関与国を ISO 正規化 (未知/重複は捨てる)。_persist_article_entities が
    # entity_type='involved_country' で保存し、actor.nation 経由のサイバー↔地政学相関に使う。
    if summary.involved_countries:
        _seen_iso: set[str] = set()
        _isos: list[str] = []
        for c in summary.involved_countries:
            iso, _raw = _taxonomy.normalize_country(str(c))
            if iso and iso.lower() not in _seen_iso:
                _seen_iso.add(iso.lower())
                _isos.append(iso)
        if _isos:
            metadata["involved_country_isos"] = _isos
    # Phase 2 (geo-map): 被害組織名 (実際に攻撃された組織のみ)。後続の
    # _persist_article_entities が entity_type='victim_org' で保存し、地図 geocode の対象にする。
    if summary.victim_orgs:
        # 空白除去 + 重複除去 (順序保持)、空文字は捨てる
        _seen_org: set[str] = set()
        _orgs: list[str] = []
        for o in summary.victim_orgs:
            v = str(o).strip()
            if v and v.lower() not in _seen_org:
                _seen_org.add(v.lower())
                _orgs.append(v)
        if _orgs:
            metadata["victim_orgs"] = _orgs
    # Phase Diamond: Capability 軸の構造化フィールド (malware_family / tool)
    # 後続の _persist_article_entities が metadata から拾って article_entities に保存
    if summary.malware_families:
        metadata["malware_families"] = list(summary.malware_families)
    if summary.tools:
        metadata["tools"] = list(summary.tools)
    # Phase Diamond-Axes: Diamond Model の 2 meta-feature 軸を LLM 出力から正規化して保存。
    # socio_political(intent/rationale/confidence) = Adversary⇄Victim、
    # technical = Capability⇄Infrastructure。永続化は add_article 経路 (articles 列)。
    from src.cti.diamond_model import parse_diamond_axes

    diamond_axes = parse_diamond_axes(summary.diamond)
    if diamond_axes.socio_political.intent != "unknown":
        metadata["socio_political_intent"] = diamond_axes.socio_political.intent
        metadata["intent_confidence"] = diamond_axes.socio_political.confidence
        if diamond_axes.socio_political.rationale:
            metadata["socio_political_rationale"] = diamond_axes.socio_political.rationale
        metadata["socio_political_confidence"] = diamond_axes.socio_political.confidence
    # P4: 対処 (CoA)。本文明示のみ (LLM 指示側で一般論創作を禁止)。
    # 監査 2026-07-05 P1: 96fde6c のインデント混入 (rationale/confidence が remediation
    # 存在時のみ永続化される退行) を復元 — rationale は intent 判定に付随する。
    if summary.remediation and summary.remediation.strip():
        # sanitize してから切り詰める (逆順だとタグ途中で切れた断片 `</d` が残る)
        metadata["remediation"] = sanitize_for_display(summary.remediation).strip()[:300]
    if diamond_axes.technical:
        metadata["technical_axis_summary"] = diamond_axes.technical
    # LLM 自由記述の HTML 残渣を一括除去 + 未登録キーの検出 (2026-08-15)。
    # title/summary/analyst_note は上で個別 sanitize 済だが、後から増えた
    # metadata 側の自由記述 (remediation 等) が漏れ、本文末尾の閉じタグ列
    # (`</p></div>...`) を LLM が引きずった値が UI に露出していた。
    # 個別追加は同じ漏れを繰り返すため、(1) 既知キーを SSoT として一括 sanitize し、
    # (2) **全 metadata 文字列を事後検証**して漏れたキーを名指しで警告する。
    # (2) が無いと「新しい自由記述キーを足したが SSoT 登録を忘れた」を誰も検知できない。
    for _key in LLM_FREE_TEXT_METADATA_KEYS:
        _val = metadata.get(_key)
        if isinstance(_val, str) and _val:
            metadata[_key] = sanitize_for_display(_val)
    _residue_keys = [k for k, v in metadata.items() if isinstance(v, str) and has_html_residue(v)]
    if _residue_keys:
        _log.warning(
            "metadata_html_residue_detected",
            article_id=article.id,
            keys=_residue_keys,
            hint="LLM_FREE_TEXT_METADATA_KEYS への登録漏れ、または sanitizer の不足",
        )
    # 時間軸レイヤ b/c: 事象発生日 / 基準 / 侵害開始日 (報道時刻と分離)。報道日 +1d を上限に
    # 検証し、偽の日付は捨てる。reference = published or now (事象は報道後に起きえない)。
    _ref_dt = article.published or datetime.now(UTC)
    _ref_date = _ref_dt.date() + timedelta(days=1)
    _ev, _basis, _comp = _normalize_temporal(
        summary.event_date,
        summary.event_date_basis,
        summary.compromise_date,
        reference=_ref_date,
    )
    if _ev:
        metadata["event_date"] = _ev
        if _basis:
            metadata["event_date_basis"] = _basis
    if _comp:
        metadata["compromise_date"] = _comp
    # Phase 5L-4: dedup_key を計算して metadata に保存 (ch 横断 dedup / クラスタリング用)
    from src.cti.dedup_key import compute_dedup_key
    from src.cti.llm_routing_flags import parse_routing_flags

    llm_flags_for_key = parse_routing_flags(summary.routing_flags or {})
    body_first = (body_text or "")[:200]
    dedup_key_value = compute_dedup_key(
        llm_key=llm_flags_for_key.dedup_key,
        title=article.title or summary.title_ja,
        body_first_line=body_first,
        article_id_fallback=article.id,
    )
    if dedup_key_value:
        metadata["dedup_key"] = dedup_key_value
    # Phase Diamond L2: trafilatura 取得本文を metadata に渡して post 後に articles.body へ保存。
    # 上限は保存 ⊇ 検出を保証する MAX_STORED_BODY_CHARS (2026-08-06 に 20k → 100k、
    # body_limits.py 参照)。
    if body_text:
        metadata["_article_body"] = body_text[:MAX_STORED_BODY_CHARS]
    # 本文完全性 (2026-07-27): body の由来と全文取得失敗理由を persistence へ渡す
    # (update_article_body の source 引数 = body_source 列の SSoT)。
    metadata["_article_body_source"] = body_source
    if extraction_failure_reason:
        metadata["_extraction_failure_reason"] = extraction_failure_reason
    # Phase 5J-2: 日本語訳タイトル採用ロジック (Phase 5E の改修)
    title_translated = (summary.title_ja or "").strip()
    if _has_japanese_kana(article.title):
        display_title = article.title
    else:
        display_title = title_translated or article.title
        # 接地検証 (監査 2026-08-01): 生成見出しが原文に無い固有名詞 (英字) を含む
        # なら幻覚の疑い → 原題へ fallback (alert の信頼性優先。無関係 3 記事が
        # 同一幻覚見出しで 3 連投された事象の再発防止)。
        if title_translated:
            from src.pipeline.summary import ungrounded_title_tokens

            _bad_tokens = ungrounded_title_tokens(
                title_translated, f"{article.title} {body_text[:5000]}"
            )
            if _bad_tokens:
                _log.warning(
                    "title_grounding_failed",
                    article_id=article.id,
                    ungrounded=_bad_tokens[:5],
                )
                display_title = article.title
    # Phase 5L-2: 表示用テキスト sanitize (二重防御)
    # feed の summary_html や LLM 出力に紛れる HTML タグ (例: </td>) や
    # HTML エンティティを除去。LLM 翻訳が元タイトルの HTML を引きずるケースを
    # 観測したため post-LLM にも適用する (import は module-level に統一 — 関数内 import は
    # 同名の module-level 束縛を隠し UnboundLocalError を招く)。
    display_title = sanitize_for_display(display_title, max_length=120, collapse_whitespace=True)
    summary_clean = sanitize_for_display(summary.summary)
    analyst_note_clean = (
        sanitize_for_display(summary.analyst_note) if summary.analyst_note else None
    )
    # Phase B-cal2: vulnerability/advisory の high を悪用シグナル不在なら medium に降格。
    capped_importance = _cap_vuln_importance(
        summary.importance,
        summary.category,
        display_title,
        summary_clean,
        (body_text or "")[:3000],
    )
    if capped_importance != summary.importance:
        _log.info(
            "importance_capped",
            article_id=article.id,
            category=summary.category,
            from_=summary.importance,
            to=capped_importance,
            reason="no_exploit_signal",
        )
    msg = BriefingMessage(
        title=display_title,
        # Phase 5T-O: RSS 経路は 1 記事 = 1 incident で BLUF が title と重複するため廃止。
        # LLM がプロンプト変更後も誤って返した bluf 値は受け取らず空文字で固定。
        bluf="",
        importance=capped_importance,
        category=summary.category,
        summary=summary_clean,
        iocs=merged_iocs if merged_iocs is not None else summary.iocs,
        mitre_techniques=(
            merged_techniques if merged_techniques is not None else summary.mitre_techniques
        ),
        sources=[
            Source(
                title=article.feed_title or "source",
                url=article.url,
                language="auto",
            ),
        ],
        analyst_note=analyst_note_clean,
        metadata=metadata,
    )
    # Phase 5L-1: 共通 routing engine で RoutingDecision を取得し log を出力
    # Phase 5T-V: feed_title + brief_cap snapshot を signal に注入。
    # brief_count_24h は呼び出し側 (run_pipeline) で repo から取得して渡す。
    # 単体 test は default=0 で渡されないため cap 機能 OFF となり既存挙動を維持。
    # Phase B-cal: source-based low_reliability 降格は撤去 (完全 content-based 化)。
    from src.cti.router import route
    from src.cti.routing_signals import extract_signals_from_briefing

    feed_title = article.feed_title or ""
    signals = extract_signals_from_briefing(
        msg,
        body_text=body_text,
        feed_title=feed_title,
        brief_count_24h_snapshot=brief_count_24h,
    )
    decision = route(signals)
    _log.info(
        "routing_decision",
        article_id=article.id,
        source="briefing",
        channel=decision.channel,
        rule_id=decision.rule_id,
        reason=decision.reason,
        signals=decision.signals_snapshot,
    )
    new_metadata = {
        **msg.metadata,
        "target_channel": decision.channel,
        "routing_reason": decision.reason,
        "routing_rule_id": decision.rule_id,
    }
    return msg.model_copy(update={"metadata": new_metadata})
