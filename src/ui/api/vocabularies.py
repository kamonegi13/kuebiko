"""語彙配信 API (docs/vocabulary_label_architecture.md §4.2)。

GET /api/v1/vocabularies  全 vocabulary を `{name: [{value,label,order,…}]}` で返す。

frontend は起動時にこれを 1 回読み (boot gate)、value→label を解決する。static な
翻訳テーブルを frontend に持たないための単一集約エンドポイント。READ_ONLY middleware は
GET を素通しするため個別ガード不要。シークレットは一切含まない (表示ラベルのみ)。
"""

from __future__ import annotations

from fastapi import APIRouter

vocabularies_api = APIRouter(prefix="/api/v1/vocabularies", tags=["vocabularies"])


@vocabularies_api.get("")
async def get_vocabularies() -> dict[str, list[dict[str, object]]]:
    """全語彙の value→label 定義を返す (backend が唯一の SSoT)。"""
    from src.vocab import all_vocabularies

    return all_vocabularies()
