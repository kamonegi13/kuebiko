"""上限を超えた文を途中で切らずに縮める (2026-11-20、11-19 監査 P0-2)。

週次の状況総括が 9 週続けて節の上限 (6,000 字) の途中で切れたまま投稿されていた。
縮めるときは **行の境界 → 文の境界** の順で切り、省いた分を必ず明示する
(黙って捨てると読み手は欠けに気付けない)。境界が無いときだけ字数で切る。
"""

from __future__ import annotations

from src.tools.sentences import last_cut_position

OMITTED_NOTE = "(ほか {n} 件は省略)"


def _note(n: int) -> str:
    return OMITTED_NOTE.format(n=n)


def _cut_at_sentence(text: str, limit: int) -> str:
    head = text[:limit]
    pos = last_cut_position(head)
    return head[: pos + 1] if pos >= 0 else head


def trim_to_boundary(text: str, cap: int) -> tuple[str, int]:
    """``cap`` 字以内に縮めた文と、省いた行 (または文の塊) の数を返す。"""
    if len(text) <= cap:
        return text, 0
    lines = text.split("\n")
    # 注記の字数を先に確保する (省く件数の桁は行数を超えない)
    budget = cap - len(_note(len(lines))) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if kept else 0)
        if used + extra > budget:
            break
        kept.append(line)
        used += extra
    if kept:
        omitted = len(lines) - len(kept)
        return "\n".join(kept) + "\n" + _note(omitted), omitted
    # 1 行目だけで上限を超える: 文の境界で切る
    body = _cut_at_sentence(lines[0], max(budget, 0))
    omitted = len(lines)  # 1 行目の残りと後続の行
    return body + "\n" + _note(omitted), omitted


def cut_at_sentence(text: str, cap: int) -> str:
    """``cap`` 字以内の最後の文末で切る (注記は付けない)。文末が無ければ字数で切る。"""
    if len(text) <= cap:
        return text
    return _cut_at_sentence(text, cap)


__all__ = ["OMITTED_NOTE", "cut_at_sentence", "trim_to_boundary"]
