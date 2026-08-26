"""公開サイトの静的書き出しの契約 (scripts/export_public_site.py)。

読者を運用者の PC に依存させないための書き出し。**経路が 1 つ増える**ので、
公開 API 側の契約 (出典必須・high のみ・原文を返さない) が静的側で破れていないかを
ここで固定する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "export_public_site",
    Path(__file__).resolve().parents[2] / "scripts" / "export_public_site.py",
)
assert _SPEC and _SPEC.loader
export_public_site = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_public_site)


class TestPublisherBodyNeverLeaves:
    """出版社の本文は静的ファイルにも出さない (§9 の再配布境界)。"""

    def test_body_key_is_rejected(self) -> None:
        payload = {"items": [{"id": "ev-1", "body": "原文"}]}

        try:
            export_public_site._assert_no_publisher_body(payload, "index")
        except SystemExit as exc:
            assert "原文" in str(exc)
        else:  # pragma: no cover - 失敗時のみ
            raise AssertionError("body を含む payload が通ってしまった")

    def test_body_ja_is_rejected_even_when_nested(self) -> None:
        payload: dict[str, Any] = {"items": [{"citations": [{"body_ja": "翻訳"}]}]}

        try:
            export_public_site._assert_no_publisher_body(payload, "index")
        except SystemExit:
            return
        raise AssertionError("入れ子の body_ja が通ってしまった")

    def test_clean_payload_passes(self) -> None:
        payload = {"items": [{"id": "ev-1", "headline": "見出し", "summary": "要約"}]}

        export_public_site._assert_no_publisher_body(payload, "index")


class TestIndexStaysSmall:
    """一覧は初回に全件読む。全文を載せると初回読み込みが重くなる。"""

    def test_teaser_is_truncated(self) -> None:
        # 実測: 全文を載せた index.json は 1.3 MB。表示は line-clamp で 2-3 行まで
        assert export_public_site._TEASER_CHARS <= 200

    def test_index_fields_exclude_citations(self) -> None:
        # citations は 360 KB を占める。詳細ファイル側にあれば足りる
        assert "citations" not in export_public_site._INDEX_FIELDS
