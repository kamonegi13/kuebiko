"""上限を超えた文を「途中で切らず」に縮める (2026-11-20、11-19 監査 P0-2)。

週次の状況総括が 9 週続けて 6,000 字の途中で切れたまま投稿されていた。
上限を超えたら行の境界 → 文の境界で縮め、省いた分を明示する。
"""

from __future__ import annotations

from src.tools.text_budget import OMITTED_NOTE, trim_to_boundary


def test_under_cap_is_unchanged() -> None:
    assert trim_to_boundary("短い文。", 100) == ("短い文。", 0)


def test_cuts_at_line_boundary_and_notes_omitted_lines() -> None:
    lines = [f"【拡大】判定{i} の動き。含意の文。" for i in range(40)]
    text = "\n".join(lines)
    out, omitted = trim_to_boundary(text, 300)
    assert len(out) <= 300
    assert omitted > 0
    assert out.endswith(OMITTED_NOTE.format(n=omitted))
    kept = out.rsplit("\n", 1)[0].split("\n")
    assert all(line in lines for line in kept)  # 行の途中で切っていない
    assert len(kept) + omitted == len(lines)


def test_single_long_line_cuts_at_sentence_end() -> None:
    text = "一文目です。" * 50 + "途中で終わる文"
    out, omitted = trim_to_boundary(text, 100)
    assert len(out) <= 100
    body = out.rsplit("\n", 1)[0]
    assert body.endswith("。")
    assert omitted >= 1


def test_no_sentence_boundary_falls_back_to_hard_cap() -> None:
    text = "あ" * 500
    out, _ = trim_to_boundary(text, 100)
    assert len(out) <= 100
