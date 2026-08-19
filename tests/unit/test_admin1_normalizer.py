"""州・県名の正規化 (2026-08-19)。

victim_city の 247 件中 97 件が Minnesota / カリフォルニア州 / 徳島県 のような
**州・県**で、都市 gazetteer では引けない。⚠ 判定基準は「所在都市 / **地域**」と書き
例に「カリフォルニア州」を挙げているので **LLM は指示どおり** — 受け手側で解決する。
"""

from __future__ import annotations

from src.cti.admin1_normalizer import normalize_admin1_key, strip_accents


class TestJapanesePrefectures:
    def test_suffix_is_stripped_and_mapped(self) -> None:
        assert normalize_admin1_key("徳島県", "JP") == "tokushima"
        assert normalize_admin1_key("愛知県", "JP") == "aichi"
        assert normalize_admin1_key("大阪府", "JP") == "osaka"
        assert normalize_admin1_key("東京都", "JP") == "tokyo"

    def test_hokkaido_keeps_its_suffix(self) -> None:
        """⚠ 「北海道」は接尾辞を落とすと「北海」になり引けない。"""
        assert normalize_admin1_key("北海道", "JP") == "hokkaido"


class TestUsStates:
    def test_english_names(self) -> None:
        assert normalize_admin1_key("Minnesota", "US") == "minnesota"
        assert normalize_admin1_key("California", "US") == "california"

    def test_katakana_names(self) -> None:
        """実データに「カリフォルニア州」9 件・「ミネソタ州」3 件。"""
        assert normalize_admin1_key("カリフォルニア州", "US") == "california"
        assert normalize_admin1_key("ミネソタ州", None) == "minnesota"


class TestAccentHandling:
    def test_macron_is_removed(self) -> None:
        """GeoNames の admin1 名はマクロン付き (hyōgo)。"""
        assert strip_accents("Hyōgo") == "Hyogo"
        assert normalize_admin1_key("Hyōgo", "JP") == "hyogo"

    def test_japanese_voiced_marks_survive(self) -> None:
        """⚠ 日本語に NFD を当てると濁点まで分解され「ジョージア」→「ショーシア」に壊れる。

        実データの複数州列挙 (「ジョージア州、ミシガン州」) で発生した。
        """
        assert strip_accents("ジョージア") == "ジョージア"
        assert normalize_admin1_key("ジョージア州、ミシガン州", None) == "ジョージア州、ミシガン州"


class TestNonAdmin1Input:
    def test_empty_returns_none(self) -> None:
        assert normalize_admin1_key("", "US") is None
        assert normalize_admin1_key("   ", "US") is None
