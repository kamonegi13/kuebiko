"""翻訳の「JSON の形が混ざっている」判定の、原文の JSON の免除 (2026-11-19)。

10-02 の関門 (``JSON_MAPPING_RE``、``body_translator.broken_translation_reason``) は、
JSON で答える課題を学習したモデルが訳文を ``{"原文": "訳文"}`` の対応表で返す崩れを
止めるためのもの。ところが
**原文そのものに JSON (設定・ログ・API 応答の例) がある記事**では、プロンプトの指示どおり
コードを原文のまま残した**正しい訳**でも発火する。11-18 時点で「JSON の形」で訳が止まって
いた 13 記事は、失敗したチャンクの原文が 13 件とも JSON を含んでいた。

``TRANSLATE_JSON_SOURCE_EXEMPT`` (コードの既定 0 = 従来と同一):
- ``shadow``: 判定は従来どおり。訳文の JSON の形が**すべて原文に在る**ため免除できる場合に
  ログ ``translate_json_source_exempt`` (件数のみ、本文なし) を出す
- ``1``: 訳文の JSON の形がすべて原文にも在れば壊れていないとみなす。原文に無い形
  (原文の文を鍵にした対応表など) が 1 つでもあれば従来どおり壊れているとする
"""

from __future__ import annotations

import os
import re

from src.logging_config import get_logger

_log = get_logger(__name__)

JSON_SOURCE_EXEMPT_ENV = "TRANSLATE_JSON_SOURCE_EXEMPT"

#: 出力が JSON の対応表 ({"原文": "訳文"}) になっている形 (body_translator と同じ定義)
JSON_MAPPING_RE = re.compile(r'\{\s*"[^"\n]{1,400}"\s*:\s*"')
_WS_RE = re.compile(r"\s+")


def json_source_exempt_mode() -> str:
    raw = os.environ.get(JSON_SOURCE_EXEMPT_ENV, "0").strip().lower()
    if raw in ("1", "true", "on"):
        return "on"
    if raw == "shadow":
        return "shadow"
    return "off"


def _shapes(text: str) -> list[str]:
    return [_WS_RE.sub("", m.group(0)) for m in JSON_MAPPING_RE.finditer(text)]


def novel_json_shapes(source: str, output: str) -> int:
    """訳文の JSON の形のうち、原文に無いものの数 (空白は無視して比べる)。"""
    in_source = set(_shapes(source))
    return sum(1 for shape in _shapes(output) if shape not in in_source)


def is_json_mapping_broken(source: str, output: str) -> bool:
    """訳文が JSON の対応表で壊れているか (旗に従う)。"""
    shapes = _shapes(output)
    if not shapes:
        return False
    mode = json_source_exempt_mode()
    if mode == "off":
        return True
    novel = novel_json_shapes(source, output)
    if mode == "shadow":
        if novel == 0:
            _log.info("translate_json_source_exempt", mode=mode, shapes=len(shapes))
        return True
    if novel == 0:
        _log.info("translate_json_source_exempt", mode=mode, shapes=len(shapes))
        return False
    return True


__all__ = [
    "JSON_MAPPING_RE",
    "JSON_SOURCE_EXEMPT_ENV",
    "is_json_mapping_broken",
    "json_source_exempt_mode",
    "novel_json_shapes",
]
