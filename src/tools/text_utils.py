"""共有テキストユーティリティ (Phase 5I で main.py / article_triage の重複統合)。

HTML タグ除去のように複数モジュールから使われる小さな処理を集約する。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")


class _HTMLStripper(HTMLParser):
    """HTML タグを除去してプレーンテキストの断片を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(html_text: str) -> str:
    """HTML タグ除去 + 文字実体参照 (``&amp;`` 等) を解決してプレーンテキスト化する。

    壊れた HTML で HTMLParser が例外を出すことがあるため、その時は
    正規表現での雑な除去にフォールバックする (graceful degradation)。
    """
    if not html_text:
        return ""
    parser = _HTMLStripper()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001
        # parser が壊れた HTML で例外を出すことがある。最後の手段で正規表現フォールバック。
        return _WHITESPACE.sub(" ", re.sub(r"<[^>]+>", " ", html_text)).strip()
    return _WHITESPACE.sub(" ", " ".join(parser.parts)).strip()
