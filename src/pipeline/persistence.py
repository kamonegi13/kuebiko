"""記事 outcome / entity / feed 死活の永続化ヘルパ (src.main から分割)。"""

from __future__ import annotations

import re

from src.cti.subject_actor import (  # noqa: PLC0414 — `as` 形式 = mypy 明示 re-export
    _CYBER_CATEGORIES as _CYBER_CATEGORIES,  # facade re-export (main.py / 既存テスト互換)
)
from src.cti.subject_actor import (
    _relevant_actors as _relevant_actors,  # facade re-export (briefing.py / main.py 互換)
)
from src.cti.subject_actor import (
    determine_subject_actors,
)
from src.cti.victim_org_filter import is_vendor_noise
from src.logging_config import get_logger
from src.storage.run_history import ArticleRecord, RunHistoryRepository
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage
from src.tools.source_router import ArticleSource
from src.tools.url_normalizer import url_hash

_log = get_logger(__name__)


def _extraction_failure_state(failure_reason: str) -> tuple[str, str] | None:
    """top-level failure_reason から body_source 失敗状態を導出する (B3a、抽出失敗のみ)。

    DegenerateBodyError (msg=None) で body が NULL のまま INSERT される記事を可視化する。
    block_page = WAF/bot 壁 (blocked)、body_too_short = 本文が空/極短 (pending_refetch=
    再取得候補)。抽出失敗でない (summarize_failed 等) は None を返し body_source を触らない。
    戻り値 = (body_source, extraction_failure_reason)。
    """
    # 書き手 (orchestrator) は "degenerate_body: {reason}" (コロン後スペース入り) を書く。
    # prefix 剥がし + strip で正規化してから照合する — startswith の完全連結照合は
    # スペース 1 個の書式差で 100% デッドコード化した (監査 2026-08-01 で発覚)。
    prefix = "degenerate_body:"
    if not failure_reason.startswith(prefix):
        return None
    reason = failure_reason[len(prefix) :].strip()
    if reason.startswith("block_page"):
        return ("blocked", "block_page")
    if reason.startswith("body_too_short"):
        return ("pending_refetch", "body_too_short")
    return None


