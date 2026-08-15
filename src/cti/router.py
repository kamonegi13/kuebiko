"""ルーティングエンジン (Phase 5L-1: RoutingDecision 構造化版)。

RoutingSignals → RoutingDecision (channel + rule_id + reason + signals_snapshot)
の決定。Inoreader / Grok 両経路から呼ばれる共通モジュール。

設計 (B1: 役割別 3 層に整理。順序は従来 R1-R8 ラダーと同一で挙動不変):
    - **優先層** (``_route_priority``): トピック優先度による escalation。PIR が literal
      では表現しきれない高精度 signal (KEV/0day/japan-critical/known-APT/apt_leak/grok)。
    - **衛生層** (``_apply_hygiene``): データ衛生による **降格のみ** (形式/低信頼/propaganda)。
      トピックと直交する固定ガードレール (PIR では制御しない)。
    - **importance 層** (``_route_importance``): high/medium の brief 集約 + cap + fallback。
    - 各 rule に安定 ID (R1〜R8) を付与し、log/UI 集計の grain を提供。
    - **rule_id を変更する際は UI/集計の互換性を考慮**

PIR の優先度は triage の importance 評価 (PIR_DRIVEN_TRIAGE) を経由して routing に
届く。R0 (PIR target_channel の直接 override) は 2026-06-13 に撤去 — チャンネル
決定権は routing に一本化 (PIR=関心の定義と評価 / routing=配信、の役割分離)。

Phase 5N で追加: R8 (apt_leak category → alert)
Phase 5T-V で追加: R5b (source_low_reliability), R6.cap_demote (brief 24h cap)
Phase B1: PIR-driven 整理 — 優先/衛生(降格専用)/importance の 3 層に再構成。
"""

from __future__ import annotations

import os

from src.config_loader import SourceQualityConfig, load_source_quality
from src.cti.routing_decision import RoutingDecision
from src.cti.routing_signals import RoutingSignals
from src.logging_config import get_logger

_log = get_logger(__name__)

# 案 B / Stage 1: config 駆動 rule engine の有効化フラグ (既定 0 = 旧ハードコード ladder)。
_RULES_ENGINE_ENV = "ROUTING_RULES_ENGINE"


