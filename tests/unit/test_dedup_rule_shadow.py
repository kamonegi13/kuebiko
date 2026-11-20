"""配信直前の重複判定の規則変更 3 案の影の記録 (I-2 / I-3 / I-4)。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.pipeline import dedup_rule_shadow as m
from src.pipeline.dedup_rule_shadow import (
    LAYER_CVE,
    LAYER_VICTIM_ORG,
    RULE_CVE_FOCAL,
    RULE_HIGH_OVERRIDE,
    RULE_VICTIM_FOCAL,
    DedupMatchFacts,
    evaluate_shadow_rules,
    layer_of_reason,
    record_dedup_rule_shadow,
)


def _lifted(**kw: Any) -> tuple[str, ...]:
    return evaluate_shadow_rules(DedupMatchFacts(**kw)).lifted_by


# --- I-2 ---
def test_cve_focal_lifts_when_new_article_is_bulk_advisory() -> None:
    assert _lifted(layer=LAYER_CVE, new_cve_count=5) == (RULE_CVE_FOCAL,)


def test_cve_focal_lifts_when_root_is_bulk_advisory() -> None:
    assert _lifted(layer=LAYER_CVE, new_cve_count=1, root_cve_count=9) == (RULE_CVE_FOCAL,)


def test_cve_focal_keeps_suppression_when_both_sides_are_focal() -> None:
    assert _lifted(layer=LAYER_CVE, new_cve_count=1, root_cve_count=2) == ()


def test_cve_focal_lifts_for_fix_vs_exploit() -> None:
    assert _lifted(layer=LAYER_CVE, new_kind="exploitation", root_kind="advisory") == (
        RULE_CVE_FOCAL,
    )


def test_cve_focal_keeps_suppression_for_same_kind_and_unknown_kind() -> None:
    assert _lifted(layer=LAYER_CVE, new_kind="advisory", root_kind="advisory") == ()
    assert _lifted(layer=LAYER_CVE, new_kind=None, root_kind="advisory") == ()


# --- I-3 ---
def test_victim_focal_lifts_with_many_victims() -> None:
    assert _lifted(layer=LAYER_VICTIM_ORG, new_victims=("a", "b", "c")) == (RULE_VICTIM_FOCAL,)
    assert _lifted(layer=LAYER_VICTIM_ORG, new_victims=("a",), root_victims=("a", "b", "c")) == (
        RULE_VICTIM_FOCAL,
    )


def test_victim_focal_lifts_for_non_subject_kind_and_masked_victim() -> None:
    assert _lifted(layer=LAYER_VICTIM_ORG, new_victims=("a",), root_kind="stats") == (
        RULE_VICTIM_FOCAL,
    )
    assert _lifted(layer=LAYER_VICTIM_ORG, new_victims=("ab***",)) == (RULE_VICTIM_FOCAL,)


def test_victim_focal_keeps_suppression_for_single_plain_victim() -> None:
    assert _lifted(layer=LAYER_VICTIM_ORG, new_victims=("acme",), new_kind="breach") == ()


def test_victim_rule_ignored_on_cve_layer() -> None:
    assert _lifted(layer=LAYER_CVE, new_victims=("a", "b", "c")) == ()


# --- I-4 ---
def test_high_override_lifts_when_new_is_high_and_root_is_not() -> None:
    assert _lifted(layer="other", new_importance="high", root_importance="medium") == (
        RULE_HIGH_OVERRIDE,
    )


def test_high_override_keeps_suppression_when_root_is_high_or_new_is_not() -> None:
    assert _lifted(layer="other", new_importance="high", root_importance="high") == ()
    assert _lifted(layer="other", new_importance="medium", root_importance="low") == ()


def test_layer_of_reason_maps_prefixes() -> None:
    assert layer_of_reason("cve dedup: prior post 48h (x)") == LAYER_CVE
    assert layer_of_reason("victim_org dedup: prior post 24h") == LAYER_VICTIM_ORG
    assert layer_of_reason("content_dedup: cross-source match") == "other"


# --- record (旗・例外) ---
class _Repo:
    def get_article_kinds(self, ids: list[str]) -> dict[str, str]:
        return {"new": "exploitation", "root": "advisory"}

    def get_article_entities(self, article_id: str) -> list[tuple[str, str]]:
        return [("cve", "CVE-2026-0001")]

    def get_articles_by_ids(self, ids: list[str]) -> dict[str, Any]:
        return {"root": SimpleNamespace(importance="medium")}


def _msg() -> Any:
    return SimpleNamespace(title="CVE-2026-0001 exploited", iocs=[], metadata={}, importance="high")


def _run(repo: Any) -> None:
    record_dedup_rule_shadow(
        repo,
        msg=_msg(),
        art_id="new",
        prior_article_id="root",
        failure_reason="cve dedup: prior post 48h (vuln)",
    )


def test_flag_off_does_not_touch_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(m.DEDUP_RULE_SHADOW_ENV, raising=False)

    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("repo must not be touched")

    _run(Boom())


def test_flag_on_logs_lifting_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(m.DEDUP_RULE_SHADOW_ENV, "1")
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(m._log, "info", lambda ev, **kw: calls.append((ev, kw)))

    _run(_Repo())

    assert calls[0][0] == "dedup_rule_shadow"
    assert calls[0][1]["lifted_by"] == [RULE_CVE_FOCAL, RULE_HIGH_OVERRIDE]
    assert calls[0][1]["prior_article_id"] == "root"


def test_exception_is_swallowed_and_logged_by_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(m.DEDUP_RULE_SHADOW_ENV, "1")
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(m._log, "warning", lambda ev, **kw: warnings.append((ev, kw)))

    class Broken(_Repo):
        def get_article_kinds(self, ids: list[str]) -> dict[str, str]:
            raise RuntimeError("secret detail")

    _run(Broken())  # 例外が出ない = 判定は変わらない

    assert warnings == [("dedup_rule_shadow_failed", {"error_type": "RuntimeError"})]
