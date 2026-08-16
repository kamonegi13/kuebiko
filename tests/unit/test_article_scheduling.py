"""記事処理のスケジューリング (逐次/並列の共通経路) のテスト。

2026-08-17: LLM 呼出の実測で「decode 主体の処理は並列化で 1.56x、prefill 主体は
0.68x」と分かったため、記事処理 (decode 主体) だけを並列化できるようにした。
既定は concurrency=1 で**現行の逐次挙動と完全に一致すること**が最重要の不変量。

soft deadline は「新規の着手を止める / 実行中は完走させる」という現行の意味を保つ。
"""

from __future__ import annotations

import asyncio

import pytest

from src.pipeline.orchestrator import _article_concurrency, _run_articles_bounded


class TestArticleConcurrencyEnv:
    def test_default_is_serial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARTICLE_CONCURRENCY", raising=False)
        assert _article_concurrency() == 1

    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("4", 4), ("8", 8)])
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
        monkeypatch.setenv("ARTICLE_CONCURRENCY", raw)
        assert _article_concurrency() == expected

    @pytest.mark.parametrize("raw", ["0", "-3", "abc", "", "   "])
    def test_invalid_falls_back_to_serial(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """壊れた値で並列度が暴走しないこと (安全側 = 逐次)。"""
        monkeypatch.setenv("ARTICLE_CONCURRENCY", raw)
        assert _article_concurrency() == 1

    def test_upper_bound_is_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """誤設定で Ollama を溢れさせないよう上限で頭打ちにする。"""
        monkeypatch.setenv("ARTICLE_CONCURRENCY", "999")
        assert _article_concurrency() == 8


class TestRunArticlesBounded:
    @pytest.mark.asyncio
    async def test_serial_preserves_call_order(self) -> None:
        called: list[int] = []

        async def handler(item: int) -> str:
            called.append(item)
            return f"r{item}"

        results, deferred = await _run_articles_bounded(
            [0, 1, 2, 3], handler, concurrency=1, should_stop=None
        )
        assert called == [0, 1, 2, 3]
        assert results == ["r0", "r1", "r2", "r3"]
        assert deferred == 0

    @pytest.mark.asyncio
    async def test_results_stay_in_input_order_when_parallel(self) -> None:
        """完了順がばらけても戻り値は入力順 (下流の突合が壊れない)。"""

        async def handler(item: int) -> str:
            await asyncio.sleep((4 - item) * 0.01)  # 後の要素ほど早く終わる
            return f"r{item}"

        results, deferred = await _run_articles_bounded(
            [0, 1, 2, 3], handler, concurrency=4, should_stop=None
        )
        assert results == ["r0", "r1", "r2", "r3"]
        assert deferred == 0

    @pytest.mark.asyncio
    async def test_concurrency_is_capped(self) -> None:
        live = 0
        peak = 0

        async def handler(item: int) -> int:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return item

        await _run_articles_bounded(list(range(12)), handler, concurrency=3, should_stop=None)
        assert peak == 3

    @pytest.mark.asyncio
    async def test_stop_before_start_defers_everything(self) -> None:
        async def handler(item: int) -> int:  # pragma: no cover - 呼ばれない
            raise AssertionError("deadline 超過後に着手してはいけない")

        results, deferred = await _run_articles_bounded(
            [0, 1, 2], handler, concurrency=2, should_stop=lambda: True
        )
        assert results == []
        assert deferred == 3

    @pytest.mark.asyncio
    async def test_stop_midway_defers_remainder_and_finishes_inflight(self) -> None:
        """deadline を跨いでも着手済みは完走し、未着手だけが繰越になる。"""
        started: list[int] = []

        async def handler(item: int) -> int:
            started.append(item)
            await asyncio.sleep(0.01)
            return item

        results, deferred = await _run_articles_bounded(
            list(range(6)),
            handler,
            concurrency=1,
            should_stop=lambda: len(started) >= 2,
        )
        assert started == [0, 1]
        assert results == [0, 1]  # 着手済みは完走して結果に載る
        assert deferred == 4

    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        async def handler(item: int) -> int:  # pragma: no cover - 呼ばれない
            raise AssertionError("空入力で handler を呼んではいけない")

        assert await _run_articles_bounded([], handler, concurrency=4, should_stop=None) == ([], 0)

    @pytest.mark.asyncio
    async def test_handler_exception_propagates(self) -> None:
        """handler は自分で例外を処理する契約。漏れたら握り潰さず上げる。

        現行の逐次ループも本体の except で拾い切れない例外は run 全体へ伝播するため、
        並列化でここを握り潰すと障害が見えなくなる。
        """

        async def handler(item: int) -> int:
            if item == 2:
                raise ValueError("boom")
            return item

        with pytest.raises(ValueError, match="boom"):
            await _run_articles_bounded([0, 1, 2, 3], handler, concurrency=2, should_stop=None)
