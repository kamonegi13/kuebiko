"""本文の節 (SECTION_KEYS) の契約。

2026-08-25 追加。生成物を「何が起きたか / 影響範囲 / 手口 / 対応 / 背景 /
利用者の対応」に分けて読ませる。**key は backend が SSoT で、表示名は frontend**
(自由記述の見出しを許すと表記が揺れる)。両者がずれると節が出なくなるので固定する。
"""

from __future__ import annotations

import pathlib
import re

from src.eventnews.models import SECTION_KEYS, EventNewsDraft, FactItem


class TestSchema:
    def test_fact_has_a_section_with_a_default(self) -> None:
        """既定を持たせる。structured 生成で値が返らなくても本文を失わない。"""
        assert FactItem(text="x").section == "what"

    def test_section_survives_serialization(self) -> None:
        """保存は `model_dump_json`。ここに載らないと表示側へ届かない。"""
        draft = EventNewsDraft(headline="h", bluf="b", facts=[FactItem(text="x", section="scope")])
        assert '"section":"scope"' in draft.model_dump_json().replace(" ", "")

    def test_vocabulary_is_the_six_keys(self) -> None:
        assert SECTION_KEYS == ("what", "scope", "how", "response", "context", "action")


class TestPromptAndFrontendAgree:
    def test_prompt_lists_every_key(self) -> None:
        """プロンプトが語彙を漏らすと、その節は永久に出ない。"""
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")
        for key in SECTION_KEYS:
            assert f"`{key}`" in prompt, f"プロンプトに {key} の説明が無い"

    def test_prompt_forbids_revisiting_a_section(self) -> None:
        """実測で `what → response → what` と往復した。プロンプト側でも止める。"""
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")
        assert "一度離れた節へ戻らない" in prompt
        assert "1 つの段落は 1 つの節に属する" in prompt

    def test_frontend_knows_the_same_keys(self) -> None:
        """key の SSoT は backend。frontend の並び順定義とずれたら節が消える。"""
        ts = pathlib.Path("frontend/src/public/sections.ts").read_text(encoding="utf-8")
        match = re.search(r"SECTION_ORDER\s*=\s*\[([^\]]+)\]", ts)
        assert match is not None, "SECTION_ORDER が見つからない"
        keys = tuple(re.findall(r'"([a-z_]+)"', match.group(1)))
        assert keys == SECTION_KEYS


class TestPromptVersion:
    def test_version_was_bumped(self) -> None:
        """出力形が変わったら版を上げる (どの版で生成したかを版履歴に残すため)。"""
        from src.eventnews.runner import _PROMPT_VERSION

        assert _PROMPT_VERSION == "eventnews-v4"


class TestSpreadGuard:
    """節が 1 種類に偏ったら気付けること。

    2026-08-25 実測: JSON 例が `"section": "what"` の 1 行だけだったとき、モデルが
    それを写して **14 件の事実行が全部 what** になった (中身は影響範囲や手口を
    含んでいた)。例を 4 行に分散したら 5/5 件で 4〜6 種に分かれた。
    """

    def test_prompt_shows_varied_section_examples(self) -> None:
        """JSON 例に 1 種類しか出さないと、モデルがそれを写す。"""
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")
        skeleton = prompt.split('"facts": [')[1].split("],")[0]
        shown = set(re.findall(r'"section":\s*"([a-z_]+)"', skeleton))
        assert len(shown) >= 3, f"JSON 例の section が偏っている: {shown}"

    def test_prompt_forbids_a_single_section(self) -> None:
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")
        assert "全ての fact を同じ節にしない" in prompt

    def test_runner_warns_instead_of_reassigning(self) -> None:
        """⚠ 自動で振り直さない (創作になる)。観測できるようにするだけ。"""
        import inspect

        from src.eventnews import runner

        src = inspect.getsource(runner._generate_version)
        assert "eventnews_sections_not_spread" in src
        assert "_SECTION_SPREAD_MIN_FACTS" in src
