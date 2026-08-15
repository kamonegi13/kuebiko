"""表示用テキストの sanitizer (Phase 5L-2)。

Inoreader feed の summary_html や LLM 翻訳出力に紛れ込む HTML タグ、
HTML エンティティ、制御文字、Unicode 非正規形を一箇所で除去する。

役割:
    1. HTML タグの除去 (``<td>`` 等の残骸を排除)
    2. HTML エンティティ復元 (``&amp;`` → ``&``)
    3. Unicode NFKC 正規化 (全角/半角の揺らぎを吸収)
    4. 制御文字除去 (NUL, BEL, etc)
    5. 連続空白の畳み込み (オプション)

設計判断:
    - 依存追加を避けるため stdlib のみで実装 (bleach は使わない)
    - URL / IOC / CVE-ID は触らない (sanitize 対象は表示テキストのみ)
    - LLM 出力にも適用する: gemma4 等が翻訳時に元タイトルの HTML
      を引きずるケースを観測したため、二重防御として post-LLM にも通す
"""

from __future__ import annotations

import html
import re
import unicodedata

# HTML タグを丸ごと除去 (greedy にしない: <a>...</a> の中身は残す)
_HTML_TAG = re.compile(r"<[^>]+>")
# 末尾で切断された不完全タグ (例: 300 字 truncate で `</div>` が `</d` になった残骸)。
# 完全タグ除去後に残るため個別に落とす (2026-08-15 に remediation で実害)。
_TRUNCATED_TAG_TAIL = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9_-]*\s*$")
# 連続する空白 / 改行を 1 つに畳む (collapse=True 時)
_WHITESPACE_RUN = re.compile(r"\s+")
# 制御文字 (NUL, ESC, BEL 等。改行/タブは保持しない場合は \x09\x0a\x0d も削る)
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_for_display(
    text: str | None,
    *,
    max_length: int | None = None,
    collapse_whitespace: bool = False,
) -> str:
    """表示用テキストをサニタイズして返す。

    Args:
        text: 入力テキスト (None なら空文字を返す)
        max_length: 切り詰め長 (None なら無制限)
        collapse_whitespace: 連続空白を 1 つに畳む (タイトル向け)

    Returns:
        HTML タグ・制御文字・エンティティを除去し NFKC 正規化した文字列
    """
    if not text:
        return ""
    # 1. HTML タグ除去 (+ 切り詰めで生じた末尾の不完全タグ)
    out = _HTML_TAG.sub("", text)
    out = _TRUNCATED_TAG_TAIL.sub("", out)
    # 2. HTML エンティティ復元 (&amp; → &)
    out = html.unescape(out)
    # 3. Unicode NFKC 正規化 (全角英数 → 半角、特殊空白 → 通常空白 等)
    out = unicodedata.normalize("NFKC", out)
    # 4. 制御文字除去
    out = _CTRL_CHARS.sub("", out)
    # 5. 連続空白の畳み込み (オプション: タイトル向け)
    if collapse_whitespace:
        out = _WHITESPACE_RUN.sub(" ", out).strip()
    # 6. 切り詰め
    if max_length is not None and len(out) > max_length:
        out = out[:max_length]
    return out


def has_html_residue(text: str | None) -> bool:
    """入力に HTML タグや HTML エンティティが残存しているかを判定する。

    sanitizer の事後検証用 (運用ログで残存検出)。
    """
    if not text:
        return False
    if _HTML_TAG.search(text):
        return True
    # 主要なエンティティが残っていないか (decimal/named いずれも)
    return bool(re.search(r"&(amp|lt|gt|quot|nbsp|#\d+);", text))
