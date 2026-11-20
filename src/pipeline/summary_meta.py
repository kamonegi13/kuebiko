"""要約のメタ文 (重要度の理由づけ) の検出と除去 (2026-11-20、11-19 監査 D4 c)。

重要度の判断は ``importance`` の欄が持つ。要約・BLUF の文に「…ため high とはしない」
「重要度は medium と判断」が混ざる型を、文単位で見つける。CVSS・引用 (括弧・鉤括弧で
挟む形)・ベンダーの評価 (大文字の High 等) には当てない (段階名は小文字だけ)。
旗 ``SUMMARY_META_GUARD`` (0 / shadow / 1) は ``briefing`` が読む:
shadow はログ ``summary_meta_sentence`` (件数のみ)、1 は当たった文だけを決定論で削る
(残りが ``MIN_REMAINING_CHARS`` 未満なら削らない)。文区切りは繰り返しの関門と同じ SSoT。
"""

from __future__ import annotations

import re

from src.tools.sentences import SENTENCE_PATTERN

MIN_REMAINING_CHARS = 30

_LEVEL = r"(?<![「『(（])\b(?:high|medium|low)\b(?![」』)）])"
_META = re.compile(
    rf"(?:重要度|importance)は\s*{_LEVEL}\s*(?:と|に)"
    rf"|{_LEVEL}\s*(?:と(?:する|した|判定|判断|評価)|とは(?:し|せ)|には(?:し|該当))",
)
_QUOTED = re.compile(r"[「『(（]\s*(?:high|medium|low)\s*[」』)）]", re.IGNORECASE)


def _is_meta(sentence: str) -> bool:
    return bool(_META.search(sentence)) and not _QUOTED.search(sentence)


def find_meta_sentences(text: str) -> list[str]:
    """メタ文 (前後の空白を除いた文) の一覧 (純粋関数)。"""
    return [m.group(0).strip() for m in SENTENCE_PATTERN.finditer(text) if _is_meta(m.group(0))]


def strip_meta_sentences(text: str) -> str:
    """メタ文を除いた文。残りが短すぎるなら元のまま (純粋関数)。"""
    kept = [m.group(0) for m in SENTENCE_PATTERN.finditer(text) if not _is_meta(m.group(0))]
    out = "".join(kept).strip()
    if out == text.strip() or len(out) < MIN_REMAINING_CHARS:
        return text
    return out


__all__ = ["MIN_REMAINING_CHARS", "find_meta_sentences", "strip_meta_sentences"]
