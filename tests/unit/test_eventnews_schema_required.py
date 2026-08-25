"""LLM へ渡す JSON schema が全プロパティ必須であること。

Ollama の制約デコードは schema をそのまま文法にするため、``required`` から
外れたフィールドは**文法上省略が許される**。2026-08-26 実測: プロンプトに 2 行
足しただけで ``key_points`` が 4/4 で欠落した (schema は変えていない)。
既定値は Python 側の構築しやすさのために残すので、ここが唯一の防波堤になる。
"""

from __future__ import annotations

from src.eventnews.models import EventNewsDraft


def test_all_draft_properties_are_required() -> None:
    # Arrange / Act
    schema = EventNewsDraft.model_json_schema()

    # Assert
    assert set(schema["required"]) == set(schema["properties"])


def test_all_fact_properties_are_required() -> None:
    # Arrange / Act — facts の中身も省略されうる (section が落ちると節が崩れる)
    fact_schema = EventNewsDraft.model_json_schema()["$defs"]["FactItem"]

    # Assert
    assert set(fact_schema["required"]) == set(fact_schema["properties"])


def test_python_side_defaults_are_preserved() -> None:
    # Arrange / Act — schema を必須にしても、コードからの構築は簡潔なままであること
    draft = EventNewsDraft(headline="見出し", bluf="要旨")

    # Assert
    assert draft.key_points == []
    assert draft.facts == []
