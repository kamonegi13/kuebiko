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
