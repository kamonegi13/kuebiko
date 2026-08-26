"""数量・日付の実在検査 (src/eventnews/quantities.py)。

識別子関門は CVE / IP / バージョンしか見ておらず、数量と日付が抜けていた。
そこを通ってモデルの推論値が「報じられている内容」として出た::

    原文 「14.2 million …」「12 million …」 → 生成「2,620万件」  (足し算)
    原文 「on Saturday」                    → 生成「8月23日」    (曜日からの割り出し)

⚠ **照合は言語をまたぐ**。プロンプトへ渡すのは原文 (多くは英語) で生成は日本語
なので、素朴に数字を探すと正しい行を大量に落とす (実測で 27 → 3 まで詰めた)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews import quantities
from src.eventnews.models import FactItem, MemberArticle


def _fact(text: str) -> FactItem:
    return FactItem(text=text, source_index=1, paragraph=1, section="what")


class TestCrossLanguageMatching:
    """英語原文 × 日本語生成で、正しい値を落とさないこと。"""

    def test_million_matches_japanese_man(self) -> None:
        source = "Japanese providers lost up to 14.2 million email logins across six ISPs."

        assert quantities.unsupported("最大1420万件のログイン情報が流出した", [source]) == ()

    def test_english_number_word_matches_digits(self) -> None:
        source = "A drone strike near a gas station killed three people."

        assert quantities.unsupported("この攻撃で民間人3人が死亡した", [source]) == ()

    def test_abbreviated_month_with_period_matches(self) -> None:
        source = "Putin signed Decree No. 604 on Aug. 24."

        assert quantities.unsupported("プーチン大統領は8月24日に署名した", [source]) == ()

    def test_thousands_separator_is_ignored(self) -> None:
        source = "The breach exposed 77,619 customer records."

        assert quantities.unsupported("顧客情報7万7,619件が対象となった", [source]) == ()


class TestDetection:
    """原文に無い値は捕まえること。"""

    def test_sum_of_two_reported_values_is_flagged(self) -> None:
        source = "Providers lost up to 14.2 million logins and a further 12 million at a telco."

        assert quantities.unsupported("合計2620万件が流出した", [source]) == ("2620万件",)

    def test_weekday_converted_to_a_date_is_flagged(self) -> None:
        source = "Russian officials said on Saturday that the facility was hit."

        assert quantities.unsupported("8月23日に施設が攻撃された", [source]) == ("8月23日",)

    def test_lines_are_reported_with_their_position(self) -> None:
        source = "The report counted 41 incidents."
        facts = [_fact("41件のインシデントが確認された"), _fact("合計2620万件が流出した")]

        found = quantities.unsupported_lines(facts, {1: source})

        assert found == ((1, ("2620万件",)),)


def test_supporting_texts_cover_what_the_prompt_showed() -> None:
    # Arrange — プロンプトはタイトル・要約・記事時刻も渡している。照合範囲を
    # 本文だけにすると「報告日は…」のような記述を誤検出する
    member = MemberArticle(
        article_id="a1",
        title="見出し",
        url="https://kuebiko.example/1",
        feed_title="媒体",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 8, 23, tzinfo=UTC),
        summary="要約",
        body="本文",
        entities=frozenset(),
    )

    # Act
    texts = quantities.supporting_texts([member])

    # Assert
    assert quantities.unsupported("報告日は2026年8月23日とされている", list(texts)) == ()
