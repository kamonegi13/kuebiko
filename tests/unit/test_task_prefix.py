"""課題の接頭辞 — 多課題 SFT の負の転移を抑える (2026-09-22)。

⚠ **発端**: detect (96 対) を判定系 5 課題の混合に足したら **triage が退行**した
(⚠ 2026-09-23 訂正: triage の退行は基準の取り違えで、崩れたのは event_kind。
 s18 は種別判定に PIR 判定のキー `matched` を書いていた = 課題の取り違え)。
教師のプロンプトは 5 課題すべてが「あなたは日本の CTI アナリストです」で始まり、
**課題の違いは本文の途中に埋もれていた**。

先行研究: 入力に固有の接頭辞を付けるとモデルの容量配分が課題ごとに調整され、
負の転移が減る (task-specific instruction prefixes / Task Compass)。

⭐ 接頭辞は **学習と本番の両方**に入れる。片方だけだと生徒が見たことのない形になる。
"""

from __future__ import annotations

from src.tools.model_tiers import Step
from src.tools.task_prefix import (
    PREFIX_TRAINED_MODELS,
    prefix_for,
    task_prefix_enabled,
    with_task_prefix,
)


class TestPerModelActivation:
    """⭐⭐ 接頭辞は **接頭辞つきで学習したモデルにだけ** 付ける (2026-09-24)。

    旧設計は大域の旗 ``SFT_TASK_PREFIX`` で、立てると接頭辞なしで学習した n17c / n17m30
    にも印が付き、見たことのない形が届く。逆に接頭辞つきで学習した s19 から印を外すと
    event_kind が 277 → 257 に落ちた (生出力で形式が崩れる)。「印で学習したか」は
    モデル固有の性質なので、モデル名で判定する。
    """

    def test_unregistered_model_gets_no_prefix(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)

        assert task_prefix_enabled("kuebiko-sft:s17") is False
        assert with_task_prefix("本文", Step.TRIAGE, "kuebiko-sft:s17") == "本文"

    def test_registered_model_gets_prefix_without_any_env(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """⭐ UI でモデルを割り当てただけで効く (環境変数の設定漏れで黙って外れない)。"""
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)

        assert "kuebiko-sft:s19" in PREFIX_TRAINED_MODELS
        assert task_prefix_enabled("kuebiko-sft:s19") is True

    def test_env_adds_models_under_evaluation(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", " kuebiko-sft:s20 , kuebiko-sft:x ")

        assert task_prefix_enabled("kuebiko-sft:s20") is True
        assert task_prefix_enabled("kuebiko-sft:x") is True
        assert task_prefix_enabled("kuebiko-sft:s17") is False

    def test_legacy_global_flag_no_longer_enables_every_model(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)

        assert task_prefix_enabled("kuebiko-sft:n17c") is False


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
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
        out = with_task_prefix("本文", Step.TRIAGE, "fake")

        assert out.startswith(prefix_for(Step.TRIAGE))
        assert out.endswith("本文")
        assert out.count(prefix_for(Step.TRIAGE)) == 1

    def test_already_prefixed_text_is_not_doubled(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
        once = with_task_prefix("本文", Step.TRIAGE, "fake")

        assert with_task_prefix(once, Step.TRIAGE, "fake") == once

    def test_unmarked_step_returns_the_text_unchanged(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
        assert with_task_prefix("本文", Step.PIR_COMPILE, "fake") == "本文"


class TestPlacementAtSequenceHead:
    """⭐ 印は **系列の先頭** に置く (system があれば system の先頭)。

    system の後ろに置くと、課題を定義する長い文を読んだ後に印が来るため、切替の
    合図として働きにくい。学習データ側 (assemble_sft_dataset) も同じ置き方にする。
    """

    def test_system_gets_the_marker_when_present(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import asyncio

        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
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

        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "fake")
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


class TestWrapperFollowsTheInnerModel:
    """同じ step の wrapper でも、中のモデルが接頭辞で学習したかで付く/付かないが分かれる。"""

    @staticmethod
    def _sent(model: str) -> str:
        import asyncio

        from src.tools.task_prefix import TaskPrefixClient

        seen: dict[str, str] = {}

        class _Inner:
            def __init__(self) -> None:
                self.model = model

            async def generate(self, prompt: str, **kw: object) -> object:
                seen["prompt"] = prompt
                return object()

        asyncio.run(TaskPrefixClient(_Inner(), Step.EVENT_KIND).generate("本文"))  # type: ignore[arg-type]
        return seen["prompt"]

    def test_model_trained_without_prefix_sees_the_original_prompt(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)

        assert self._sent("kuebiko-sft:n17c") == "本文"

    def test_prefix_trained_model_sees_the_marker(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)

        assert self._sent("kuebiko-sft:s19") == "[task: kind]\n本文"


def test_untrained_step_gets_no_marker_even_for_prefix_trained_model() -> None:
    from src.tools.task_prefix import with_task_prefix

    # n19 は事象ニュースを接頭辞つきで学習したが、深掘りは学習していない
    got = with_task_prefix("本文", Step.EVENT_NEWS, "kuebiko-sft:n19")
    assert got.startswith("[task: event_news]")
    assert with_task_prefix("本文", Step.DIGEST_DEEP_DIVE, "kuebiko-sft:n19") == "本文"
    assert with_task_prefix("本文", Step.DIGEST_DEEP_DIVE_SELECT, "kuebiko-sft:n19") == "本文"
