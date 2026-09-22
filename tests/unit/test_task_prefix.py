"""課題の接頭辞 — 多課題 SFT の負の転移を抑える (2026-09-22)。

⚠ **発端**: detect (96 対) を判定系 5 課題の混合に足したら **triage が退行**した
(26B からの移動が s17 の 4 件 → 49 件、うち降格 43 件、high→low の反転 2 件)。
教師のプロンプトは 5 課題すべてが「あなたは日本の CTI アナリストです」で始まり、
**課題の違いは本文の途中に埋もれていた**。

先行研究: 入力に固有の接頭辞を付けるとモデルの容量配分が課題ごとに調整され、
負の転移が減る (task-specific instruction prefixes / Task Compass)。

⭐ 接頭辞は **学習と本番の両方**に入れる。片方だけだと生徒が見たことのない形になる。
"""

from __future__ import annotations

from src.tools.model_tiers import Step
from src.tools.task_prefix import prefix_for, task_prefix_enabled, with_task_prefix


class TestFlagDefault:
    def test_disabled_by_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """⚠⚠ 常駐モデルは接頭辞なしで学習されている — 既定 ON にすると
        コードを入れた瞬間に見たことのない形が届く。"""
        monkeypatch.delenv("SFT_TASK_PREFIX", raising=False)

        assert task_prefix_enabled() is False
        assert with_task_prefix("本文", Step.TRIAGE) == "本文"


class TestPrefixFor:
    def test_each_trained_step_has_its_own_marker(self) -> None:
        got = {prefix_for(s) for s in (Step.TRIAGE, Step.PAIR_JUDGE, Step.EVENT_KIND)}

        assert len(got) == 3
        assert all(p and p.endswith("\n") for p in got)

    def test_detect_and_triage_do_not_share_a_marker(self) -> None:
        """⭐ この 2 つが取り違えられて triage が降格方向へ寄った。"""
        assert prefix_for(Step.SYNTHESIS_DETECT) != prefix_for(Step.TRIAGE)

    def test_untrained_step_has_no_marker(self) -> None:
        """学習していない step に印を付けない (本番だけ形が変わるのを防ぐ)。"""
        assert prefix_for(Step.PIR_COMPILE) == ""


class TestWithTaskPrefix:
    def test_prefix_is_prepended_once(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX", "1")
        out = with_task_prefix("本文", Step.TRIAGE)

        assert out.startswith(prefix_for(Step.TRIAGE))
        assert out.endswith("本文")
        assert out.count(prefix_for(Step.TRIAGE)) == 1

    def test_already_prefixed_text_is_not_doubled(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX", "1")
        once = with_task_prefix("本文", Step.TRIAGE)

        assert with_task_prefix(once, Step.TRIAGE) == once

    def test_unmarked_step_returns_the_text_unchanged(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX", "1")
        assert with_task_prefix("本文", Step.PIR_COMPILE) == "本文"

    def test_enabled_by_flag(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """旗を立てたときだけ付く (接頭辞つきで学習したモデルの配備と同じ版で立てる)。"""
        monkeypatch.setenv("SFT_TASK_PREFIX", "1")

        assert task_prefix_enabled() is True
        assert with_task_prefix("本文", Step.TRIAGE).startswith("[task: triage]")


class TestPlacementAtSequenceHead:
    """⭐ 印は **系列の先頭** に置く (system があれば system の先頭)。

    system の後ろに置くと、課題を定義する長い文を読んだ後に印が来るため、切替の
    合図として働きにくい。学習データ側 (assemble_sft_dataset) も同じ置き方にする。
    """

    def test_system_gets_the_marker_when_present(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import asyncio

        monkeypatch.setenv("SFT_TASK_PREFIX", "1")
        seen: dict[str, object] = {}

        class _Inner:
            model = "fake"

            async def generate(self, prompt: str, **kw: object) -> object:
                seen["prompt"] = prompt
                seen["system"] = kw.get("system")
                return object()

        from src.tools.task_prefix import TaskPrefixClient

        c = TaskPrefixClient(_Inner(), Step.PAIR_JUDGE)  # type: ignore[arg-type]
        asyncio.run(c.generate("本文", system="役割の定義"))

        assert str(seen["system"]).startswith("[task: pair]")
        assert seen["prompt"] == "本文"

    def test_prompt_gets_the_marker_when_there_is_no_system(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import asyncio

        monkeypatch.setenv("SFT_TASK_PREFIX", "1")
        seen: dict[str, object] = {}

        class _Inner:
            model = "fake"

            async def generate(self, prompt: str, **kw: object) -> object:
                seen["prompt"] = prompt
                return object()

        from src.tools.task_prefix import TaskPrefixClient

        c = TaskPrefixClient(_Inner(), Step.TRIAGE)  # type: ignore[arg-type]
        asyncio.run(c.generate("本文", system=None))

        assert str(seen["prompt"]).startswith("[task: triage]")
