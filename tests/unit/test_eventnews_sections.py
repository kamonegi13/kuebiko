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
    """プロンプトを変えたら版を上げること。

    版を据え置くと、モデル別・版別の比較で「どの指示のときの出力か」が分からなく
    なる。2026-08-26 に v3→v4 の比較で「分量の下限を落としたこと」が 31B の出力を
    半減させた原因だと特定できたのは、版が分かれていたから。同日の複数回の変更は
    すべて v4 のままで、切り分け不能になる寸前だった。

    版番号を直書きするだけのテストは、**プロンプトを変えても通ってしまう**ので
    意味がない。ファイルのハッシュと対にして、変更したら必ず気付くようにする。
    """

    #: prompts/eventnews/refine.j2 の SHA-256。**プロンプトを変えたら版と一緒に更新する**。
    EXPECTED_PROMPT_SHA256 = "0cc6cf7761f03dd0f8fc7e00dca4df3b09bdd64d8473038c0b5e2ce2d28010ea"
    EXPECTED_VERSION = "eventnews-v6"

    def test_prompt_change_requires_a_version_bump(self) -> None:
        import hashlib
        from pathlib import Path

        from src.eventnews.runner import _PROMPT_VERSION

        prompt = Path(__file__).resolve().parents[2] / "prompts" / "eventnews" / "refine.j2"
        digest = hashlib.sha256(prompt.read_bytes()).hexdigest()

        assert _PROMPT_VERSION == self.EXPECTED_VERSION
        assert digest == self.EXPECTED_PROMPT_SHA256, (
            "プロンプトが変わっています。_PROMPT_VERSION を上げ、"
            f"EXPECTED_PROMPT_SHA256 を {digest} に更新してください"
        )


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


class TestInternalSurfaceMatchesPublic:
    """内部画面 (Tier1/ローカル) が公開面と同じ生成物を出すこと。

    同じ ``event_item_versions`` を読んでいるのでデータは共通だが、公開面だけに
    配線すると**内部画面の方が読みにくくなる**という逆転が起きる (2026-08-26 に
    実際に起きた: 要点は内部 API が返さず、節は型に無く段落のみだった)。
    """

    def test_internal_api_returns_key_points(self) -> None:
        api = pathlib.Path("src/ui/api/eventnews.py").read_text(encoding="utf-8")
        assert '"key_points": body.get("key_points", [])' in api

    def test_internal_detail_uses_the_shared_section_builder(self) -> None:
        """節の組み方を 2 つ持つと、どちらかが古くなる。"""
        tsx = pathlib.Path("frontend/src/pages/eventnews/EventNewsDetail.tsx").read_text(
            encoding="utf-8"
        )
        assert "buildSections" in tsx

    def test_summary_and_key_points_use_distinct_labels(self) -> None:
        """BLUF は散文の「要約」、箇条書きが「要点」。片方だけ別名にすると語が割れる。"""
        tsx = pathlib.Path("frontend/src/pages/eventnews/EventNewsDetail.tsx").read_text(
            encoding="utf-8"
        )
        assert "要約 (kuebiko 生成)" in tsx
        assert ">要点<" in tsx
