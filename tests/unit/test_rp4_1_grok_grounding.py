"""RP-4-1 の純関数のテスト。"""

from __future__ import annotations

from scripts.rp4_1_grok_grounding import logic

# 2026-10-26 12:00 UTC 付近の snowflake (窓の中)
_IN = str((1793016000000 - 1288834974657) << 22)
_OLD = str((1792000000000 - 1288834974657) << 22)


def _row(i: int, sid: str, **kw: object) -> dict[str, object]:
    base = {
        "id": i, "url": f"https://x.com/a/status/{sid}", "body": "b", "summary": "s",
        "created_at": f"2026-10-26T00:00:0{i}", "article_id": f"a{i}",
    }  # fmt: skip
    return {**base, **kw}


def test_posted_at_roundtrip() -> None:
    assert logic.posted_at(_IN).year == 2026


def test_fold_keeps_earliest_and_counts_exclusions() -> None:
    rows = [
        _row(2, _IN),
        _row(1, _IN),
        _row(3, _OLD),
        _row(4, "x", url="https://x.com/none"),
        _row(5, _IN + "0", body=""),
    ]
    kept, f = logic.fold_population(rows)
    assert [r["id"] for r in kept] == [1]
    assert (f.no_status, f.empty_body_or_summary, f.duplicate_rows, f.outside_window) == (
        1,
        1,
        1,
        1,
    )


def test_fire_reasons_flags_number_not_in_body() -> None:
    row = {
        "summary": "版 7.0.2u4 が悪用",
        "body": "ESXi exploit",
        "title": "",
        "published_at": "2026-10-26",
    }
    assert any(r.startswith("number:") for r in logic.fire_reasons(row))


def test_draw_sample_all_when_few_and_deterministic() -> None:
    f, n = logic.draw_sample(["3", "1"], [str(i) for i in range(10, 30)])
    assert f == ["1", "3"] and len(n) == logic.NOFIRE_SAMPLE_N
    assert (f, n) == logic.draw_sample(["1", "3"], [str(i) for i in range(10, 30)])


def test_decide_lines() -> None:
    kw = {"population": 500, "fired": 50, "failed": 0, "sample_n": 36}
    assert logic.decide(judged=30, errors=27, **kw)[0] == "on"
    assert logic.decide(judged=30, errors=20, **kw)[0] == "shadow"
    assert logic.decide(judged=30, errors=10, **kw)[0] == "off"
    assert logic.decide(judged=30, errors=27, **{**kw, "fired": 100})[0] == "shadow"
    assert logic.decide(judged=30, errors=27, **{**kw, "population": 50})[0] == "hold"
    assert logic.decide(judged=15, errors=15, **{**kw, "fired": 15})[0] == "shadow"
