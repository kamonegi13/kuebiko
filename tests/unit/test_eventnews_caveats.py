"""原文が付けた但し書き (caveats) の保持。

要約すると最初に落ちる種類の情報で、落ちると読み手が数字を誤読する。
実測 (2026-08-26): 原文が「週 80 件超は公開サンドボックスへの投稿数であり
被害組織数とは別の指標」と断っているのに、31B の生成は「週 80 件超が確認された」
だけになり、**観測された量が被害の規模に化けていた**。留保語の密度は 31B が
Sonnet の 55%、「相違」欄は 0.1 対 0.7。

散文の指示では 2 度とも動かなかったため (分量の下限・出力形式)、**構造で保持させる**。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from src.eventnews import runner
from src.eventnews.models import EventNewsDraft, FactItem


class TestSchema:
    def test_draft_has_a_caveats_field(self) -> None:
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            caveats=[FactItem(text="これは投稿数であり被害数ではない", source_index=1)],
        )

        assert [c.text for c in draft.caveats] == ["これは投稿数であり被害数ではない"]

    def test_caveats_are_required_in_the_llm_schema(self) -> None:
        """既定値のあるフィールドは制約デコードで省略が許され、静かに落ちる。"""
        schema = EventNewsDraft.model_json_schema()

        assert "caveats" in schema["required"]


class TestPrompt:
    def test_skeleton_shows_caveat_examples(self) -> None:
        """骨組みの例が唯一効いた介入 (散文の指示は 2 度とも無効だった)。"""
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")

        assert '"caveats"' in prompt
        assert "被害組織数ではない" in prompt

    def test_prompt_separates_caveats_from_unknowns(self) -> None:
        """unknowns は「まだ分からないこと」、caveats は「原文が範囲を限定していること」。"""
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")

        assert "`unknowns` との違い" in prompt

    def test_prompt_forbids_inventing_caveats(self) -> None:
        prompt = pathlib.Path("prompts/eventnews/refine.j2").read_text(encoding="utf-8")

        assert "但し書きとして創作しない" in prompt


class TestDropWarning:
    """空のまま増えていくことに気付けること (自動で書き足さない)。"""

    def test_runner_warns_when_the_source_qualifies_but_the_draft_does_not(self) -> None:
        source = inspect.getsource(runner._generate_version)

        assert "eventnews_caveats_dropped" in source
        assert "_CAVEAT_MARKERS" in source

    def test_markers_exclude_over_common_phrases(self) -> None:
        """一般的すぎる語を入れると常時警告になり、誰も見なくなる。"""
        for common in ("ではない", "という", "とされる", "可能性"):
            assert common not in runner._CAVEAT_MARKERS

    def test_runner_does_not_rewrite_caveats(self) -> None:
        """原文に無い但し書きを補うのは創作になる。観測に留める。"""
        tree = ast.parse(inspect.getsource(runner._generate_version))
        assigned = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }

        assert "caveats" not in assigned


class TestSurfacesConsumeIt:
    """write-only の欄を作らない (CLAUDE.md §7)。"""

    def test_public_api_returns_caveats(self) -> None:
        api = pathlib.Path("src/ui/api/public_news.py").read_text(encoding="utf-8")

        assert '"caveats": body.get("caveats", [])' in api

    def test_both_surfaces_render_caveats(self) -> None:
        for path in (
            "frontend/src/public/PublicNewsSite.tsx",
            "frontend/src/pages/eventnews/EventNewsDetail.tsx",
        ):
            tsx = pathlib.Path(path).read_text(encoding="utf-8")
            assert "caveats" in tsx, f"{path} が但し書きを描いていない"
            assert "読むうえでの但し書き" in tsx


class TestPromptPersistedForSft:
    """SFT 教師データ: (基底プロンプト, 関門通過後の出力) の対を版に残す。

    メンバー記事は後から合流して動くため、事後にプロンプトを再構成しても
    正確な対にならない — 生成時に対で残すのが唯一の方法 (2026-08-27)。
    Sonnet の生成を続けながら教師データを蓄積し、後で 31B へ蒸留する戦略の前提。
    """

    def test_runner_stores_the_base_prompt(self) -> None:
        source = inspect.getsource(runner._generate_version)

        # 保存するのは**基底** (書き直しヒント抜き) — 「基底 → 関門通過後の出力」の
        # 対が、学習で目指す挙動そのもの (関門の修正を焼き込む)
        assert "prompt_text = gen.build_prompt(selected, allowed)" in source
        assert "prompt_text=prompt_text" in source

    def test_public_api_never_exposes_the_prompt(self) -> None:
        api = pathlib.Path("src/ui/api/public_news.py").read_text(encoding="utf-8")

        assert "prompt_text" not in api
