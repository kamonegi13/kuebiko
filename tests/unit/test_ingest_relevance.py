"""src.cti.ingest_relevance のテスト (2026-10-08、M4)。

docs/importance_relevance_redesign.md §6b: s23 投入前の安全網。タイトル+概要のみ・
再現率優先 (過剰検出歓迎) の粗いヒント判定を固定する。
"""

from __future__ import annotations

import pytest

from src.cti.ingest_relevance import ingest_relevance_hint, invalidate_core_sir_keyword_cache


def setup_function() -> None:
    invalidate_core_sir_keyword_cache()


def test_japan_mention_fires() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="日本の重要インフラ企業が不正アクセスを受けた",
        summary_preview="被害の詳細は調査中。",
    )
    assert hint.fired
    assert "jp" in hint.reasons


def test_watched_nation_name_fires() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="中国政府系ハッカーによる欧州通信事業者への攻撃",
        summary_preview="詳細は調査中。",
    )
    assert hint.fired
    assert "nation:CN" in hint.reasons


def test_watched_nation_apt_alias_fires() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="Volt Typhoon の新たな事前配置キャンペーンが確認された",
        summary_preview="米重要インフラを標的にした活動が継続している。",
    )
    assert hint.fired
    assert any(r.startswith("actor:") for r in hint.reasons)


def test_core_sir_keyword_fires() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="ランサムウェア被害で病院が業務停止",
        summary_preview="複数の自治体にも影響が広がっている。",
    )
    assert hint.fired
    assert any(r.startswith("sir:") for r in hint.reasons)


def test_unrelated_article_does_not_fire() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="新しいパスワード管理アプリのレビュー",
        summary_preview="使いやすさと価格を比較した。",
    )
    assert not hint.fired
    assert hint.reasons == ()


def test_english_japan_alias_fires() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="Japan-based manufacturer discloses data breach",
        summary_preview="The incident affected an undisclosed number of customers.",
    )
    assert hint.fired
    assert "jp" in hint.reasons


def test_reasons_are_deduplicated() -> None:
    hint = ingest_relevance_hint(
        feed="Example",
        title="日本と日本企業の二重言及テスト",
        summary_preview="日本国内でも影響が確認された。",
    )
    assert hint.reasons.count("jp") == 1


class TestDomesticJapanCues:
    """国名の語が無い国内の地名・組織・媒体の手がかり (2026-10-08)。平たい triage で low に
    なる国内の小さな事案を取り込みで落とさないため。"""

    @pytest.mark.parametrize(
        "title",
        [
            "さいたま市、「支援措置対象者」の住所情報を相手方へ漏えい",
            "長和町ケーブルテレビサイトに不正アクセス",
            "大阪の病院で電子カルテが停止",
            "国内の製造業を狙うフィッシングが増加",
        ],
    )
    def test_domestic_place_or_word_fires(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert hint.fired and "jp:domestic" in hint.reasons

    def test_domestic_feed_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.cti import ingest_relevance

        monkeypatch.setattr(ingest_relevance, "_jp_feed_names", lambda: frozenset({"国内の媒体"}))
        hint = ingest_relevance_hint(
            feed="国内の媒体", title="社員が業務用パソコンを紛失", summary_preview=""
        )
        assert "jp:feed" in hint.reasons

    @pytest.mark.parametrize("title", ["暗号資産市場が急落", "攻撃と防御の区別が難しい"])
    def test_words_that_only_look_like_places_do_not_fire(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert "jp:domestic" not in hint.reasons

    def test_foreign_routine_patch_does_not_fire(self) -> None:
        hint = ingest_relevance_hint(
            feed="any", title="Microsoft patches Outlook bug in monthly update", summary_preview=""
        )
        assert not hint.fired


def test_watched_capital_names_fire() -> None:
    hint = ingest_relevance_hint(
        feed="any", title="CIA 長官によるモスクワ訪問の背景", summary_preview=""
    )
    assert "nation:RU" in hint.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Uniqlo sees profits soar in China despite Beijing-Tokyo tensions",
        "Bruce Lee ballet to lead WestK performance season in China",
    ],
)
def test_watched_nation_without_security_context_does_not_fire(title: str) -> None:
    hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
    assert not any(r.startswith("nation:") for r in hint.reasons)


def test_watched_nation_with_security_context_fires() -> None:
    hint = ingest_relevance_hint(
        feed="any", title="ロシア、ウクライナのエネルギー施設をミサイルで攻撃", summary_preview=""
    )
    assert "nation:RU" in hint.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Securing the United States Bulk-Power System",
        "Anthropic rolls out program to secure critical infrastructure",
        "Senate bill would push DOD to oversee commercial frontier AI models",
    ],
)
def test_cyber_policy_topics_fire_without_nation(title: str) -> None:
    hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
    assert "cyber_policy" in hint.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Hillsides turn green as Lüliang continues to improve in China",
        "Crime drama leads China's National Day box office",
        "Through a Serbian lens, a different China unfolds in Hainan",
    ],
)
def test_nation_only_without_security_context_does_not_fire(title: str) -> None:
    hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
    assert not hint.fired


def test_generic_actor_alias_without_security_context_does_not_fire() -> None:
    hint = ingest_relevance_hint(
        feed="any",
        title="Clean economy co-operation agenda for India and the EU",
        summary_preview="",
    )
    assert not any(r.startswith("actor:") for r in hint.reasons)


