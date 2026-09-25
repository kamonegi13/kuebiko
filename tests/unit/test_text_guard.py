"""生成文の反復暴走を畳む関門 (2026-09-25)。"""

from __future__ import annotations

import time

from src.synthesis.text_guard import collapse_repetition


def test_runaway_tag_repeat_is_collapsed() -> None:
    # 2026-09-25 夕ブリーフの「比重」節の形 (286 回連続して上限で途切れていた)
    head = "【新規】EC カートシステムへの不正アクセスにより利用者情報が漏えいした。 "
    text = head + "/ 新規開設 (detect-new) " * 286 + "/ 新規設"
    fixed, runs = collapse_repetition(text)
    assert runs == 1
    assert fixed.count("新規開設 (detect-new)") == 1
    assert fixed.startswith(head)


def test_normal_enumeration_is_untouched() -> None:
    text = (
        "日本企業への不正アクセスでは、A 社の事例 (低確度)、B 社の流出 (中確度)、"
        "C 社の侵害 (中確度) が該当する。高確度 / 高確度 / 高確度。"
    )
    assert collapse_repetition(text) == (text, 0)


def test_empty_is_untouched() -> None:
    assert collapse_repetition("") == ("", 0)


def test_long_normal_text_is_fast() -> None:
    # 節の上限 6,000 字の正常文で遅くならないこと (正規表現の後戻りが暴れない)
    text = "".join(f"事象{i}について確度は中程度と見立てた。" for i in range(300))[:6000]
    t0 = time.monotonic()
    fixed, runs = collapse_repetition(text)
    assert runs == 0 and fixed == text
    assert time.monotonic() - t0 < 1.0
