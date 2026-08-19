"""表示用テキストの sanitizer (Phase 5L-2)。

RSS feed の summary_html や LLM 翻訳出力に紛れ込む HTML タグ、
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
# LLM が JSON 文字列を閉じ損ねると、後続フィールドの JSON が**そのまま値へ流れ込む**
# (2026-08-18 に本番 33 件を確認。remediation の値に "article_type": ... 以降が丸ごと
# 入り、300 字 truncate のおかげで「少し長い対処」に見えて 2 か月気付けなかった)。
# 署名は「JSON のキーと値の開始」= 引用符で囲んだ ASCII 識別子 + コロン + 値の開始文字。
# 日本語の散文がこの形になることは実質無いので、ここを境に後ろを捨てる。
_JSON_TAIL = re.compile(r'"[A-Za-z_][A-Za-z0-9_]{2,30}"\s*:\s*(?:"|\[|\{|true|false|null|-?\d)')
# 切断面に残る JSON の残骸 (`", ` / `、",` / 文字列としての `\n`)。
_JSON_CUT_DEBRIS = re.compile(r'(?:\\n|[\s"\'、,\\])+$')


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


def cut_json_tail(text: str | None) -> str:
    """値に流れ込んだ「JSON の続き」を切り落とす。

    LLM が文字列を閉じ損ねたとき、後続フィールドの JSON がその値の一部になる。
    先頭の正しい 1 文は救えるので、**捨てるのは署名以降だけ**にする。
    """
    if not text:
        return ""
    match = _JSON_TAIL.search(text)
    if not match:
        return text
    return _JSON_CUT_DEBRIS.sub("", text[: match.start()])


def has_json_tail(text: str | None) -> bool:
    """値に JSON の続きが混入しているか (事後検証・監査用)。"""
    return bool(text) and bool(_JSON_TAIL.search(text or ""))


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


# ---- 抽出本文と記事の同一性 (2026-08-18) ----
#
# 取得が成功していても **別記事の本文が入っている** ことがある (14 日で 7 件、0.4%)。
# 実例: databreachtoday の npm ワーム記事に「Open Secure AI Alliance…」の本文、
# The Register の記事にナビ断片 (`MOST POPULAR AI …`)。同じ URL を後から叩くと
# 正しく取れるので**取得時点の過渡的な事象**であり、サイト構造の対処では防げない。
#
# ⚠ 「取得成功 N 件」しか見ていなかったため 2 か月検知できなかった。
# fetch の成否とは別に **中身が当該記事か** を測る指標を持つ。
_TITLE_NGRAM = 3


def _char_ngrams(text: str, n: int = _TITLE_NGRAM) -> set[str]:
    """空白を除いた文字 n-gram。⚠ 日本語は空白区切りの語トークンでは測れない。"""
    compact = "".join((text or "").split())
    return {compact[i : i + n] for i in range(len(compact) - n + 1)}


# 文字種の大分類 (dominant script 判定用)。同一性比較は文字 n-gram なので、
# 文字種が違う 2 タイトル (韓国語ページ title vs 日本語 RSS title 等) は常に 0 点に
# なり「別記事混入」と区別できない。初日実測 (2026-08-19、警告 15 件の抜き取り 4 件中
# 3 件) がこの型の誤検知だった — 測れないものは測れないと言う (None)。
_SCRIPT_RANGES: tuple[tuple[str, int, int], ...] = (
    ("latin", 0x0041, 0x024F),
    ("cyrillic", 0x0400, 0x04FF),
    ("hangul", 0xAC00, 0xD7AF),
    ("cjk", 0x3040, 0x30FF),  # ひらがな・カタカナ
    ("cjk", 0x4E00, 0x9FFF),  # 漢字 (日中共用のため hangul と別枠)
)


def dominant_script(text: str | None) -> str | None:
    """タイトルの主要文字種 (latin/cyrillic/hangul/cjk)。判定不能 (記号のみ等) は None。"""
    counts: dict[str, int] = {}
    for ch in text or "":
        code = ord(ch)
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= code <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def title_similarity(a: str | None, b: str | None) -> float:
    """2 つのタイトルの近さ (0.0-1.0)。短い側を分母にする包含率。

    サイト側タイトルは「記事名 | サイト名」のように付加語を持つことが多いので、
    Jaccard ではなく短い側基準にする (付加語で不当に下がらない)。
    """
    ga, gb = _char_ngrams(a or ""), _char_ngrams(b or "")
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))
