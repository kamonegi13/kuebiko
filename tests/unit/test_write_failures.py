"""src.ops.write_failures のテスト (P0-10): 記録の失敗を黙って飲まず、計数して見えるようにする。"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from src.ops import write_failures


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    write_failures.reset_for_tests()
    yield
    write_failures.reset_for_tests()


def test_snapshot_is_empty_initially() -> None:
    assert dict(write_failures.snapshot()) == {}


def test_record_failure_counts_per_component() -> None:
    write_failures.record_failure("llm_usage", ValueError("x"))
    write_failures.record_failure("llm_usage", RuntimeError("y"))
    write_failures.record_failure("detect_score", OSError("z"))
    assert dict(write_failures.snapshot()) == {"llm_usage": 2, "detect_score": 1}


def test_snapshot_is_an_immutable_copy() -> None:
    write_failures.record_failure("a", ValueError("x"))
    snap = write_failures.snapshot()
    with pytest.raises(TypeError):
        snap["a"] = 99  # type: ignore[index]
    write_failures.record_failure("a", ValueError("x"))
    assert snap["a"] == 1  # 取得後の加算は写しに影響しない
    assert write_failures.snapshot()["a"] == 2


def test_log_has_exception_type_only(caplog: pytest.LogCaptureFixture) -> None:
    secret = "SECRET-TOKEN-VALUE"
    with caplog.at_level("WARNING"):
        write_failures.record_failure("comp", ValueError(secret))
    text = caplog.text
    assert "ops_write_failed" in text
    assert "ValueError" in text
    assert secret not in text


def test_thread_safe_counting() -> None:
    def work() -> None:
        for _ in range(200):
            write_failures.record_failure("c", ValueError("x"))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert write_failures.snapshot()["c"] == 1600
