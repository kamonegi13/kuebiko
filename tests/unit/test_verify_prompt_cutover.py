"""プロンプト切替の合否判定 (scripts/verify_prompt_cutover.py) の純粋ロジックの unit test。

判定は ``evaluate_cutover`` に閉じており、DB にも時刻にも触れない。ここでは
``build_field_stats`` の戻り値と同じ形の dict を 2 つ渡して、ゲートが仕様どおりに
効くこと (と、効いてはいけないものが効かないこと) を固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from verify_prompt_cutover import (  # noqa: E402
    MIN_POST_ARTICLES,
    ROLE_EXCLUDED,
    ROLE_GATE,
    ROLE_REFERENCE,
    STATUS_FAIL,
    STATUS_INCONCLUSIVE,
    STATUS_PASS,
    evaluate_cutover,
)

_ARTICLES = 500  # MIN_POST_ARTICLES を十分に超える標本


def _stat(
    field_id: str,
    *,
    availability: str = "full",
    rate: float | None = None,
    total: int = _ARTICLES,
    distribution: dict[str, float] | None = None,
    average: float | None = None,
) -> dict[str, Any]:
    """1 フィールドぶんの stats。``rate`` / ``distribution`` を渡さなければ持たせない。"""
    coverage = None
    if rate is not None:
        coverage = {
            "filled": int(round(rate * total)),
            "total": total,
            "rate": rate,
            "scope_label": "配信済み記事",
        }
    buckets = [
        {"value": value, "label": None, "vocab": None, "count": 0, "share": share}
        for value, share in (distribution or {}).items()
    ]
    return {
        "field_id": field_id,
        "availability": availability,
        "source_note": "",
        "notes": [],
        "coverage": coverage,
        "distribution": buckets,
        "sub_metrics": [],
        "average": None
        if average is None
        else {"label": "平均文字数", "value": average, "unit": "字"},
    }


def _stats(count: int, fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "window": {"days": 7, "since": "2026-08-09T07:01:26Z", "until": "2026-08-16T07:01:26Z"},
        "denominator": {"label": "配信済み記事", "count": count, "note": ""},
        "generated_at": "2026-08-16T07:01:26Z",
        "fields": fields,
    }


def _baseline_fields() -> dict[str, dict[str, Any]]:
    """PASS になる健全な断面 (各テストはこの写しを 1 か所だけ壊す)。"""
    return {
        "title_ja": _stat("title_ja", availability="partial", rate=0.82),
        "summary": _stat("summary", rate=0.99, average=320.0),
        "importance": _stat("importance", distribution={"high": 0.10, "medium": 0.50, "low": 0.40}),
        "category": _stat("category", distribution={"apt": 0.30, "vulnerability": 0.70}),
        "article_type": _stat("article_type", distribution={"news": 0.90, "analysis": 0.10}),
        "victim_sector": _stat("victim_sector", rate=0.60, total=200),
        "analyst_note": _stat("analyst_note", availability="none"),
    }


def _row(verdict: Any, field_id: str, metric: str, label: str | None = None) -> Any:
    for row in verdict.rows:
        matches_label = label is None or row.label == label
        if row.field_id == field_id and row.metric == metric and matches_label:
            return row
    raise AssertionError(f"row not found: {field_id}/{metric}/{label}")


class TestSampleSize:
    def test_undersized_post_window_is_inconclusive(self) -> None:
        # Arrange: 切替直後で post 窓に 12 件しかない
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(12, _baseline_fields())

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert: PASS でも FAIL でもない明示的な状態 (exit 2)
        assert verdict.status == STATUS_INCONCLUSIVE
        assert verdict.exit_code == 2
        assert any("切替後の窓" in reason for reason in verdict.reasons)

    def test_inconclusive_suspends_all_verdicts(self) -> None:
        # Arrange: 標本不足かつ coverage が大きく低下している (判定してはいけない)
        broken = _baseline_fields()
        broken["summary"] = _stat("summary", rate=0.10, total=12, average=320.0)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(12, broken)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert: FAIL を出さず、閾値超過も記録しない
        assert verdict.status == STATUS_INCONCLUSIVE
        assert verdict.failures == ()

    def test_undersized_pre_window_is_inconclusive(self) -> None:
        # Arrange: 切替前の基準線が薄いと差の意味が決まらない
        pre = _stats(20, _baseline_fields())
        post = _stats(_ARTICLES, _baseline_fields())

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_INCONCLUSIVE
        assert any("切替前の窓" in reason for reason in verdict.reasons)

    def test_min_articles_is_overridable(self) -> None:
        # Arrange
        pre = _stats(50, _baseline_fields())
        post = _stats(50, _baseline_fields())

        # Act
        verdict = evaluate_cutover(pre, post, min_articles=10)

        # Assert
        assert verdict.status == STATUS_PASS
        assert verdict.min_articles == 10


class TestCoverageGates:
    def test_no_change_passes(self) -> None:
        # Arrange
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, _baseline_fields())

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_PASS
        assert verdict.exit_code == 0
        assert verdict.failures == ()

    def test_summary_coverage_drop_fails(self) -> None:
        # Arrange: 「JSON のみ出力してください」削除の影響 = summary 充足率 2 ポイント低下
        after = _baseline_fields()
        after["summary"] = _stat("summary", rate=0.97, average=320.0)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert verdict.exit_code == 1
        assert any(failure.startswith("summary /") for failure in verdict.failures)

    def test_title_ja_is_judged_despite_being_a_proxy_metric(self) -> None:
        # 削除した強調文の直接の標的なので、partial でも明示ゲートとして判定する
        after = _baseline_fields()
        after["title_ja"] = _stat("title_ja", availability="partial", rate=0.79)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert _row(verdict, "title_ja", "coverage").role == ROLE_GATE

    def test_small_coverage_drop_within_generic_threshold_passes(self) -> None:
        # Arrange: 明示ゲート以外の自然変動 (実測で週次 7〜13 pt 揺れる) は通す。
        # ここで FAIL すると狼少年になり、本物の破局を無視する習慣がついてしまう。
        after = _baseline_fields()
        after["victim_sector"] = _stat("victim_sector", rate=0.47, total=200)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_PASS

    def test_generic_coverage_drop_beyond_threshold_fails(self) -> None:
        # Arrange: 「フィールドが丸ごと死んだ」級の破局のみを捕まえる網 (自然変動の
        # 監視は weekly-fill-rate-audit が中央値基準で担当する)
        after = _baseline_fields()
        after["victim_sector"] = _stat("victim_sector", rate=0.20, total=200)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert any(failure.startswith("victim_sector /") for failure in verdict.failures)

    def test_coverage_increase_never_fails(self) -> None:
        # Arrange: 遵守率が上がるのは劣化ではない
        after = _baseline_fields()
        after["victim_sector"] = _stat("victim_sector", rate=0.95, total=200)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act / Assert
        assert evaluate_cutover(pre, post).status == STATUS_PASS

    def test_tiny_scope_is_demoted_to_reference(self) -> None:
        # Arrange: scope 分母 10 件では 1 件の増減が 10 ポイント動く (誤 FAIL を防ぐ)
        before = _baseline_fields()
        before["victim_sector"] = _stat("victim_sector", rate=0.90, total=10)
        after = _baseline_fields()
        after["victim_sector"] = _stat("victim_sector", rate=0.20, total=10)

        # Act
        verdict = evaluate_cutover(_stats(_ARTICLES, before), _stats(_ARTICLES, after))

        # Assert
        assert verdict.status == STATUS_PASS
        assert _row(verdict, "victim_sector", "coverage").role == ROLE_REFERENCE


class TestDistributionGates:
    def test_importance_shift_fails(self) -> None:
        # Arrange: high が 10 → 40% (判定傾向そのものの変質)。実測の自然変動は
        # 最大 13.5 pt なので、それを超える水準で初めて FAIL にする。
        after = _baseline_fields()
        after["importance"] = _stat(
            "importance", distribution={"high": 0.40, "medium": 0.20, "low": 0.40}
        )
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert any("importance / 分布 high" in failure for failure in verdict.failures)

    def test_article_type_shift_fails(self) -> None:
        # Arrange: 実際に検出した破局 (分類器の投入で「未設定」が -94.9 pt) の縮小版
        after = _baseline_fields()
        after["article_type"] = _stat("article_type", distribution={"news": 0.60, "analysis": 0.40})
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act / Assert
        assert evaluate_cutover(pre, post).status == STATUS_FAIL

    def test_shift_within_threshold_passes(self) -> None:
        # Arrange: 5 ポイントの揺れは許容
        after = _baseline_fields()
        after["category"] = _stat("category", distribution={"apt": 0.35, "vulnerability": 0.65})
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act / Assert
        assert evaluate_cutover(pre, post).status == STATUS_PASS

    def test_value_missing_from_one_window_is_compared_as_zero(self) -> None:
        # Arrange: 切替後に low が消えた (= share 0)
        after = _baseline_fields()
        after["importance"] = _stat("importance", distribution={"high": 0.10, "medium": 0.90})
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert _row(verdict, "importance", "share", "分布 low").post == 0.0


class TestAverageLength:
    def test_summary_length_change_beyond_ratio_fails(self) -> None:
        # Arrange: 320 → 250 字 (-21.9%)
        after = _baseline_fields()
        after["summary"] = _stat("summary", rate=0.99, average=250.0)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        assert verdict.status == STATUS_FAIL
        assert any("summary / 平均文字数" in failure for failure in verdict.failures)

    def test_summary_length_change_within_ratio_passes(self) -> None:
        # Arrange: 320 → 300 字 (-6.3%)
        after = _baseline_fields()
        after["summary"] = _stat("summary", rate=0.99, average=300.0)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, after)

        # Act / Assert
        assert evaluate_cutover(pre, post).status == STATUS_PASS


class TestAvailability:
    def test_none_availability_is_excluded_from_comparison(self) -> None:
        # Arrange: DB に列が無いフィールドは 0% ではなく「測れない」
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, _baseline_fields())

        # Act
        verdict = evaluate_cutover(pre, post)

        # Assert
        row = _row(verdict, "analyst_note", "coverage")
        assert row.role == ROLE_EXCLUDED
        assert (row.pre, row.post, row.delta) == (None, None, None)
        assert "analyst_note" in verdict.excluded_fields
        assert row.note

    def test_field_that_loses_its_column_is_not_read_as_a_100pt_drop(self) -> None:
        # Arrange: 切替前は full で 90%、切替後は統計なし
        before = _baseline_fields()
        before["victim_sector"] = _stat("victim_sector", rate=0.90, total=200)
        after = _baseline_fields()
        after["victim_sector"] = _stat("victim_sector", availability="none")

        # Act
        verdict = evaluate_cutover(_stats(_ARTICLES, before), _stats(_ARTICLES, after))

        # Assert
        assert verdict.status == STATUS_PASS
        assert _row(verdict, "victim_sector", "coverage").role == ROLE_EXCLUDED
        assert "victim_sector" in verdict.excluded_fields

    def test_partial_field_does_not_affect_the_verdict(self) -> None:
        # Arrange: 代理指標が 60 ポイント落ちても判定には使わない (基準の変化と切り分け不能)
        before = _baseline_fields()
        before["diamond"] = _stat("diamond", availability="partial", rate=0.80)
        after = _baseline_fields()
        after["diamond"] = _stat("diamond", availability="partial", rate=0.20)

        # Act
        verdict = evaluate_cutover(_stats(_ARTICLES, before), _stats(_ARTICLES, after))

        # Assert
        assert verdict.status == STATUS_PASS
        row = _row(verdict, "diamond", "coverage")
        assert row.role == ROLE_REFERENCE
        assert row.delta == -60.0
        assert "diamond" in verdict.partial_fields
        assert row.note

    def test_partial_distribution_is_reference_only(self) -> None:
        # Arrange: partial フィールドの分布が激変しても判定に使わない
        before = _baseline_fields()
        before["diamond"] = _stat(
            "diamond", availability="partial", distribution={"espionage": 0.90, "unknown": 0.10}
        )
        after = _baseline_fields()
        after["diamond"] = _stat(
            "diamond", availability="partial", distribution={"espionage": 0.10, "unknown": 0.90}
        )

        # Act
        verdict = evaluate_cutover(_stats(_ARTICLES, before), _stats(_ARTICLES, after))

        # Assert
        assert verdict.status == STATUS_PASS
        assert all(row.role == ROLE_REFERENCE for row in verdict.rows if row.field_id == "diamond")


class TestVerdictShape:
    def test_default_min_articles_matches_the_documented_gate(self) -> None:
        assert MIN_POST_ARTICLES == 300

    def test_rows_carry_field_ids_only_and_no_free_text_from_articles(self) -> None:
        # 記事本文・タイトルを出力に載せない不変量 (ラベルは語彙 enum とフィールド ID のみ)
        pre = _stats(_ARTICLES, _baseline_fields())
        post = _stats(_ARTICLES, _baseline_fields())
        verdict = evaluate_cutover(pre, post)
        known = set(_baseline_fields())
        assert {row.field_id for row in verdict.rows} <= known
