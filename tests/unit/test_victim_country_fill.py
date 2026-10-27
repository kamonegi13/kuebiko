"""被害国の取りこぼしを埋める規則 (src/cti/victim_country_fill.py) のテスト。

付けすぎは害が大きいので、「埋める」規則は決定論で保守的に作る。fixture の組織名は架空。
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.cti.victim_country_fill import (
    ORG_HISTORY_MIN_ARTICLES,
    FillMode,
    build_org_history,
    fill_mode,
    infer_victim_country,
)


def _infer(
    *,
    title: str = "",
    summary: str = "",
    orgs: list[str] | None = None,
    article_type: str = "breaking",
    category: str = "breach",
    history: dict[str, Counter[str]] | None = None,
) -> tuple[str, str] | None:
    fill = infer_victim_country(
        article_type=article_type,
        category=category,
        victim_orgs=orgs or [],
        title=title,
        summary=summary,
        org_history=history,
    )
    return None if fill is None else (fill.iso, fill.rule)


class TestScope:
    def test_non_breaking_is_never_filled(self) -> None:
        assert _infer(article_type="research", orgs=["架空商事株式会社"]) is None

    def test_non_cyber_category_is_never_filled(self) -> None:
        assert _infer(category="geopolitical", orgs=["架空商事株式会社"]) is None

    def test_no_signal_returns_none(self) -> None:
        assert _infer(title="Ransomware group posts new victims", orgs=["Unknown"]) is None


class TestJapaneseLegalForm:
    def test_legal_form_in_victim_org_fills_jp(self) -> None:
        assert _infer(orgs=["架空商事株式会社"]) == ("JP", "jp_legal_form")

    def test_legal_form_in_summary_fills_jp(self) -> None:
        got = _infer(summary="架空食品株式会社は、自社サーバへの不正アクセスを公表した。")
        assert got == ("JP", "jp_legal_form")

    def test_foreign_nation_in_text_blocks(self) -> None:
        got = _infer(summary="架空電機株式会社の米国子会社に不正アクセスがあった。")
        assert got is None

    def test_subsidiary_wording_blocks(self) -> None:
        got = _infer(summary="架空電機株式会社の現地法人に不正アクセスがあった。")
        assert got is None

    def test_works_for_incident_and_apt_categories(self) -> None:
        assert _infer(category="incident", orgs=["架空運輸株式会社"]) is not None


class TestJapaneseNoticeTitle:
    def test_disclosure_notice_title_fills_jp(self) -> None:
        got = _infer(title="不正アクセスによる情報漏えいに関するお詫びとお知らせ(第2報)")
        assert got == ("JP", "jp_notice_title")

    def test_notice_with_subsidiary_wording_blocks(self) -> None:
        assert _infer(title="当社連結子会社における不正アクセスに関するお知らせ") is None

    def test_notice_with_foreign_nation_blocks(self) -> None:
        got = _infer(title="米国拠点の不正アクセスに関するお知らせ")
        assert got is None

    def test_investigation_result_word_alone_is_not_enough(self) -> None:
        assert _infer(title="再被害で調査結果公表、攻撃者は大量のアカウントを窃取") is None

    def test_notice_title_needs_breach_or_incident(self) -> None:
        assert _infer(category="malware", title="サービス再開のお知らせ") is None


class TestOrgLegalSuffix:
    @pytest.mark.parametrize(
        ("org", "iso"),
        [
            ("Example Holdings Pty Ltd", "AU"),
            ("Esempio Industriale S.p.A.", "IT"),
            ("Przyklad Sp. z o.o.", "PL"),
        ],
    )
    def test_unambiguous_suffix_fills(self, org: str, iso: str) -> None:
        assert _infer(orgs=[org]) == (iso, "org_legal_suffix")

    def test_ambiguous_suffix_is_not_used(self) -> None:
        assert _infer(orgs=["Example Trading Co., Ltd"]) is None
        assert _infer(orgs=["Beispiel GmbH"]) is None
        assert _infer(orgs=["Exemplo Comercio Ltda"]) is None  # BR/CL/CO で使われる
        assert _infer(orgs=["Esempio S.r.l."]) is None  # IT/RO/AR で使われる
        assert _infer(orgs=["Example (Pty) Ltd"]) is None  # ZA

    def test_multiple_orgs_with_different_suffix_countries_do_not_fill(self) -> None:
        assert _infer(orgs=["A Pty Ltd", "B S.p.A."]) is None


class TestOrgHistory:
    def _history(self, iso: str, n: int) -> dict[str, Counter[str]]:
        return {"example cloud": Counter({iso: n})}

    def test_consistent_history_fills(self) -> None:
        got = _infer(
            title="Example Cloud が侵害を公表",
            orgs=["Example Cloud"],
            history=self._history("US", ORG_HISTORY_MIN_ARTICLES),
        )
        assert got == ("US", "org_history")

    def test_org_missing_from_title_does_not_fill(self) -> None:
        got = _infer(
            title="AI エージェントが隔離環境から脱出",
            orgs=["Example Cloud"],
            history=self._history("US", 9),
        )
        assert got is None

    def test_too_few_articles_does_not_fill(self) -> None:
        got = _infer(
            title="Example Cloud が侵害を公表",
            orgs=["Example Cloud"],
            history=self._history("US", ORG_HISTORY_MIN_ARTICLES - 1),
        )
        assert got is None

    def test_conflicting_history_does_not_fill(self) -> None:
        history = {"example cloud": Counter({"US": 5, "NL": 1})}
        assert _infer(title="Example Cloud 侵害", orgs=["Example Cloud"], history=history) is None

    def test_list_posts_with_many_orgs_do_not_use_history(self) -> None:
        got = _infer(
            title="Example Cloud と Other Org",
            orgs=["Example Cloud", "Other Org"],
            history=self._history("US", 9),
        )
        assert got is None

    def test_history_none_is_safe(self) -> None:
        assert _infer(title="Example Cloud 侵害", orgs=["Example Cloud"], history=None) is None


class TestConflicts:
    def test_rules_that_disagree_return_none(self) -> None:
        history = {"架空商事株式会社": Counter({"US": 5})}
        got = _infer(
            title="架空商事株式会社に不正アクセス", orgs=["架空商事株式会社"], history=history
        )
        assert got is None

    def test_rules_that_agree_keep_the_first_rule(self) -> None:
        history = {"架空商事株式会社": Counter({"JP": 5})}
        got = _infer(
            title="架空商事株式会社に不正アクセス", orgs=["架空商事株式会社"], history=history
        )
        assert got == ("JP", "jp_legal_form")


class TestBuildOrgHistory:
    def test_only_single_org_articles_with_iso_count(self) -> None:
        rows = [
            ("a1", "Example Cloud", "US", 1),
            ("a2", "example cloud", "US", 1),
            ("a3", "Example Cloud", "NL", 3),  # 一覧記事 (被害組織が複数) は数えない
            ("a4", "Example Cloud", None, 1),  # 国なしは数えない
            ("a5", "Example Cloud", "", 1),
        ]
        history = build_org_history(rows)
        assert history == {"example cloud": Counter({"US": 2})}

    def test_same_article_id_is_counted_once(self) -> None:
        rows = [("a1", "Example Cloud", "US", 1), ("a1", "Example Cloud", "US", 1)]
        assert build_org_history(rows)["example cloud"]["US"] == 1


class TestFillMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VICTIM_COUNTRY_FILL", raising=False)
        assert fill_mode() is FillMode.SHADOW

    @pytest.mark.parametrize(
        ("value", "mode"),
        [("0", FillMode.OFF), ("off", FillMode.OFF), ("1", FillMode.ON), ("on", FillMode.ON)],
    )
    def test_env_values(self, monkeypatch: pytest.MonkeyPatch, value: str, mode: FillMode) -> None:
        monkeypatch.setenv("VICTIM_COUNTRY_FILL", value)
        assert fill_mode() is mode

    def test_unknown_value_falls_back_to_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VICTIM_COUNTRY_FILL", "maybe")
        assert fill_mode() is FillMode.OFF


def test_history_query_passes_like_pattern_as_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQL に % を直書きすると psycopg がプレースホルダと読み、履歴が毎回空になる (10-27 本番)。"""
    import src.storage.db_backend as db_backend
    from src.cti import victim_country_fill as vcf

    seen: dict[str, object] = {}

    class _Cur:
        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            seen.update(sql=sql, params=params)

        def fetchall(self) -> list[dict[str, object]]:
            return []

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def close(self) -> None:
            pass

    monkeypatch.setattr(db_backend, "connect", lambda: _Conn())

    assert vcf.load_org_history() == {}
    sql = str(seen["sql"])
    params = seen["params"]
    assert "%" not in sql
    assert isinstance(params, tuple)
    assert sql.count("?") == len(params)
    assert params[0] == f"{vcf.INFERRED_RAW_PREFIX}%"
