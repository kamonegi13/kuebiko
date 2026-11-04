"""書き手のプロンプト: 追加規則は DETECT_WRITER_GATES=1 のときだけ入り、旗 0 は main 版と一致。

trim_blocks 無しの環境 (passes._render・prompt_store の synthesis/grounded) が対象。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import jinja2
import pytest

from src.synthesis.grounded.passes import _render

_REL = "prompts/synthesis/detect_cluster_claim.j2"
_ROOT = Path(__file__).resolve().parents[2]
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


def _main_template() -> str:
    try:
        out = subprocess.run(
            ["git", "show", f"main:{_REL}"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("main の版を取れない")
    return out.stdout


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_ROOT / "prompts")),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


class TestFlagOffIsByteIdenticalToMain:
    def test_render_seam_path(self) -> None:
        # _render は keep_trailing_newline 無しの環境 (passes.py)
        env = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)
        baseline = env.from_string(_main_template()).render(**_CTX, strict_rules=False)

        assert _render(_TEMPLATE, strict_rules=False, **_CTX) == baseline

    def test_synthesis_style_environment(self) -> None:
        env = _env()
        baseline = env.from_string(_main_template()).render(**_CTX, strict_rules=False)
        got = env.get_template(_TEMPLATE).render(**_CTX, strict_rules=False)

        assert got == baseline


class TestFlagOn:
    def test_extra_rules_appear_only_when_on(self) -> None:
        on = _render(_TEMPLATE, strict_rules=True, **_CTX)
        off = _render(_TEMPLATE, strict_rules=False, **_CTX)

        assert _RULE_MARK in on
        assert _RULE_MARK not in off
        assert "再浮上" in on