def _persist_article_outcomes(
    *,
    outcomes: list[dict[str, object]],
    articles_by_id: dict[str, Article],
    dedup_repo: RunHistoryRepository | None,
    run_id: int | None,
) -> None:
    """1 ブリーフィングごとに ``articles`` テーブルへ 1 行書く (Phase 5A)。

    - ``run_id is None`` または ``dedup_repo is None`` のときは何もしない
      (CLI 単体実行や互換性テストで run 履歴を使わないケース)
    - 例外は飲み込まずログに残す: dashboard が壊れても本処理は止めない
    """
    if run_id is None or dedup_repo is None or not outcomes:
        return
    for outcome in outcomes:
        article_id = str(outcome["article_id"])
        article = articles_by_id.get(article_id)
        if article is None:
            continue
        msg_obj = outcome.get("msg")
        msg = msg_obj if isinstance(msg_obj, BriefingMessage) else None
        importance = msg.importance if msg is not None else None
        category = msg.category if msg is not None else None
        title = msg.title if msg is not None else article.title
        # Phase B-cal2 (2026-06-05): 永続 URL は briefing の primary source を優先する。
        # 背景: Grok は 1 レポート (article.url=grok.com/chat の JSON ページ) から N 件の
        # tweet briefing を展開するため、article.url のままだと全件が grok.com/chat を指し
        # daily-focus / Web UI のリンクが JSON ページに飛んでいた。msg.sources[0].url は
        # 各 tweet の X パーマリンク (RSS 経路では article.url と同一なので無影響)。
        display_url = article.url
        if msg is not None and msg.sources and msg.sources[0].url:
            display_url = msg.sources[0].url
        # Phase 5L-4: dedup_key を metadata から取り出して永続化
        dedup_key_val: str | None = None
        if msg is not None:
            raw_key = msg.metadata.get("dedup_key")
            if isinstance(raw_key, str) and raw_key:
                dedup_key_val = raw_key
        # Phase H: PMESII-PT 軸 + Diamond victim を metadata から取得
        pmesii_set: set[str] = set()
        victim_sec_can: str | None = None
        victim_sec_raw: str | None = None
        victim_cnt_iso: str | None = None
        victim_cnt_raw: str | None = None
        victim_cnt_scope: str | None = None
        # Phase Diamond-Axes: socio-political intent / technical 軸を metadata から取得
        socio_intent: str | None = None
        socio_rationale: str | None = None
        technical_summary: str | None = None
        # 2026-07-06 バグ修正: intent_conf / remediation_val は if msg 内でしか代入されず
        # 事前初期化が欠落していた。msg=None (grok が briefing を生まない記事等) の永続化で
        # UnboundLocalError → article_record_persist_failed になっていた。兄弟変数と揃える。
        intent_conf: str | None = None
        remediation_val: str | None = None
        # アナリスト所見 (2026-08-18 永続化)。BriefingMessage の直フィールドなので
        # metadata 経由ではなく msg から直接取る。
        analyst_note_val: str | None = None
        # flow Phase 3: route() が metadata に載せた投稿先決定の監査情報。
        # 「なぜこのチャンネルか」を記事単位で永続化する (route() 非経由なら None)。
        routing_rule_id_val: str | None = None
        routing_reason_val: str | None = None
        is_ransomware_val: bool = False
        # 時間軸レイヤ b/c (2026-06-27): 事象発生日 / 基準 / 侵害開始日 (報道時刻と分離)。
        event_date_val: str | None = None
        event_date_basis_val: str | None = None
        compromise_date_val: str | None = None
        # 主題アクター層 (2026-07-17): NULL = 未評価 (判定失敗時も NULL = legacy 照合に fail-safe)
        subject_ids_val: str | None = None
        subject_source_val: str | None = None
        subject_conf_val: str | None = None
        # アクター辞書 D1 (2026-07-26): 主題判定 LLM 層の生入力 (summarizer 出力) を
        # 一級観測として永続化。判定を「入力からの再導出可能な射影」にする — 新アクター
        # 承認時に全期間の LLM 出力へ遡及帰属でき、層の死活も fill-rate 監査で観測できる。
        llm_primary_raw_val: str | None = None
        llm_primary_conf_val: str | None = None
        subject_rationale_val: str | None = None
        if msg is not None:
            axes_obj = msg.metadata.get("pmesii_axes")
            if isinstance(axes_obj, list):
                pmesii_set = {a for a in axes_obj if isinstance(a, str)}
            _vsc = msg.metadata.get("victim_sector_canonical")
            _vsr = msg.metadata.get("victim_sector_raw")
            _vci = msg.metadata.get("victim_country_iso")
            _vcr = msg.metadata.get("victim_country_raw")
            _vcs = msg.metadata.get("victim_country_scope")
            victim_sec_can = _vsc if isinstance(_vsc, str) else None
            victim_sec_raw = _vsr if isinstance(_vsr, str) else None
            victim_cnt_iso = _vci if isinstance(_vci, str) else None
            victim_cnt_raw = _vcr if isinstance(_vcr, str) else None
            victim_cnt_scope = _vcs if isinstance(_vcs, str) else None
            _spi = msg.metadata.get("socio_political_intent")
            _spc = msg.metadata.get("intent_confidence")
            _spr = msg.metadata.get("socio_political_rationale")
            _tas = msg.metadata.get("technical_axis_summary")
            socio_intent = _spi if isinstance(_spi, str) else None
            intent_conf = _spc if isinstance(_spc, str) else None
            _rmd = msg.metadata.get("remediation")
            remediation_val = _rmd if isinstance(_rmd, str) else None
            _note = (msg.analyst_note or "").strip()
            analyst_note_val = _note or None
            socio_rationale = _spr if isinstance(_spr, str) else None
            technical_summary = _tas if isinstance(_tas, str) else None
            _rri = msg.metadata.get("routing_rule_id")
            _rrn = msg.metadata.get("routing_reason")
            routing_rule_id_val = _rri if isinstance(_rri, str) else None
            routing_reason_val = _rrn if isinstance(_rrn, str) else None
            is_ransomware_val = bool(msg.metadata.get("is_ransomware"))
            # 時間軸レイヤ b/c: 事象発生日 / 基準 / 侵害開始日 (報道時刻と分離)。
            _evd = msg.metadata.get("event_date")
            _evb = msg.metadata.get("event_date_basis")
            _cmp = msg.metadata.get("compromise_date")
            event_date_val = _evd if isinstance(_evd, str) else None
            event_date_basis_val = _evb if isinstance(_evb, str) else None
            compromise_date_val = _cmp if isinstance(_cmp, str) else None
            # 主題アクター判定 (docs/subject_actor_attribution_design.md)。全経路 (RSS/grok)
            # の永続化直前 = 単一の判定点。失敗は NULL のまま = legacy 照合に fail-safe。
            try:
                from src.cti.actor_normalizer import load_actor_aliases

                _rf_raw = msg.metadata.get("routing_flags")
                _rf = _rf_raw if isinstance(_rf_raw, dict) else {}
                # D1 (C1 で蘇生, 2026-07-27): LLM 層の生入力を確保 (判定失敗でも入力観測は残す)。
                # named_primary_actor = 統合分類器の **ungated** 生アクター名 (辞書内外)。旧実装は
                # gated primary_actor_id を入れていたため cyber 記事で 0.35% に枯死していた。
                llm_primary_raw_val = str(_rf.get("named_primary_actor") or "").strip() or None
                llm_primary_conf_val = str(_rf.get("confidence") or "").strip() or None
                # 主題判定の根拠文 (2026-08-13 可視化)。特に「主題なし」の理由を記事詳細へ
                subject_rationale_val = str(_rf.get("subject_rationale") or "").strip() or None
                _det_raw = msg.metadata.get("detected_actor_ids")
                _det = [str(x) for x in _det_raw] if isinstance(_det_raw, list) else []
                _subj = determine_subject_actors(
                    titles=(
                        title or "",
                        str(msg.metadata.get("original_title") or ""),
                    ),
                    detected_actor_ids=_det,
                    llm_primary_actor_id=str(_rf.get("primary_actor_id") or ""),
                    llm_confidence=str(_rf.get("confidence") or "low"),
                    category=category,
                    registry=load_actor_aliases(),
                )
                subject_ids_val = ",".join(_subj.ids)
                subject_source_val = _subj.source
                subject_conf_val = _subj.confidence
            except Exception as e:  # noqa: BLE001 — 主題判定失敗で永続化を止めない
                _log.warning(
                    "subject_actor_determination_failed",
                    article_id=article_id,
                    error=str(e),
                )
        try:
            dedup_repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=article_id,
                    title=title,
                    url=display_url,
                    feed_title=article.feed_title,
                    # Source Identity Decoupling: 安定 source キーを永続化 (取り込み側は既に設定済)
                    feed_url=article.feed_url,
                    importance=importance,
                    category=category,
                    status=outcome["status"],  # type: ignore[arg-type]
                    failure_reason=(
                        str(outcome["failure_reason"]) if outcome.get("failure_reason") else None
                    ),
                    posted_channel=(
                        str(outcome["posted_channel"]) if outcome.get("posted_channel") else None
                    ),
                    dedup_key=dedup_key_val,
                    discord_message_id=(
                        str(outcome["discord_message_id"])
                        if outcome.get("discord_message_id")
                        else None
                    ),
                    discord_channel_id=(
                        str(outcome["discord_channel_id"])
                        if outcome.get("discord_channel_id")
                        else None
                    ),
                    summary=(str(outcome["summary"]) if outcome.get("summary") else None),
                    pmesii_p="P" in pmesii_set,
                    pmesii_m="M" in pmesii_set,
                    pmesii_e="E" in pmesii_set,
                    pmesii_s="S" in pmesii_set,
                    pmesii_i_infra="I-infra" in pmesii_set,
                    pmesii_i_cyber="I-cyber" in pmesii_set,
                    pmesii_p_env="P-env" in pmesii_set,
                    pmesii_t="T" in pmesii_set,
                    victim_sector_canonical=victim_sec_can,
                    victim_sector_raw=victim_sec_raw,
                    victim_country_iso=victim_cnt_iso,
                    victim_country_raw=victim_cnt_raw,
                    victim_country_scope=victim_cnt_scope,
                    is_ransomware=is_ransomware_val,
                    socio_political_intent=socio_intent,
                    intent_confidence=intent_conf,
                    remediation=remediation_val,
                    analyst_note=analyst_note_val,
                    socio_political_rationale=socio_rationale,
                    technical_axis_summary=technical_summary,
                    routing_rule_id=routing_rule_id_val,
                    routing_reason=routing_reason_val,
                    editorial_stance=(
                        str(outcome["editorial_stance"])
                        if outcome.get("editorial_stance")
                        else None
                    ),
                    # 記事の公開時刻 (RSS pubDate)。created_at は取得/処理時刻なので別保持。
                    published_at=article.published,
                    # 時間軸レイヤ b/c: 事象の実発生日 / 基準 / 侵害開始日 (報道時刻と分離)。
                    event_date=event_date_val,
                    event_date_basis=event_date_basis_val,
                    compromise_date=compromise_date_val,
                    # 主題アクター層 (言及と分離した「記事の主語」、PIR 照合の主題ゲート用)
                    subject_actor_ids=subject_ids_val,
                    subject_actor_source=subject_source_val,
                    subject_actor_confidence=subject_conf_val,
                    # D1: LLM 層の生入力 (辞書解決前) — 再導出・遡及帰属・監査の基盤
                    llm_primary_actor_raw=llm_primary_raw_val,
                    llm_primary_confidence=llm_primary_conf_val,
                    subject_actor_rationale=subject_rationale_val,
                    # 記事タイプ (judgment_classifier 由来、metadata 経由)。記事詳細で表示。
                    article_type=(
                        str(msg.metadata.get("article_type"))
                        if msg is not None and msg.metadata.get("article_type")
                        else None
                    ),
                ),
            )
            # Phase Diamond: IoC / CVE / TTP / malware_family / tool を article_entities
            # に永続化 (Threats tab の Capability / Infrastructure 軸表示 + 逆引き検索用)
            _persist_article_entities(
                dedup_repo=dedup_repo,
                article_id=article_id,
                msg=msg,
                feed_title=article.feed_title or "",
                subject_actor_ids=subject_ids_val,
                subject_actor_source=subject_source_val,
            )
            # Phase Diamond L2: trafilatura 取得本文を articles.body へ永続化
            # (entity 再抽出 / dictionary lookup の基盤)
            if msg is not None:
                body_raw = msg.metadata.get("_article_body")
                if isinstance(body_raw, str) and body_raw:
                    # 本文完全性 (2026-07-27): body_source を必ず添えて書く
                    # (body 非 NULL ⇒ body_source 非 NULL の不変条件、seam=update_article_body)。
                    _src = msg.metadata.get("_article_body_source")
                    body_source_val = str(_src) if isinstance(_src, str) and _src else "unknown"
                    _fr = msg.metadata.get("_extraction_failure_reason")
                    failure_reason_val = str(_fr) if isinstance(_fr, str) and _fr else None
                    try:
                        dedup_repo.update_article_body(
                            article_id,
                            body_raw,
                            source=body_source_val,
                            failure_reason=failure_reason_val,
                        )
                    except Exception as e:  # noqa: BLE001
                        _log.debug(
                            "article_body_persist_failed",
                            article_id=article_id,
                            error=str(e),
                        )
            else:
                # B3a: 抽出失敗 (msg=None、DegenerateBodyError) の状態を可視化する。body は
                # 書かず (NULL のまま) body_source + reason だけ残し、不可視だった抽出失敗
                # (BleepingComputer 等の 80 件/月) を検出可能にする。retry/KPI 挙動は不変。
                _state = _extraction_failure_state(str(outcome.get("failure_reason") or ""))
                if _state is not None:
                    # 取得側の理由 (http_error_403 等、orchestrator が明示キーで渡す) を
                    # 優先 — 「なぜ読めない本文か」の方が診断価値が高い。無ければ
                    # degenerate 分類 (block_page/body_too_short) を残す。
                    _ereason = outcome.get("extraction_failure_reason")
                    try:
                        dedup_repo.mark_extraction_state(
                            article_id,
                            body_source=_state[0],
                            failure_reason=str(_ereason) if _ereason else _state[1],
                        )
                    except Exception as e:  # noqa: BLE001
                        _log.debug(
                            "extraction_state_persist_failed",
                            article_id=article_id,
                            error=str(e),
                        )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "article_record_persist_failed",
                article_id=article_id,
                run_id=run_id,
                error=str(e),
            )

        # Phase B: terminal な content 失敗 (summarize/extract failed) は URL を seen 登録し、
        # RSS 経路 (自前 seen-state なし) での再取得ループを防ぐ。2 retry + salvage 後も
        # 失敗 = content 起因で永続的、cross-run retry は無益なため。post_failed (Discord
        # 障害 = transient) は除外して再試行余地を残す。watcher 経路は元から自前 seen で重複防止。
        # 監査 2026-07-05 P1: LLM の一時障害 (transient_failure=True) も除外 — Ollama 停止
        # 時間帯の記事が恒久ロストしていた。次 run の RSS 窓での再取得に委ねる。
        status_val = str(outcome.get("status") or "")
        if outcome.get("transient_failure"):
            _log.info(
                "transient_failure_url_not_marked_seen",
                article_id=article_id,
                status=status_val,
            )
        elif status_val in ("summarize_failed", "extract_failed"):
            try:
                dedup_repo.mark_url_seen(
                    url_hash=url_hash(article.url),
                    url=article.url,
                    article_id=article_id,
                    title=article.title,
                )
            except Exception as e:  # noqa: BLE001
                _log.debug("failed_article_seen_mark_failed", article_id=article_id, error=str(e))


