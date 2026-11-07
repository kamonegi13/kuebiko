"""書き手のプロンプト: 追加規則は DETECT_WRITER_GATES=1 のときだけ入り、旗 0 は変更前と一致。

変更前 (c6ff0601 の親) のテンプレートを旗 0 相当で描画した結果を tests/fixtures に凍結してあり、
それとバイト比較する (main へのマージ後も自己比較にならない)。trim_blocks 無しの環境
(passes._render・prompt_store の synthesis) が対象。
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from src.synthesis.grounded.passes import _render

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures"
_CTX: dict[str, object] = {
    "period_label": "P",
    "active_titles": ["追跡中A"],
    "pir_context": [{"title": "SIR1"}],
    "articles": [
        {"feed_title": "F", "title": "T", "article_id": "a1", "importance": "high"},
    ],
}
_RULE_MARK = "必ず 1 文だけ"
_TEMPLATE = "synthesis/detect_cluster_claim.j2"


def _frozen(name: str) -> str:
    with (_FIXTURES / f"detect_cluster_claim_flag0_{name}.txt").open(
        encoding="utf-8", newline=""
    ) as f:
        return f.read()


class TestFlagOffIsByteIdenticalToBeforeTheChange:
    def test_render_seam_path(self) -> None:
        got = _render(_TEMPLATE, strict_rules=False, **_CTX)

        assert got.encode() == _frozen("render_seam").encode()

    def test_synthesis_style_environment(self) -> None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_ROOT / "prompts")),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
        )
        got = env.get_template(_TEMPLATE).render(**_CTX, strict_rules=False)

        assert got.encode() == _frozen("synthesis_style").encode()


class TestFlagOn:
    def test_extra_rules_appear_only_when_on(self) -> None:
        on = _render(_TEMPLATE, strict_rules=True, **_CTX)
        off = _render(_TEMPLATE, strict_rules=False, **_CTX)

        assert _RULE_MARK in on
        assert _RULE_MARK not in off
        assert "再浮上" in on
