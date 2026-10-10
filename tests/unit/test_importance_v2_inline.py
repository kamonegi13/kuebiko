"""M3b 段1: routing 時点の inline ``ImportanceV2`` 計算 (``src/cti/importance_v2_inline.py``)。

本番の投稿先は変えない (shadow 専用)。確認する不変条件:
- inline の深刻度の軸 LLM は importance 帯 + サイバー category のときだけ呼ぶ
- 軸が無くても (severity_axes=None) KEV 該当だけで S3 になる (importance_v2.derive_severity
  の on_kev チェックは axes の有無より前に評価される)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import src.tools.kev_client as kev_client
from src.cti.importance_v2_inline import (
    axes_eligible,
    build_importance_v2_inline,
    classify_axes_inline,
)
from src.tools.discord_publisher import BriefingMessage


def _msg(
    *,
    summary: str = "summary",
    importance: str = "high",
    category: str = "vulnerability",
    metadata: dict[str, object] | None = None,
) -> BriefingMessage:
    return BriefingMessage(
        title="t",
        bluf="",
        summary=summary,
        importance=importance,  # type: ignore[arg-type]
        category=category,
        metadata=metadata or {},
    )


class TestAxesEligible:
    def test_high_cyber_category_is_eligible(self) -> None:
        assert axes_eligible("high", "breach") is True

    def test_medium_cyber_category_is_eligible(self) -> None:
        assert axes_eligible("medium", "vulnerability") is True

    def test_low_importance_is_not_eligible(self) -> None:
        assert axes_eligible("low", "breach") is False

    def test_non_cyber_category_is_not_eligible(self) -> None:
        # non_cyber (importance_v2.NON_CYBER_CATEGORIES) は derive_severity が常に
        # severity=None を返すので、inline で LLM を呼んでも無駄になる。
        assert axes_eligible("high", "geopolitical") is False
        assert axes_eligible("high", "policy") is False
        assert axes_eligible("high", "other") is False


class TestClassifyAxesInline:
    async def test_flag_off_skips_llm_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROUTING_V2_INLINE_AXES", "0")

        def _boom(*_a: object, **_kw: object) -> None:
            raise AssertionError("classify_axes が呼ばれてはいけない")

        monkeypatch.setattr("src.cti.severity_axes.classify_axes", _boom)

        result = await classify_axes_inline(
            "title",
            "summary",
            importance="high",
            category="breach",
        )

        assert result == (None, "")

    async def test_ineligible_skips_llm_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_kw: object) -> None:
            raise AssertionError("classify_axes が呼ばれてはいけない")

        monkeypatch.setattr("src.cti.severity_axes.classify_axes", _boom)

        result = await classify_axes_inline(
            "title",
            "summary",
            importance="low",
            category="breach",
        )

        assert result == (None, "")

    async def test_eligible_calls_llm_and_returns_model_dump(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.cti.severity_axes import SeverityAxes

        axes = SeverityAxes(
            scope="single_org",
            impact="data_exposure",
            confirmation="confirmed",
            magnitude="unknown",
            exploitation="not_applicable",
            actor="criminal",
            target="general_org",
        )

        async def _fake_classify_axes(
            llm: object, title: str, summary: str, body: str = ""
        ) -> SeverityAxes:
            return axes

        monkeypatch.setattr("src.cti.severity_axes.classify_axes", _fake_classify_axes)

        class _FakeLlm:
            model = "fake-model"

        monkeypatch.setattr("src.cti.importance_v2_inline._axes_llm", lambda: _FakeLlm())

        dumped, model_label = await classify_axes_inline(
            "title",
            "summary",
            importance="high",
            category="breach",
        )

        assert dumped == axes.model_dump()
        assert model_label == "fake-model"

    def test_axes_client_is_built_for_severity_axes_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.cti import importance_v2_inline
        from src.tools.model_tiers import Step

        seen: list[Step] = []

        def _fake_build(step: Step, _config: object) -> object:
            seen.append(step)
            return object()

        monkeypatch.setattr("src.tools.model_tiers.build_llm_for", _fake_build)
        monkeypatch.setattr("src.config_loader.load_app_config", lambda: object())

        importance_v2_inline._axes_llm()

        assert seen == [Step.SEVERITY_AXES]


class TestBuildImportanceV2Inline:
    def test_kev_cve_without_severity_axes_still_yields_s3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        year = datetime.now(UTC).year
        cve = f"CVE-{year}-12345"
        monkeypatch.setattr(kev_client, "get_kev_cve_set", lambda: frozenset({cve}))

        msg = _msg(summary=f"{cve} が実環境で悪用されていると CISA が公表した。")

        v2 = build_importance_v2_inline(msg=msg, body_text="", feed_title="", severity_axes=None)

        assert v2.severity == "S3"
        assert v2.severity_basis == "kev"

    def test_no_kev_and_no_axes_yields_no_severity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(kev_client, "get_kev_cve_set", lambda: frozenset())

        msg = _msg(summary="何らかの事案が発生した。", category="breach")

        v2 = build_importance_v2_inline(msg=msg, body_text="", feed_title="", severity_axes=None)

        assert v2.severity is None
        assert v2.severity_basis == "no_axes"
