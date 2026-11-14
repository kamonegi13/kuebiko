"""要約の繰り返しの関門 (src/pipeline/summary_repetition.py)。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from src.pipeline.summary import SummaryOutput
from src.pipeline.summary_repetition import (
    GUARD_ENV,
    RETRY_TEMPERATURE_DELTA,
    check_summary_output,
    find_repetition,
    guard_summary_repetition,
    main,
    truncate_repetition,
)

_S = "英国の政府機関が相次いで侵害され、職員の情報が流出しました。"
_E = "The attacker exploited a flaw in the VPN appliance."


def _summary(**kw: Any) -> SummaryOutput:
    base: dict[str, Any] = {
        "title_ja": "見出し",
        "importance": "medium",
        "category": "breach",
        "summary": "正常な要約です。攻撃者は不明です。",
    }
    return SummaryOutput(**{**base, **kw})


def test_detects_japanese_sentence_repeated_many_times() -> None:
    m = find_repetition("導入の文章です、これは最初の文。" + _S * 257)

    assert m is not None
    assert m.count == 257
    assert m.first_index == 1
    assert m.sentence_chars == len(_S)


def test_two_repeats_are_not_detected() -> None:
    assert find_repetition(_S * 2) is None


def test_short_sentences_are_ignored() -> None:
    assert find_repetition("はい。" * 50) is None


def test_detects_english_repeat() -> None:
    m = find_repetition((_E + " ") * 5)

    assert m is not None and m.count == 5


def test_check_scans_free_text_fields_only() -> None:
    out = _summary(summary=_S * 4, analyst_note=_S * 3, bluf="正常な BLUF です。")

    got = check_summary_output(out)

    assert [m.field for m in got] == ["summary", "analyst_note"]
    assert check_summary_output(out.model_dump()) == got


def test_truncate_keeps_first_occurrence_and_is_idempotent() -> None:
    text = "冒頭の説明文はここにあります。" + _S * 10
    once = truncate_repetition(text)

    assert once == "冒頭の説明文はここにあります。" + _S
    assert truncate_repetition(once) == once
    assert find_repetition(once) is None


def test_truncate_without_repetition_is_noop() -> None:
    assert (
        truncate_repetition("正常な要約です。攻撃者は不明です。")
        == "正常な要約です。攻撃者は不明です。"
    )


async def _run(mode: str, monkeypatch: pytest.MonkeyPatch, retry: SummaryOutput) -> Any:
    monkeypatch.setenv(GUARD_ENV, mode)
    regen = AsyncMock(return_value=retry)
    bad = _summary(summary="最初の文章はこうです。" + _S * 20)
    with capture_logs() as logs:
        got = await guard_summary_repetition(
            bad, regen, base_temperature=0.3, article_id="a1", model="m"
        )
    return bad, got, regen, logs


async def test_shadow_logs_and_leaves_output_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    bad, got, regen, logs = await _run("shadow", monkeypatch, _summary())

    assert got is bad
    regen.assert_not_called()
    ev = [x for x in logs if x["event"] == "summary_repetition_detected"]
    assert len(ev) == 1 and ev[0]["repeat_count"] == 20 and ev[0]["field"] == "summary"
    assert _S not in json.dumps(logs, ensure_ascii=False)


async def test_on_uses_clean_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    clean = _summary(summary="作り直した正常な要約です。")
    _, got, regen, _ = await _run("1", monkeypatch, clean)

    assert got is clean
    regen.assert_awaited_once_with(pytest.approx(0.3 + RETRY_TEMPERATURE_DELTA))


async def test_on_truncates_when_retry_still_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    still_bad = _summary(summary=_S * 9)
    bad, got, regen, logs = await _run("1", monkeypatch, still_bad)

    assert regen.await_count == 1
    assert got.summary == "最初の文章はこうです。" + _S
    assert bad.summary != got.summary
    assert any(x["event"] == "summary_repetition_truncated" and x["retries"] == 1 for x in logs)


async def test_off_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    bad, got, regen, logs = await _run("0", monkeypatch, _summary())

    assert got is bad and logs == []
    regen.assert_not_called()


async def test_clean_output_is_untouched_in_on_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GUARD_ENV, "1")
    ok = _summary()
    regen = AsyncMock()

    assert await guard_summary_repetition(ok, regen, base_temperature=0.3) is ok
    regen.assert_not_called()


def test_cli_prints_counts_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    lines = [
        json.dumps({"output": {"summary": _S * 5}}),
        json.dumps({"output": {"summary": "正常な要約です。"}}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines)))

    assert main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"rows": 2, "repetitive_rows": 1, "by_field": {"summary": 1}}
