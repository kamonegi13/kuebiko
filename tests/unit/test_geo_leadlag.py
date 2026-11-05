"""GEO-LEADLAG-MONTHLY の決定論部品のテスト (系列・山・置換・欠測)。"""

from __future__ import annotations

from datetime import date, timedelta

from scripts.replay import _geo_leadlag as gl
from scripts.replay._sqlguard import assert_read_only_sql
from scripts.replay.geo_leadlag_run import ENTITIES_SQL, NEW_FEEDS_SQL, build_articles_sql

D0 = date(2026, 10, 29)


def _fact(**kw: object) -> gl.ArticleFacts:
    base: dict[str, object] = {
        "day": D0,
        "category": "geopolitical",
        "parties": frozenset(),
        "victim_iso": "",
        "subject_nations": frozenset(),
        "has_hacktivist_subject": False,
        "intent": "",
        "intent_confidence": "",
    }
    return gl.ArticleFacts(**{**base, **kw})  # type: ignore[arg-type]


def test_geo_article_columns() -> None:
    cols = gl.article_columns(_fact(parties=frozenset({"RU", "JP", "UA"})))
    assert cols == {"G", "G_RU", "G_ADVJA", "G_RUUA"}


def test_cyber_article_columns_and_weak_intent() -> None:
    strong = _fact(category="apt", victim_iso="US", intent="espionage", intent_confidence="high")
    weak = _fact(category="apt", victim_iso="US", intent="espionage", intent_confidence="low")
    assert gl.article_columns(strong) == {"T", "C2"}
    assert gl.article_columns(weak) == {"T"}


def test_state_subject_counts_c1_and_c2_without_intent() -> None:
    f = _fact(category="malware", victim_iso="JP", subject_nations=frozenset({"CN"}))
    assert gl.article_columns(f) == {"T", "C1_CN", "C2"}


def test_hacktivist_by_intent_or_subject_and_other_category_ignored() -> None:
    assert "C3" in gl.article_columns(_fact(category="incident", has_hacktivist_subject=True))
    assert "C3" in gl.article_columns(
        _fact(category="incident", intent="hacktivism", intent_confidence="medium")
    )
    assert gl.article_columns(_fact(category="other")) == frozenset()


def test_ratio_series_marks_missing_below_floor() -> None:
    counts = [{"T": 19, "C3": 5}, {"T": 20, "C3": 5}]
    assert gl.ratio_series(counts, "C3", "T", 20) == [None, 0.25]


def test_missing_days_lists() -> None:
    days = [D0, D0 + timedelta(days=1)]
    counts = [dict.fromkeys(gl.COLUMNS, 0), {**dict.fromkeys(gl.COLUMNS, 0), "G": 10, "T": 20}]
    m = gl.missing_days(days, counts)
    assert m["geo_missing"] == [D0.isoformat()]
    assert m["cyber_missing"] == [D0.isoformat()]


def test_month_groups_fold_start_month_into_next() -> None:
    days = gl.date_range(D0, date(2026, 12, 2))
    groups = gl.month_groups(days)
    assert [len(g) for g in groups] == [33, 2]


def test_regime_groups_merge_short_runs() -> None:
    days = gl.date_range(D0, date(2026, 11, 30))
    groups = gl.regime_groups(days, [date(2026, 10, 31), date(2026, 11, 3)])
    assert sorted(i for g in groups for i in g) == list(range(len(days)))
    assert all(len(g) >= gl.MIN_REGIME_DAYS for g in groups)


def test_onset_requires_percentile_numerator_and_first_day_only() -> None:
    num = [0] * 20
    den = [10] * 20
    num[5] = 5
    num[6] = 5  # 続く 2 日目は onset にしない
    num[10] = 1  # 分子 1 は山にしない
    flags = gl.onset_flags(num, den, [list(range(20))])
    assert [i for i, f in enumerate(flags) if f] == [5]


def test_onset_skipped_when_over_half_missing() -> None:
    num = [5] * 10
    den = [10] * 4 + [1] * 6
    assert not any(gl.onset_flags(num, den, [list(range(10))]))


def test_usable_pairs_drops_out_of_range_and_sparse_windows() -> None:
    n = 40
    r: list[float | None] = [0.1] * n
    onset = [False] * n
    onset[5] = True  # pre が範囲外
    onset[20] = True
    onset[39] = True  # post が範囲外
    pairs = gl.usable_pairs(onset, r, gl.WINDOWS["W1"])
    assert len(pairs) == 1
    assert abs(pairs[0].diff) < 1e-12
    sparse: list[float | None] = [None] * n
    assert gl.usable_pairs(onset, sparse, gl.WINDOWS["W1"]) == []


def test_effect_relative_none_when_pre_zero() -> None:
    assert gl.effect([gl.Pair(0.1, 0.0)]) == (0.1, None)
    assert gl.effect([]) is None


def test_holm_adjust_monotone() -> None:
    out = gl.holm_adjust({"a": 0.01, "b": 0.04, "c": 0.03})
    assert abs(out["a"] - 0.03) < 1e-12
    assert abs(out["c"] - 0.06) < 1e-12
    assert abs(out["b"] - 0.06) < 1e-12


def _synthetic(n: int) -> list[dict[str, int]]:
    rows = []
    for i in range(n):
        c = dict.fromkeys(gl.COLUMNS, 0)
        c["G"], c["T"] = 20, 40
        c["G_RUUA"] = 6 if i % 7 == 3 else 1
        c["C3"] = 20 if 3 < i % 7 < 6 else 4
        rows.append(c)
    return rows


def test_run_test_is_deterministic_and_enumerates_all_shifts() -> None:
    counts = _synthetic(300)
    groups = [list(range(300))]
    a = gl.run_test("H3", "W1", counts, groups)
    b = gl.run_test("H3", "W1", counts, groups)
    assert a == b
    assert not a.insufficient
    assert a.p_raw is not None
    assert a.p_raw >= 1 / (300 - 59 + 1)
    assert a.ci_low is not None
    assert a.ci_high is not None
    assert a.ci_low <= a.ci_high


def test_small_sample_is_insufficient() -> None:
    res = gl.run_test("H3", "W1", _synthetic(50), [list(range(50))])
    assert res.insufficient
    assert res.p_raw is None


def test_primary_family_has_six_tests() -> None:
    res = gl.run_primary_family(_synthetic(300), [list(range(300))])
    assert {(r.hypothesis, r.window) for r in res} == {
        (h, w) for h in ("H1", "H2", "H3") for w in ("W1", "W2")
    }


def test_queries_pass_read_only_guard() -> None:
    for sql in (build_articles_sql(), ENTITIES_SQL, NEW_FEEDS_SQL):
        assert_read_only_sql(sql)
