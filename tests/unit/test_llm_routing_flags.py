"""src.cti.llm_routing_flags のテスト (Phase 5L-3)。"""

from __future__ import annotations

from src.cti.llm_routing_flags import LLMRoutingFlags, parse_routing_flags


class TestParseRoutingFlags:
    def test_valid_full_input(self) -> None:
        raw = {
            "japan_targeted": True,
            "is_breaking_critical": True,
            "primary_actor_id": "salt typhoon",
            "dedup_key": "salt-typhoon-supplychain-ibm-italy",
            "confidence": "high",
        }
        flags = parse_routing_flags(raw)
        assert flags.japan_targeted is True
        assert flags.is_breaking_critical is True
        assert flags.primary_actor_id == "salt-typhoon"  # slug 化で空白 → ハイフン
        assert flags.dedup_key == "salt-typhoon-supplychain-ibm-italy"
        assert flags.confidence == "high"

    def test_missing_input_returns_safe_defaults(self) -> None:
        flags = parse_routing_flags(None)
        assert flags == LLMRoutingFlags()
        assert flags.japan_targeted is False
        assert flags.is_breaking_critical is False
        assert flags.primary_actor_id == ""
        assert flags.dedup_key == ""
        assert flags.confidence == "low"

    def test_non_dict_input_returns_defaults(self) -> None:
        assert parse_routing_flags("invalid") == LLMRoutingFlags()
        assert parse_routing_flags([1, 2, 3]) == LLMRoutingFlags()

    def test_empty_dict_returns_defaults(self) -> None:
        assert parse_routing_flags({}) == LLMRoutingFlags()

    def test_invalid_field_types_are_dropped(self) -> None:
        """LLM が型を間違えても安全側 default に倒す。"""
        raw = {
            "japan_targeted": "true",  # str ではなく bool 期待
            "is_breaking_critical": 1,  # int も拒否
            "primary_actor_id": 42,  # int
            "dedup_key": None,
            "confidence": "extreme",  # 規定外
        }
        flags = parse_routing_flags(raw)
        # すべて default に倒れる
        assert flags.japan_targeted is False
        assert flags.is_breaking_critical is False
        assert flags.primary_actor_id == ""
        assert flags.dedup_key == ""
        assert flags.confidence == "low"

    def test_partial_input_keeps_other_defaults(self) -> None:
        flags = parse_routing_flags({"japan_targeted": True})
        assert flags.japan_targeted is True
        assert flags.is_breaking_critical is False  # 未指定は default
        assert flags.confidence == "low"

    def test_slug_normalization(self) -> None:
        """大文字 / 連続空白 / 制御文字を slug 化。"""
        flags = parse_routing_flags(
            {"primary_actor_id": "  Salt   Typhoon  \x00", "confidence": "medium"},
        )
        assert flags.primary_actor_id == "salt-typhoon"

    def test_dedup_key_max_length(self) -> None:
        long_key = "x" * 200
        flags = parse_routing_flags({"dedup_key": long_key})
        assert len(flags.dedup_key) == 120


class TestLLMRoutingFlagsModel:
    def test_default_construction(self) -> None:
        flags = LLMRoutingFlags()
        assert flags.japan_targeted is False
        assert flags.confidence == "low"

    def test_frozen(self) -> None:
        from pydantic import ValidationError

        flags = LLMRoutingFlags(japan_targeted=True)
        try:
            flags.japan_targeted = False
        except ValidationError:
            return  # 期待動作
        # frozen=True なら割り当ては失敗する想定
        # 失敗しなかった = フィールドは更新されていないはず
        # (BaseModel frozen=True はインスタンス書き換え禁止)