def _append_pir_entities(
    entities: list[tuple[str, str]],
    *,
    msg: BriefingMessage,
    feed_title: str,
    actor_values: set[str],
    subject_actor_ids: str | None = None,
    subject_actor_source: str | None = None,
) -> None:
    """記事を現行 PIR config で inline 評価し ``("pir", pir_id)`` を append する (L1a)。

    監査 backlog 2026-07-05: PIR だけが日次 full-replace バッチで付与され、取込〜翌朝の
    freshness ラグで夕方 synthesis が PIR 盲目だった。他エンティティ同様その場で評価し、
    situation が開いた瞬間に pir_ids が載るようにする。バッチ (rebuild_pir_entities) と
    **同一の evaluate_pir_for_article** を使い挙動を一致させる。失敗しても本処理は止めない。
    """
    try:
        from src.pir.evaluator import evaluate_pir_for_article, has_match_criteria
        from src.pir.integration import get_pir_config

        body = msg.metadata.get("_article_body")
        article_data: dict[str, object] = {
            "title": msg.title or "",
            "summary": msg.summary or "",
            "body": body if isinstance(body, str) else "",
            "feed_title": feed_title,
            "victim_sector_canonical": msg.metadata.get("victim_sector_canonical"),
            "victim_country_iso": msg.metadata.get("victim_country_iso"),
            "article_type": msg.metadata.get("article_type"),
            # signal-first 述語 (match ツリー) が読む意味プロパティ。2026-07-23 以前は
            # 未供給で inline だけ legacy keyword 照合になり rebuild と乖離していた
            # (docs/pir_concept_llm_judge_design.md §4.5)。
            "category": msg.category,
            "socio_political_intent": msg.metadata.get("socio_political_intent"),
            "is_ransomware": bool(msg.metadata.get("is_ransomware")),
            # 主題アクター層: evaluator の actors/actor_nations を主題ゲートに切替える
            # (docs/subject_actor_attribution_design.md、None なら legacy 照合)
            "subject_actor_ids": subject_actor_ids,
            "subject_actor_source": subject_actor_source,
        }
        for pir in get_pir_config().priorities:
            if (
                pir.enabled
                and has_match_criteria(pir)
                and evaluate_pir_for_article(pir, article_data, actor_values)
            ):
                entities.append(("pir", pir.id))
    except Exception as e:  # noqa: BLE001 — PIR 評価失敗で entity 永続化を止めない
        _log.debug("inline_pir_tag_failed", error=str(e))