def is_rules_engine_enabled() -> bool:
    """config/delivery/routing_rules.yaml の rule engine を使うか (既定 False)。"""
    return os.environ.get(_RULES_ENGINE_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# Phase 5T-V: source_quality.yaml の読み込みキャッシュ (process-life)
_SOURCE_QUALITY_CACHE: SourceQualityConfig | None = None


# source_quality も運用 config として DB を正とする (案 B 拡張)。
SOURCE_QUALITY_CONFIG_KEY = "source_quality"


def _load_source_quality_db_first() -> SourceQualityConfig:
    """DB (config_store) 優先で source_quality をロード。未保存/障害時は yaml seed。"""
    raw: object = None
    try:
        from src.storage.config_store import get_config

        raw = get_config(SOURCE_QUALITY_CONFIG_KEY)
    except Exception as e:  # noqa: BLE001 — DB 障害で routing を壊さない
        _log.warning("source_quality_db_read_failed", error=str(e))
        raw = None
    if isinstance(raw, dict):
        try:
            return SourceQualityConfig(**raw)
        except Exception as e:  # noqa: BLE001 — 破損 DB 値は seed に degrade
            _log.warning("source_quality_db_parse_failed", error=str(e))
    return load_source_quality()


def get_source_quality(force_reload: bool = False) -> SourceQualityConfig:
    """source_quality をプロセスキャッシュ越しに取得 (Phase 5T-V / DB 化)。

    DB を正とし、未保存時のみ config/sources/source_quality.yaml (seed) に fallback。
    Web UI で編集された場合は force_reload=True で再読込する。
    """
    global _SOURCE_QUALITY_CACHE
    if force_reload or _SOURCE_QUALITY_CACHE is None:
        _SOURCE_QUALITY_CACHE = _load_source_quality_db_first()
    return _SOURCE_QUALITY_CACHE


def seed_source_quality_if_absent() -> bool:
    """DB 未投入なら source_quality.yaml seed を version 1 として取り込む (起動時)。"""
    try:
        from src.storage.config_store import seed_config_if_absent

        return seed_config_if_absent(
            SOURCE_QUALITY_CONFIG_KEY,
            load_source_quality().model_dump(mode="json"),
            note="初期 seed (config/sources/source_quality.yaml)",
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("source_quality_seed_failed", error=str(e))
        return False


def route(signals: RoutingSignals) -> RoutingDecision:
    """RoutingSignals から RoutingDecision を決定する (Phase 5L-1)。

    チャンネル決定権は routing (engine / legacy ladder) に一本化されている。
    PIR の優先度は triage の importance 評価 (PIR_DRIVEN_TRIAGE) を経由して
    ここに届く (R0 = PIR target_channel の直接 override は 2026-06-13 撤去。
    全 21 PIR が auto のまま一度も発動せず、チャンネル体系 = 緊急度ベースと
    噛み合わない構造的余剰だったため。詳細は CLAUDE.md §13)。

    legacy ルール優先順 (上から評価、最初にマッチした rule を返す):

        R2. 即応 → alert (japan_critical+APT, breaking+KEV/0day)
        R3. Inoreader 日本関連弱信号 → japan_watch
        R4. recap/tutorial/opinion/press/research → watch
        R5. 信頼性 all-low → watch
        R5b. source_low_reliability + demote category → watch (Phase 5T-V)
        R6. importance high/medium + security_relevant → brief
              cap 到達後 + importance=medium → watch (R6.cap_demote, Phase 5T-V)
        R7. fallback → watch
    """
    snap = _signals_snapshot(signals)
    sq = get_source_quality()  # source_quality.yaml (R3.5 / R5b / R6 が参照)

    return _route_base(signals, sq, snap)


def _route_base(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
) -> RoutingDecision:
    """channel の base 決定。

    案 B / Stage 1: ``ROUTING_RULES_ENGINE=1`` のとき config 駆動 rule engine
    (DB config_store の routing rules) を使う。engine が None を返す (config 破損等)
    場合は fail-safe で legacy。既定 (flag 0) は従来どおり legacy hardcoded ladder。
    旧 R1 grok 分岐 (grok_daily 集約) は Grok JSONL 化 (theme→channel を briefing
    metadata で直接指定) に伴い 2026-06-13 撤去。
    """
    if is_rules_engine_enabled():
        from src.cti.routing_rules import evaluate_routing_rules

        decision = evaluate_routing_rules(signals, sq, snap)
        if decision is not None:
            return decision
    return _route_legacy(signals, sq, snap)


def _route_legacy(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
) -> RoutingDecision:
    """legacy の決定層。役割で 3 段に分離 (順序は従来 R1-R8 と同一、挙動不変)。

    1. ``_route_priority``   — トピック優先度による escalation (PIR 意図を高精度 signal で補完)
    2. ``_apply_hygiene``    — データ衛生による **降格のみ** (形式 / 低信頼 / propaganda)
    3. ``_route_importance`` — importance による brief 集約 + cap 量制御 + fallback

    R3.5 (高脅威 brief) が衛生 R4 (形式降格) より前 = 高脅威が形式降格に勝つ tier も保持。
    優先層が None を返したときだけ衛生層、衛生層が None のときだけ importance 層に進む
    ことで、従来の単一ラダー (R1→…→R7) と同一の決定を再現する。
    """
    strong = _route_priority(signals, sq, snap)
    if strong is not None:
        return strong
    hygiene = _apply_hygiene(signals, sq, snap)
    if hygiene is not None:
        return hygiene
    return _route_importance(signals, sq, snap)


def _route_priority(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
) -> RoutingDecision | None:
    """優先層: トピック優先度による escalation。該当なしは None (衛生層へ委譲)。

    本関数の escalation は、PIR が literal keyword では表現しきれない高精度 signal
    (CISA KEV カタログ照合 / 0day / japan-critical / known-APT / apt_leak) を
    担う層。「過去ルールの寄せ集め」ではなく PIR 意図の高精度版という位置づけ。

    旧 R1/R1.5 (Grok ソース → grok_daily 集約) は Grok JSONL 化に伴い 2026-06-13
    撤去 (Grok briefing は metadata.target_channel を直接持ち本関数を経由しない)。
    """
    # ---------- R8: APT leak (PIR 3) → alert (Phase 5N) ----------
    # APT 組織自体の内部資料・ツール・通信内容のリーク事案 (i-Soon, NTC Vulkan 等)。
    # アトリビューション材料として CTI 業界では超高価値、即応 ch にエスカレート。
    # category は LLM が briefing/summarizer.j2 で判定する。
    if signals.is_apt_leak and signals.article_type not in (
        "recap",
        "tutorial",
        "opinion",
        "press",
    ):
        return RoutingDecision(
            channel="alert",
            rule_id="R8.alert_apt_leak",
            reason="APT 関連組織の内部リーク (PIR 3)",
            signals_snapshot=snap,
        )

    # ---------- R2: Inoreader 即応条件 ----------
    # Phase B-cal2 (2026-06-04): alert escalation は cyber-threat category に限定する。
    # 背景: research / geopolitical / other / policy は KEV/0day/APT 語が "話題として"
    # 本文に登場するだけ (例: 脅威を論じる arXiv 論文の "ゼロデイ"/"in-the-wild"、TI
    # サービス紹介の "ゼロデイ") で R2 を誤発火させ、低重要度記事が alert に漏れていた。
    # high_threat_brief_categories (= 実 actionable な脅威種別: apt/vuln/malware/incident/
    # breach/advisory) のみ alert 適格とする。B-cal の「research/geopolitical は watch 固定」
    # 方針は brief (R3.5/R6) のみに適用されていたが、alert (R2) にも拡張する。
    if (
        signals.article_type not in ("recap", "tutorial", "opinion", "press")
        and signals.category in sq.high_threat_brief_categories
    ):
        if signals.mentions_japan_critical and signals.has_known_apt:
            return RoutingDecision(
                channel="alert",
                rule_id="R2.inoreader.alert_japan_critical_apt",
                reason="inoreader japan_critical + known APT",
                signals_snapshot=snap,
            )
        if signals.article_type == "breaking" and (
            signals.has_kev_or_active_exploit or signals.is_zero_day
        ):
            return RoutingDecision(
                channel="alert",
                rule_id="R2.inoreader.alert_breaking_kev",
                reason="inoreader breaking + KEV/0day",
                signals_snapshot=snap,
            )

    # ---------- R3: Inoreader 日本関連弱信号 ----------
    if signals.is_japan_security_relevant and signals.article_type not in (
        "recap",
        "tutorial",
        "opinion",
        "press",
    ):
        return RoutingDecision(
            channel="japan_watch",
            rule_id="R3.inoreader.japan_watch",
            reason="inoreader japan-relevant",
            signals_snapshot=snap,
        )

    # ---------- R3.5: 高重要度の脅威 writeup → brief (style 降格より優先) ----------
    # Phase B-cal: importance=high かつ実 actionable な脅威カテゴリの記事は、
    # 技術解析・考察スタイルで article_type が research/opinion に分類されても
    # watch に埋めず朝読 (brief) に確実に上げる。
    # (背景: R4 は style で watch 降格するため、高価値な APT/malware 詳細解析が
    #  significance より先に style で消えていた = 監査で 88 件の high→watch 流出を確認)
    # japan 関連 (R3) / alert 級 (R2/R8) は既に上で確定済みなので、ここに来るのは
    # 「日本標的でない高重要度脅威」= まさに brief に欲しい記事。
    if (
        signals.importance == "high"
        and signals.category in sq.high_threat_brief_categories
        and signals.editorial_stance != "propaganda"  # propaganda は R5b で降格を維持
    ):
        return RoutingDecision(
            channel="brief",
            rule_id="R3.5.high_threat_brief",
            reason=f"high importance threat (category={signals.category})",
            signals_snapshot=snap,
        )

    return None  # 優先層に escalation 該当なし → 衛生層へ


def _apply_hygiene(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
) -> RoutingDecision | None:
    """衛生層: データ衛生による **降格のみ** (→ watch)。該当なしは None (importance 層へ)。

    トピック優先度とは直交する固定ガードレール: 形式 (recap/opinion 等) / 信頼性
    all-low / propaganda framing。**どの PIR に該当しても効く** べき品質ルールなので、
    PIR では制御せず常に固定。優先層 (escalation) を通過しなかった記事にのみ適用される。
    """
    # ---------- R4: 観察対象に降格 (article_type) ----------
    if signals.article_type in ("recap", "tutorial", "opinion", "press", "research"):
        return RoutingDecision(
            channel="watch",
            rule_id="R4.watch_article_type_demote",
            reason=f"article_type={signals.article_type}",
            signals_snapshot=snap,
        )

    # ---------- R5: 信頼性 all-low 降格 ----------
    if signals.confidence_all_low:
        return RoutingDecision(
            channel="watch",
            rule_id="R5.watch_low_confidence",
            reason="all-low confidence demote",
            signals_snapshot=snap,
        )

    # ---------- R5b: editorial_stance=propaganda → watch 強制降格 ----------
    # Phase B-R5b 改訂: LLM が judge した content-based 判定が primary。
    # source 識別子は使わず、記事の rhetorical 性質 (一方的 framing) で判定。
    # 国営メディアでも factual_report なら降格しない (公平な content-based 判定)。
    if signals.editorial_stance == "propaganda":
        return RoutingDecision(
            channel="watch",
            rule_id="R5b.propaganda_demote",
            reason=(
                f"editorial_stance=propaganda detected (feed_hint={signals.feed_title or '-'})"
            ),
            signals_snapshot=snap,
        )

    # (Phase B-cal 2026-05-30: R5b-fallback (source-based low_reliability_feeds 降格) を撤去。
    #  DB 監査で content-based stance が安定 (unknown 0.78% / propaganda 降格 100% / 低信頼feed
    #  の unknown は月1件) と確認でき、fallback は実質死にコードだったため。routing は完全
    #  content-based 化した。source 識別子で channel を変える要素はゼロ。)

    return None  # 衛生降格に該当なし → importance 層へ


def _route_importance(
    signals: RoutingSignals,
    sq: SourceQualityConfig,
    snap: dict[str, object],
) -> RoutingDecision:
    """importance 層 (弱い優先): high/medium を brief に集約 + cap 量制御 + fallback。

    衛生層を通過した記事のみ到達するため、ここでの brief 集約は形式/品質を満たした
    記事に限られる (R6 が衛生 R4/R5/R5b より後ろにあった従来 tier をそのまま保持)。
    """
    # ---------- R6: 朝読 (brief) ----------
    if signals.importance in ("high", "medium") and signals.is_security_relevant:
        # Phase 5T-V: brief 24h cap。importance=high は cap 無視、medium は降格。
        if (
            sq.brief_cap_24h > 0
            and signals.brief_count_24h_snapshot >= sq.brief_cap_24h
            and signals.importance == "medium"
        ):
            return RoutingDecision(
                channel="watch",
                rule_id="R6.cap_demote",
                reason=(
                    f"brief_cap_24h={sq.brief_cap_24h} reached "
                    f"(count={signals.brief_count_24h_snapshot}), medium demoted"
                ),
                signals_snapshot=snap,
            )
        return RoutingDecision(
            channel="brief",
            rule_id="R6.brief_importance",
            reason=f"importance={signals.importance} security-relevant",
            signals_snapshot=snap,
        )

    # ---------- R7: フォールバック ----------
    return RoutingDecision(
        channel="watch",
        rule_id="R7.fallback",
        reason="fallback",
        signals_snapshot=snap,
    )


def _signals_snapshot(signals: RoutingSignals) -> dict[str, object]:
    """RoutingDecision に同梱する判定根拠の主要シグナル。

    debug/UI で grep する用途に絞り、IOC や本文といった重い情報は含めない。
    """
    return {
        "source": signals.source,
        "article_type": signals.article_type,
        "importance": signals.importance,
        "mentions_japan": signals.mentions_japan,
        "mentions_japan_critical": signals.mentions_japan_critical,
        "is_japan_security_relevant": signals.is_japan_security_relevant,
        "has_kev_or_active_exploit": signals.has_kev_or_active_exploit,
        "is_zero_day": signals.is_zero_day,
        "has_known_apt": signals.has_known_apt,
        "is_security_relevant": signals.is_security_relevant,
        "confidence_all_low": signals.confidence_all_low,
        "is_apt_leak": signals.is_apt_leak,  # Phase 5N
    }