def test_nation_with_tech_policy_context_fires() -> None:
    hint = ingest_relevance_hint(
        feed="any",
        title="Pentagon unveils quantum computing push as China rivalry grows",
        summary_preview="",
    )
    assert "nation:CN" in hint.reasons


def test_cyber_policy_term_does_not_fire_alone_on_preprint_feed() -> None:
    hint = ingest_relevance_hint(
        feed="arXiv cs.CR", title="A new quantum-resistant encryption scheme", summary_preview=""
    )
    assert "cyber_policy" not in hint.reasons


class TestSecurityContextNationHintFixR2b:
    """2026-10-11 (R2b, data/eval/r2_report.md): `_SECURITY_CONTEXT` の nation ヒントが
    国営メディアの生活・観光記事に誤発火していた 2 つの原因 (頭字語に単語境界が無い /
    government・visit・summit・talks・diplomat・war 等が広すぎる) を固定する。"""

    @pytest.mark.parametrize(
        "title",
        [
            # 頭字語の単語境界なし + IGNORECASE により "financial"/"transaction" の部分
            # 文字列 (cia / nsa) に CIA/NSA が誤って当たっていた
            "China's financial markets rally as Beijing eyes new growth targets",
            "Cross-border transaction volumes surge between China and partner nations",
            # government/visit/summit/talks/diplomat/war が広すぎて生活・観光記事に反応していた
            "Panda cub mistaken for stray bear returns home as local government hails reunion",
            "Tourists visit Beijing's Forbidden City ahead of National Day holiday",
            "Elephant dung notebook craze delights visitors at Yunnan nature reserve",
            "Taxi driver returns lost wallet to grateful tourist in Shanghai",
            "ロシア企業の会談記録、観光客誘致策を発表",
            "北京市当局、花火大会の日程を発表",
        ],
    )
    def test_lifestyle_tourism_articles_do_not_fire_nation_hint(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert not any(r.startswith("nation:") for r in hint.reasons)

    @pytest.mark.parametrize(
        "title",
        [
            # 国家系 APT・サイバー作戦 (cyber/hack/attack 等の語で依然発火すべき)
            "China-linked hackers breach European telecom networks",
            "北朝鮮のサイバー部隊、暗号資産取引所を標的に攻撃",
            "ロシア軍、ウクライナのエネルギー施設にミサイル攻撃",
            "イランの諜報機関がスパイ活動を展開との報告",
            # 頭字語は単語単位なら依然発火する (区切りがあれば誤爆ではない)
            "CIA report ties Beijing-based group to espionage campaign",
            "FBI と NSA、ロシア発のランサムウェア集団を制裁対象に指定",
            # 制裁・輸出規制は国家系の CTI で頻出
            "対北朝鮮制裁、ミサイル開発関連の輸出規制を強化",
        ],
    )
    def test_cti_relevant_articles_still_fire_nation_hint(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert any(r.startswith("nation:") for r in hint.reasons)

    def test_japan_reason_unaffected_by_security_context_fix(self) -> None:
        """`_japan_reason` は `_SECURITY_CONTEXT` を参照しない独立経路 — 修正の影響を受けない。"""
        hint = ingest_relevance_hint(
            feed="any",
            title="北京市当局、花火大会の日程を発表",
            summary_preview="日本からの観光客も増加している。",
        )
        assert "jp" in hint.reasons


class TestSecurityContextIntelligenceAndSpyFixR2bFollowup:
    """2026-10-11 追加確認 (data/eval/r2b_report.md の追加調査): 修正後もなお残った非 CTI 誤発火を
    本文まで読んで分類した結果、最大要因は bare `intelligence` が "artificial intelligence" (AI
    業界ニュース) に誤爆すること (shadow 13 件中 5 件)。次点は `spy` が "no more spying" のような
    貿易交渉の慣用句的引用に反応すること。`intelligence` は agency/service/officer 等の文脈語を
    要求し、`spy` は削除する (espionage は残すため取りこぼしなし)。"""

    @pytest.mark.parametrize(
        "title",
        [
            # "intelligence" = artificial intelligence (AI) の誤爆 (本物の国家情報機関ではない)
            "China hails 'constructive' trade talks with EU as both sides seek ties in AI,"
            " new energy and artificial intelligence cooperation",
            "US AI firm Anthropic boosts artificial intelligence adoption as China eyes"
            " language model access",
            "Modi and Xi can't bridge the India-China divide on artificial intelligence"
            " governance by themselves",
            # "spy" が貿易交渉の慣用句的引用に反応する誤爆
            "EU is its own worst enemy: China's envoy says bloc wants 'no more dumping,"
            " no more spying and fair competition'",
        ],
    )
    def test_ai_and_spying_idiom_do_not_fire_nation_hint(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert not any(r.startswith("nation:") for r in hint.reasons)

    @pytest.mark.parametrize(
        "title",
        [
            # intelligence agency/service/officer は本物の情報機関なので引き続き発火すべき
            "China's intelligence agency accused of running influence operation in Europe",
            "Russia intelligence officer identified as handler of European hacking ring",
            "North Korea intelligence service linked to cryptocurrency theft campaign",
            # espionage は残すので削除の影響を受けない
            "China national charged with espionage against defense contractor",
        ],
    )
    def test_intelligence_agency_and_espionage_still_fire_nation_hint(self, title: str) -> None:
        hint = ingest_relevance_hint(feed="any", title=title, summary_preview="")
        assert any(r.startswith("nation:") for r in hint.reasons)
