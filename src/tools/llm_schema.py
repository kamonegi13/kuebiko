"""LLM へ渡す JSON schema の共通加工 (制約デコードの落とし穴への手当)。

structured 出力を使う全 step で同じ罠を踏むため、helper をここ 1 箇所に置く
(eventnews / synthesis render が共有。新しい structured 出力もここを使う)。
"""

from __future__ import annotations

from pydantic.json_schema import JsonSchemaValue


def require_all_properties(schema: JsonSchemaValue) -> JsonSchemaValue:
    """LLM へ渡す JSON schema の全プロパティを ``required`` にする。

    既定値を持つフィールドを pydantic は ``required`` から外す。Ollama の制約デコードは
    その schema をそのまま文法にするため、**省略が文法上許され、後発のフィールドから
    静かに落ちる**。2026-08-26 実測: プロンプトに 2 行足しただけで ``key_points`` が
    4/4 で欠落した (schema は変えていない = 省略が許されている限り再発する)。
    Python 側の構築しやすさ (既定値) は保ったまま、**LLM へ渡す schema だけ**全必須にする。
    """
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["required"] = list(properties)
    return schema
