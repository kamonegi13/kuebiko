"""プロンプト切替の合否判定 (scripts/verify_prompt_cutover.py) の純粋ロジックの unit test。

判定は ``evaluate_cutover`` に閉じており、DB にも時刻にも触れない。ここでは
``build_field_stats`` の戻り値と同じ形の dict を 2 つ渡して、ゲートが仕様どおりに
効くこと (と、効いてはいけないものが効かないこと) を固定する。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.storage.config_store import ConfigVersion

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from verify_prompt_cutover import (  # noqa: E402
    MIN_POST_ARTICLES,
    ROLE_EXCLUDED,
    ROLE_GATE,
    ROLE_REFERENCE,
    STATUS_FAIL,
    STATUS_INCONCLUSIVE,
    STATUS_PASS,
    CutoverVerdict,
    changes_within,
)
from verify_prompt_cutover import evaluate_cutover as _evaluate_cutover  # noqa: E402


def evaluate_cutover(pre: dict[str, Any], post: dict[str, Any], **kwargs: Any) -> CutoverVerdict:
    """閾値ゲートの test 用ラッパ。

    ``version_checked`` の既定は production 側で **False (未検査=判定不能)** に倒して
    あるため、閾値そのものを固定する本 file では「版検査は済んでいる」を既定に置く。
    窓の純度ゲート自体の test は生の ``_evaluate_cutover`` を直接呼ぶ。
    """
    kwargs.setdefault("version_checked", True)
    return _evaluate_cutover(pre, post, **kwargs)


_ARTICLES = 500  # MIN_POST_ARTICLES を十分に超える標本


def _version(version: int, created_at: str) -> ConfigVersion:
    """判定基準の版履歴 1 行 (note は判定に使わないので空)。"""
    return ConfigVersion(version=version, note="", created_at=created_at)


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


class TestCatastrophicGate:
    """C7 破局ゲート (2026-08-22 H6): 無人 rollback の作動対象は分布と全滅級のみ。"""

    def test_distribution_shift_is_catastrophic(self) -> None:
        from verify_prompt_cutover import is_catastrophic_row

        after = _baseline_fields()
        after["importance"] = _stat(
            "importance", distribution={"high": 0.40, "medium": 0.20, "low": 0.40}
        )
        verdict = evaluate_cutover(_stats(_ARTICLES, _baseline_fields()), _stats(_ARTICLES, after))
        assert verdict.status == STATUS_FAIL
        assert any(is_catastrophic_row(r) for r in verdict.rows)

    def test_small_explicit_coverage_drop_is_not_catastrophic(self) -> None:
        # summary 充足率 -2pt は総合 FAIL だが、無人 rollback の作動条件にはならない
        # (片側 Goodhart ラチェット防止 — 人間向け報告にのみ残る)
        from verify_prompt_cutover import is_catastrophic_row

        after = _baseline_fields()
        after["summary"] = _stat("summary", rate=0.97, average=320.0)
        verdict = evaluate_cutover(_stats(_ARTICLES, _baseline_fields()), _stats(_ARTICLES, after))
        assert verdict.status == STATUS_FAIL
        assert not any(is_catastrophic_row(r) for r in verdict.rows)

    def test_summary_length_change_is_not_catastrophic(self) -> None:
        # ブレビティ改善 (要約長 -20%) を無人で巻き戻さない
        from verify_prompt_cutover import is_catastrophic_row

        after = _baseline_fields()
        after["summary"] = _stat("summary", rate=0.99, average=256.0)
        verdict = evaluate_cutover(_stats(_ARTICLES, _baseline_fields()), _stats(_ARTICLES, after))
        assert verdict.status == STATUS_FAIL
        assert not any(is_catastrophic_row(r) for r in verdict.rows)


class TestWindowPurity:
    """窓の中で判定基準がさらに動いていたら判定を出さない (2026-08-19 の関門)。

    実例: 08-16 の層分けを 7 日窓で測ると v3 (08-17) / v4 (08-18) が窓に入る。
    「注意して読む」ではコマンドが平然と PASS を返すので、コード側で止める。
    """

    def _clean(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return (_stats(_ARTICLES, _baseline_fields()), _stats(_ARTICLES, _baseline_fields()))

    def test_version_landing_inside_post_window_blocks_the_verdict(self) -> None:
        # Arrange: 切替後の窓の中で判定基準が v4 に上がっている
        pre, post = self._clean()

        # Act
        verdict = _evaluate_cutover(
            pre,
            post,
            version_checked=True,
            post_changes=(_version(4, "2026-08-18T11:07:53+00:00"),),
        )

        # Assert: 「切替後」が単一の状態でないので PASS/FAIL を付けない
        assert verdict.status == STATUS_INCONCLUSIVE
        assert any("単一の状態でない" in reason for reason in verdict.reasons)

    def test_allow_mixed_downgrades_the_block_to_a_warning(self) -> None:
        # Arrange: 承知の上で混成窓を見るときだけ判定を続行する
        pre, post = self._clean()

        # Act
        verdict = _evaluate_cutover(
            pre,
            post,
            version_checked=True,
            post_changes=(_version(4, "2026-08-18T11:07:53+00:00"),),
            allow_mixed=True,
        )

        # Assert: 判定は出るが、混成した事実は必ず本文に残る
        assert verdict.status == STATUS_PASS
        assert any("単一の状態でない" in warning for warning in verdict.warnings)

    def test_unchecked_history_is_inconclusive_not_clean(self) -> None:
        # Arrange: 版履歴を読めなかった (key 打ち間違い / DB 障害)
        pre, post = self._clean()

        # Act
        verdict = _evaluate_cutover(pre, post, version_checked=False)

        # Assert: 「検査していない」を「混成なし」に倒さない (fail-closed)
        assert verdict.status == STATUS_INCONCLUSIVE
        assert any("版履歴" in reason for reason in verdict.reasons)

    def test_version_inside_pre_window_only_warns(self) -> None:
        # Arrange: 基準線側の混成は比較の相手が blend になるだけで致命ではない
        pre, post = self._clean()

        # Act
        verdict = _evaluate_cutover(
            pre,
            post,
            version_checked=True,
            pre_changes=(_version(3, "2026-08-17T21:44:39+00:00"),),
        )

        # Assert
        assert verdict.status == STATUS_PASS
        assert any("基準線が混成" in warning for warning in verdict.warnings)


class TestChangesWithin:
    """窓に入った版の抽出。境界の扱いを固定する。"""

    _HISTORY = (
        _version(1, "2026-08-16T07:01:26+00:00"),
        _version(2, "2026-08-16T07:12:08+00:00"),
        _version(3, "2026-08-17T21:44:39+00:00"),
        _version(4, "2026-08-18T11:07:53+00:00"),
    )
    _CUTOVER = datetime(2026, 8, 16, 7, 1, 26, tzinfo=UTC)

    def test_cutover_version_itself_is_not_contamination(self) -> None:
        # Arrange/Act: 切替時刻ちょうどの v1 は「切替そのもの」
        found = changes_within(
            self._HISTORY,
            since=self._CUTOVER,
            until=self._CUTOVER + timedelta(days=7),
            include_since=False,
        )

        # Assert: v1 は除き、窓に入った v2/v3/v4 だけを汚染として挙げる
        assert [v.version for v in found] == [2, 3, 4]

    def test_pre_window_includes_a_version_sitting_on_its_left_edge(self) -> None:
        # Arrange/Act: 切替前の窓 [v1, cutover) — 左端に乗った版は基準線を混成させる
        found = changes_within(
            self._HISTORY,
            since=self._CUTOVER,
            until=self._CUTOVER + timedelta(days=1),
            include_since=True,
        )

        # Assert
        assert [v.version for v in found] == [1, 2]

    def test_unparsable_timestamp_is_kept_not_dropped(self) -> None:
        # Arrange: created_at が壊れている版 (落とすと fail-open になる)
        broken = (_version(9, "not-a-timestamp"),)

        # Act
        found = changes_within(
            broken,
            since=self._CUTOVER,
            until=self._CUTOVER + timedelta(days=1),
            include_since=False,
        )

        # Assert
        assert [v.version for v in found] == [9]

    def test_versions_outside_the_window_are_ignored(self) -> None:
        # Arrange/Act: 窓より後の版は関係ない
        found = changes_within(
            self._HISTORY,
            since=self._CUTOVER,
            until=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
            include_since=False,
        )

        # Assert
        assert [v.version for v in found] == [2]

    def test_sub_second_precision_does_not_make_the_cutover_version_contamination(self) -> None:
        # Arrange: created_at はマイクロ秒まで持つが --cutover は秒精度で書かれる
        history = (_version(1, "2026-08-16T07:01:26.696350+00:00"),)

        # Act
        found = changes_within(
            history,
            since=datetime(2026, 8, 16, 7, 1, 26, tzinfo=UTC),
            until=datetime(2026, 8, 23, 7, 1, 26, tzinfo=UTC),
            include_since=False,
        )

        # Assert: 0.7 ミリ秒の差で「切替そのもの」を汚染に数えない
        assert found == ()