def _persist_article_entities(
    *,
    dedup_repo: RunHistoryRepository,
    article_id: str,
    msg: BriefingMessage | None,
    feed_title: str = "",
    subject_actor_ids: str | None = None,
    subject_actor_source: str | None = None,
) -> None:
    """Phase Diamond: 1 article の Diamond Model entities を永続化。

    保存対象:
        - **Adversary**: actor (canonical id、detected_actor_ids 由来。Phase 0 F2)
        - **Capability**: cve / ttp / malware_family / tool
        - **Infrastructure**: ioc_ip / ioc_domain / ioc_sha256 / ioc_sha1 / ioc_md5 / ioc_url

    Victim は articles 列 (victim_sector_canonical / victim_country_iso) に直接持つため対象外。
    """
    if msg is None:
        return
    entities: list[tuple[str, str]] = []

    # CVE / TTP は LLM 抽出 (msg.mitre_techniques) + 本文 regex の合算
    seen_cve: set[str] = set()
    seen_ttp: set[str] = set()
    for tech in msg.mitre_techniques or []:
        t = str(tech).strip().upper()
        if not t:
            continue
        if t.startswith("CVE-") and t not in seen_cve:
            seen_cve.add(t)
            entities.append(("cve", t))
        elif t.startswith("T") and any(c.isdigit() for c in t) and t not in seen_ttp:
            seen_ttp.add(t)
            entities.append(("ttp", t))

    # IoC (msg.iocs は本文から抽出された raw value list、ioc_type は値から判別)
    # Phase B-cal: briefing/summarizer.j2 は CVE を mitre_techniques ではなく iocs 配列に
    # 出力する。旧実装は mitre_techniques しか見ず、_classify_ioc_type にも cve 分岐が
    # 無かったため CVE entity がほぼ永続化されていなかった (監査: 322 記事中 cve=1)。
    # ここで iocs 内の CVE を拾って entity_type='cve' に正規化する。
    for ioc in msg.iocs or []:
        v = str(ioc).strip()
        if not v:
            continue
        cve_match = _CVE_RE.search(v)
        if cve_match:
            c = cve_match.group(0).upper()
            if c not in seen_cve:
                seen_cve.add(c)
                entities.append(("cve", c))
            continue
        kind = _classify_ioc_type(v)
        if kind:
            entities.append((kind, v))

    # CVE は summary 本文にも原文引用される (briefing/summarizer.j2 の指示)。iocs 配列が
    # 取りこぼした分の保険として summary からも regex で補完する。
    # ただし iocs/mitre が CVE を curate 済み (seen_cve 非空) の場合は summary scrape を
    # 行わない: summary は narrative なので過去事案・他社事例の CVE を文中で言及することがあり
    # (例: KDDI 侵害記事が過去の IIJ CVE-2025-42599 を背景説明)、それを本件 CVE として
    # 誤って紐付けてしまう。curate された iocs を信頼し、iocs が CVE を 1 件も拾えなかった
    # 場合のみ最後の保険として summary regex に fallback する。
    if not seen_cve:
        for c in _CVE_RE.findall(msg.summary or ""):
            cu = c.upper()
            if cu not in seen_cve:
                seen_cve.add(cu)
                entities.append(("cve", cu))

    # malware_family / tool (Phase 2: summarizer の新フィールド)。
    # 2026-06-20: 生 LLM 文字列のままだと表記ゆれ ("The Gentlemen"/"Thegentlemen")・tool 誤分類
    # ("Cobalt Strike")・generic noise ("ransomware") が混入し集計破綻 → malware_normalizer で
    # canonical 化 / tool 再分類 / drop する (Capability 頂点の curated 語彙化)。
    from src.cti.malware_normalizer import load_malware_normalizer

    mnorm = load_malware_normalizer()
    seen_cap: set[tuple[str, str]] = set()

    def _add_cap(etype: str, value: str) -> None:
        ck = (etype, value.lower())
        if value and ck not in seen_cap:
            seen_cap.add(ck)
            entities.append((etype, value))

    for fam in msg.metadata.get("malware_families", []) or []:
        disp, kind = mnorm.normalize(str(fam))
        if kind != "drop" and disp:
            _add_cap(kind, disp)  # kind = 'malware_family' or (誤分類なら) 'tool'
            # P2 (tagging survey): family → malware type の辞書導出 (決定論)。
            # is_ransomware boolean の一般化 — wiper/infostealer/ransomware は状況認識上
            # 意味が異なる。辞書 (malware_aliases.yaml type 列) 未宣言の family は付けない。
            if kind == "malware_family":
                mtype = mnorm.type_of(disp)
                if mtype:
                    _add_cap("malware_type", mtype)
    for tool in msg.metadata.get("tools", []) or []:
        disp, kind = mnorm.normalize_tool(str(tool))
        if kind != "drop" and disp:
            _add_cap(kind, disp)  # 未収録 tool は tool 維持、既知 malware のみ malware へ

    # P1 (tagging survey 全会一致 gap): CVE → NVD CPE 逆引きで影響ベンダ/製品を entity 化
    # (決定論、cache のみ参照 = LLM/network なし)。cache 未収録 (warming 中) は付かない —
    # backfill (scripts/backfill_derived_tags.py) が後日回収する。資産照合・パッチ優先度の軸。
    try:
        from src.tools.nvd_client import get_affected as _nvd_affected

        seen_av: set[str] = set()
        seen_ap: set[str] = set()
        for c in sorted(seen_cve):
            vendors, products = _nvd_affected(c)
            for v in vendors:
                if v and v.lower() not in seen_av and len(seen_av) < _MAX_AFFECTED_PER_ARTICLE:
                    seen_av.add(v.lower())
                    entities.append(("affected_vendor", v))
            for pr in products:
                if pr and pr.lower() not in seen_ap and len(seen_ap) < _MAX_AFFECTED_PER_ARTICLE:
                    seen_ap.add(pr.lower())
                    entities.append(("affected_product", pr))
    except Exception as e:  # noqa: BLE001 — 逆引き失敗で entity 永続化を止めない
        _log.debug("affected_lookup_failed", error=str(e))

    # Phase 2 (geo-map): victim_org (実際に攻撃された組織のみ)。地図 geocode の対象。
    seen_vorg: set[str] = set()
    for org in msg.metadata.get("victim_orgs", []) or []:
        v = str(org).strip()
        if not v or v.lower() in seen_vorg:
            continue
        # 判定基準は「ベンダ / ソフトメーカーを含めない」と明記しているが、30 日実測で
        # Google 19 / Microsoft 17 / Cisco 6 等 49 件が混入し、地図と被害国 KPI に
        # 偽の点を出していた (バグバウンティの報奨金記事・製品対応記事)。
        # ⭐ 指示では止まらないので決定論で遮断する。
        # ⚠ category が breach/incident なら遮断しない = ベンダ自身の侵害記事は残す。
        if is_vendor_noise(v, msg.category):
            _log.info("victim_org_vendor_filtered", value=v, category=msg.category)
            continue
        seen_vorg.add(v.lower())
        entities.append(("victim_org", v))

    # #7 (geo-map 精密化): victim_city (被害組織の所在都市、明示時のみ)。都市レベル geocode の対象。
    _vcity = str(msg.metadata.get("victim_city", "") or "").strip()
    if _vcity:
        entities.append(("victim_city", _vcity))

    # 地政学情勢ブリッジ: 関与国 (ISO)。entity_type='involved_country' で永続化し、
    # actor.nation 経由のサイバー↔地政学相関 (情勢把握) の join key にする。
    _inv = msg.metadata.get("involved_country_isos")
    seen_inv: set[str] = set()
    if isinstance(_inv, list):
        for iso in _inv:
            v = str(iso).strip()
            if v and v.lower() not in seen_inv:
                seen_inv.add(v.lower())
                entities.append(("involved_country", v))

    # 言及タグ (決定論、mention_tagger): mentioned_country (タイトル/要約の国名言及。
    # involved_country=当事国とは type 分離し意味論を混ぜない) + campaign (Operation <Name>)。
    # involved_country の LLM 記入漏れ (地政学/軍事記事の 4 割超) の補完で、Situation Ledger の
    # 割当キー・国家系ビューの将来利用に供する。LLM プロンプトは変更しない (過負荷回避)。
    from src.cti.mention_tagger import derive_mention_entities

    entities.extend(
        derive_mention_entities(
            title=msg.title or "", summary=msg.summary or "", involved_isos=seen_inv
        )
    )

    # Phase 0 (F2): Adversary 軸を entity 永続化 (学習記憶の礎石)。
    # detected_actor_ids は actor_registry.find_all(body) の canonical id。これを保存する
    # ことで pir/evaluator の entity_type='actor' query が活性化し、actor retrospection が
    # 毎回の regex 再走査ではなく DB の事実になる。
    detected_actor_ids = msg.metadata.get("detected_actor_ids")
    actor_values: set[str] = set()
    if isinstance(detected_actor_ids, list):
        seen_actor: set[str] = set()
        for aid in detected_actor_ids:
            a = str(aid).strip()
            if a and a not in seen_actor:
                seen_actor.add(a)
                actor_values.add(a.lower())
                entities.append(("actor", a))

    # F5 alias 使用統計: 本文で実際にヒットした名前を記事単位で記録 (INSERT OR IGNORE の
    # 記事粒度 PK により run 横断の重複行でも二重計上しない)。失敗しても取込は殺さない。
    matches = msg.metadata.get("detected_actor_matches")
    if isinstance(matches, list) and matches:
        try:
            pairs = [
                (str(m[0]), str(m[1]))
                for m in matches
                if isinstance(m, list | tuple) and len(m) == 2
            ]
            dedup_repo.record_alias_usage(article_id, pairs)
        except Exception as e:  # noqa: BLE001 — 統計欠落は取込失敗の理由にならない
            _log.warning("alias_usage_persist_failed", article_id=article_id, error=str(e))

    # Actor Recall Layer (Part C2): 新興アクター候補を **暗定** entity で永続化 (確定と別 type)。
    # 即可視 (検索 facet) にしつつ、裏取り集計 (propose_emerging_actors) で proposal に上げる。
    provisional = msg.metadata.get("provisional_actor_candidates")
    if isinstance(provisional, list):
        seen_prov: set[str] = set()
        for cand in provisional:
            key = str(cand.get("key", "")).strip() if isinstance(cand, dict) else ""
            if key and key not in seen_prov:
                seen_prov.add(key)
                entities.append(("actor_provisional", key))

    # L1a: PIR タグを取込時 inline で付与 (他エンティティと同じ扱い)。actor_values は
    # 直上で収集した actor entity 値 (バッチの _fetch_article_actor_values と同一源)。
    _append_pir_entities(
        entities,
        msg=msg,
        feed_title=feed_title,
        actor_values=actor_values,
        subject_actor_ids=subject_actor_ids,
        subject_actor_source=subject_actor_source,
    )

    if not entities:
        return
    try:
        added = dedup_repo.add_article_entities(article_id, entities)
        _log.debug(
            "article_entities_persisted",
            article_id=article_id,
            attempted=len(entities),
            inserted=added,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "article_entities_persist_failed",
            article_id=article_id,
            error=str(e),
        )


