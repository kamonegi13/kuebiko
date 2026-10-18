"""LLM の自由文 ``reason`` 欄を、長さで検証を落とさずに収める後処理 (2026-10-18)。

旧: ``Field(max_length=200)`` だと LLM の理由が 1 字超えただけで構造化出力全体の検証が失敗し、
正しい判定 (importance / matched 等) まで捨てて fail-open していた。さらに maxLength は
JSON schema として Ollama の文法にも渡るため、生成の暴走を誘発した前例がある
(2026-09-26)。長さの制約はスキーマに持たせず、検証後にここで文の区切りへ収める。
"""

from __future__ import annotations

from src.logging_config import get_logger

_log = get_logger(__name__)

#: reason の保存上限 (字)。下流は triage_rejections 500 字 / briefing 表示。
#: s24 教師の理由は中央値 164 字・190 字以上が 27.6% なので旧 200 では足りない。
TRIAGE_REASON_MAX = 300
_SENTENCE_ENDS = "。．！？!?.\n"
_ELLIPSIS = "…"


def trim_reason(text: str, limit: int = TRIAGE_REASON_MAX) -> str:
    """上限を超える理由を文の区切りで収める。区切りが無ければ上限で切って「…」を付ける。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENTENCE_ENDS)
    # 先頭近くの区切りで極端に短くなるのは避ける (半分未満なら字数で切る)
    if cut >= limit // 2:
        return head[: cut + 1].rstrip()
    return head[: limit - 1] + _ELLIPSIS


def trim_reason_logged(text: str, *, owner: str, limit: int = TRIAGE_REASON_MAX) -> str:
    """``trim_reason`` に加え、切り詰めたら ``reason_trimmed`` を数える (owner = スキーマ名)。"""
    trimmed = trim_reason(text, limit)
    if trimmed != text:
        _log.info("reason_trimmed", owner=owner, original_len=len(text), trimmed_len=len(trimmed))
    return trimmed
