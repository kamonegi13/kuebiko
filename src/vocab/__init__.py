"""語彙レジストリ (value→ja_label の単一 SSoT)。

コード所有 enum (A) / 設定語彙 (B) の表示ラベルを backend 1 箇所に定義し、
`/api/v1/vocabularies` で配信する。frontend は静的な value→label マップを持たない。
設計: docs/vocabulary_label_architecture.md
"""

from src.vocab.registry import (
    VocabItem,
    Vocabulary,
    all_vocabularies,
    get_vocabulary,
    registered_names,
)

__all__ = [
    "VocabItem",
    "Vocabulary",
    "all_vocabularies",
    "get_vocabulary",
    "registered_names",
]