# CVE-YYYY-NNNN[N] (1999-2099 想定、ioc_extractor._CVE_RE と整合)
_CVE_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)
# P1: 1 記事あたりの affected_vendor / affected_product 上限 (CPE 列挙の暴走防止)
_MAX_AFFECTED_PER_ARTICLE = 6

# IoC 値 → entity_type の簡易分類 (ioc_extractor の判定と整合)
_IOC_RE_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?$")
_IOC_RE_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_IOC_RE_SHA1 = re.compile(r"^[a-fA-F0-9]{40}$")
_IOC_RE_MD5 = re.compile(r"^[a-fA-F0-9]{32}$")
_IOC_RE_URL = re.compile(r"^https?://", re.IGNORECASE)


def _classify_ioc_type(value: str) -> str | None:
    if _IOC_RE_IPV4.match(value):
        return "ioc_ip"
    if _IOC_RE_SHA256.match(value):
        return "ioc_sha256"
    if _IOC_RE_SHA1.match(value):
        return "ioc_sha1"
    if _IOC_RE_MD5.match(value):
        return "ioc_md5"
    if _IOC_RE_URL.match(value):
        return "ioc_url"
    # domain (簡易: ドット含み英数 / 大文字小文字、tld 2 字以上)
    if re.match(r"^[a-zA-Z0-9.\-_]+\.[a-zA-Z]{2,}$", value):
        return "ioc_domain"
    return None


