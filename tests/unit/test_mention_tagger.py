"""mention_tagger (mentioned_country / campaign 決定論導出) のテスト。"""

from __future__ import annotations

from src.cti.mention_tagger import campaigns_in_text, derive_mention_entities


class TestCampaignsInText:
    def test_extracts_english_operation_name(self) -> None:
        got = campaigns_in_text("Europol announces Operation Endgame takedown results")
        assert got == frozenset({"Operation Endgame"})

    def test_extracts_two_word_operation_name(self) -> None:
        got = campaigns_in_text("Operation Midnight Eclipse targeted PAN-OS devices")
        assert "Operation Midnight Eclipse" in got

    def test_blocks_generic_operation_compounds(self) -> None:
        # SOC 文脈の "Security Operation Center" / OT の "Operation Technology" は作戦名でない
        assert campaigns_in_text("modernizing the Security Operation Center") == frozenset()
        assert campaigns_in_text("Operation Technology (OT) networks at risk") == frozenset()

    def test_extracts_katakana_operation(self) -> None:
        got = campaigns_in_text("欧州刑事警察機構がオペレーション・エンドゲームの成果を公表")
        assert got == frozenset({"Operation エンドゲーム"})

    def test_empty_text(self) -> None:
        assert campaigns_in_text("") == frozenset()


class TestDeriveMentionEntities:
    def test_derives_mentioned_country_from_title(self) -> None:
        got = derive_mention_entities(
            title="米空軍、B-2爆撃機の運用能力を初公開",
            summary="",
            involved_isos=set(),
        )
        assert ("mentioned_country", "US") in got

    def test_involved_iso_is_not_duplicated_as_mention(self) -> None:
        # 当事国 (involved_country) として付与済みの ISO は言及タグを重複付与しない
        got = derive_mention_entities(
            title="ロシアによるウクライナへの攻撃",
            summary="",
            involved_isos={"ru"},
        )
        types_values = set(got)
        assert ("mentioned_country", "RU") not in types_values
        assert ("mentioned_country", "UA") in types_values

    def test_summary_is_also_scanned(self) -> None:
        got = derive_mention_entities(
            title="制裁監視の緩和に関する報告書",
            summary="北朝鮮の不法な石炭輸出が増加していると報告された。",
            involved_isos=set(),
        )
        assert ("mentioned_country", "KP") in got

    def test_campaign_included(self) -> None:
        got = derive_mention_entities(
            title="Operation Endgame: 大規模ボットネット解体",
            summary="",
            involved_isos=set(),
        )
        assert ("campaign", "Operation Endgame") in got

    def test_no_mentions_returns_empty(self) -> None:
        got = derive_mention_entities(
            title="EDR の防御ロジックを AI で解析する手法",
            summary="技術的な解説記事。",
            involved_isos=set(),
        )
        assert got == []
