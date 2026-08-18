"""LLM へ渡す JSON Schema の required 拡張のテスト (2026-08-18)。

既定値を持つ field は required に載らず、Ollama の構造化出力では「途中で JSON を
閉じる」ことが許される。実測ではこれが致命的だった: gold set 172 件で **analyst_note を
書いた記事は victim_* を 1 つも埋めない** (重複 2 件)。schema 順で所見は 10 番目、
被害の帰属は 15-19 番目なので、所見を書いた記事はそこで閉じて到達していなかった。
"""

from __future__ import annotations

from src.pipeline.summary import _LLM_REQUIRED_FIELDS, SummaryOutput


class TestLlmSchemaRequired:
    def test_extraction_fields_are_required_for_the_llm(self) -> None:
        """欠落そのものを禁じる — プロンプトで頼むのではなく schema で保証する。"""
        required = set(SummaryOutput.model_json_schema()["required"])

        assert required >= _LLM_REQUIRED_FIELDS
        for field in ("analyst_note", "victim_sector", "victim_country", "victim_orgs"):
            assert field in required

    def test_suppressed_fields_stay_optional(self) -> None:
        """summarizer に出させないフィールドまで強制すると無意味な値を書かせる。"""
        required = set(SummaryOutput.model_json_schema()["required"])

        for field in ("bluf", "routing_flags", "pmesii_axes", "diamond", "article_type"):
            assert field not in required

    def test_required_fields_can_express_absence(self) -> None:
        """捏造を強制しないため、対象は null / [] を返せる型に限る。"""
        schema = SummaryOutput.model_json_schema()

        for field in _LLM_REQUIRED_FIELDS:
            prop = schema["properties"][field]
            nullable = any(b.get("type") == "null" for b in prop.get("anyOf", []))
            is_array = prop.get("type") == "array"
            assert nullable or is_array, f"{field} は「無い」を表現できない"


class TestPythonSideStaysLenient:
    """内部生成・テスト・Grok 経路の後方互換。**LLM にだけ厳しくする**非対称。"""

    def test_internal_construction_needs_only_the_four_core_fields(self) -> None:
        out = SummaryOutput(title_ja="題", importance="low", category="other", summary="本文")

        assert out.analyst_note is None
        assert out.victim_orgs == []

    def test_validation_accepts_payload_without_required_llm_fields(self) -> None:
        """few-shot 例の検証 (rubric_model) はこの経路を通るので緩いままである必要がある。"""
        parsed = SummaryOutput.model_validate(
            {"title_ja": "題", "importance": "low", "category": "other", "summary": "本文"}
        )

        assert parsed.victim_sector is None