def _persist_feed_health(source: ArticleSource, dedup_repo: RunHistoryRepository | None) -> None:
    """ソースの per-source fetch 結果を source_fetch_health へ upsert する。

    監査 2026-07-05 P2: ソースの恒常死 (fetch 失敗 / 抽出ゼロ) は run 成否から
    見えない — heartbeat の沈黙検出と購読画面の層別ラベルが読む観測点をここで残す。

    seam は ``last_fetch_health()`` (transport 横断、2026-08-02)。RSS だけでなく
    html_scraper / sitemap watcher も同じ形で死活を残すことで、「低頻度で静か」と
    「壊れて無音」を全 transport で区別できるようにする。旧 ``last_results`` 経路は
    それを持たない source (テスト用の簡易 fake 等) のための後方互換。
    """
    if dedup_repo is None:
        return
    try:
        health_fn = getattr(source, "last_fetch_health", None)
        if callable(health_fn):
            outcomes = [o.as_row() for o in health_fn()]
        else:
            results = getattr(source, "last_results", None)
            if not results:
                return
            outcomes = [
                (r.feed.url, r.feed.name, r.error is None, r.error or "", len(r.articles))
                for r in results
            ]
        if not outcomes:
            return
        dedup_repo.upsert_source_fetch_health(outcomes)
    except Exception as e:  # noqa: BLE001 — 観測の失敗で収集を止めない
        _log.warning("feed_health_persist_failed", error=str(e))
