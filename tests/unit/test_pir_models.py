"""src/pir/models.py: Pydantic schema の round-trip / validation テスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from src.pir.models import (
    Pir,
    PirConfig,
    PirMetadata,
    SpotlightConfig,
    StrongSignals,
)


def test_minimal_pir_defaults() -> None:
    p = Pir(id="pir_test", title="Test PIR")
    assert p.enabled is True
    assert p.target_importance == "auto"
    assert p.strong_signals.keywords == []
    assert p.spotlight.enabled is False
    assert p.match is None
    assert p.llm_judge.enabled is False
    assert p.metadata.approved_by_user is True


def test_pir_full_fields_round_trip() -> None:
    now = datetime.now(UTC)
    p = Pir(
        id="pir_full",
        title="Full PIR",
        description="long description",
        enabled=True,
        strong_signals=StrongSignals(
            keywords=["kw1", "kw2"],
            actors=["APT41"],
            sectors=["defense"],
            countries=["JP", "US"],
            feed_titles=["JPCERT"],
        ),
        target_importance="high",
        spotlight=SpotlightConfig(enabled=True, title="🇯🇵 JP", window="daily"),
        metadata=PirMetadata(
            created_at=now,
            updated_at=now,
            migrated_from="article_triage.py:1",
            approved_by_user=False,
            rationale="test",
        ),
    )
    # round trip via model_dump → model_validate
    data = p.model_dump(mode="json")
    p2 = Pir.model_validate(data)
    assert p2.id == p.id
    assert p2.strong_signals.actors == ["APT41"]
    assert p2.spotlight.window == "daily"
    assert p2.metadata.approved_by_user is False


def test_legacy_target_channel_key_rejected_by_model() -> None:
    """R0 撤去後、Pir 直 validate では target_channel は未知キー (extra=forbid)。

    過去データの読み込みは loader.strip_legacy_pir_keys が除去してから validate する。
    """
    with pytest.raises(ValidationError):
        Pir.model_validate({"id": "pir_bad", "title": "bad", "target_channel": "alert"})


def test_strip_legacy_pir_keys_drops_target_channel() -> None:
    from src.pir.loader import strip_legacy_pir_keys

    raw = {
        "version": 1,
        "priorities": [{"id": "a", "title": "A", "target_channel": "alert"}],
    }
    cleaned = strip_legacy_pir_keys(raw)
    assert "target_channel" not in cast(list[dict[str, object]], cleaned["priorities"])[0]
    # 元 dict は不変 (immutable)
    assert "target_channel" in cast(list[dict[str, object]], raw["priorities"])[0]
    # 除去後は validate が通る
    cfg = PirConfig.model_validate(cleaned)
    assert cfg.priorities[0].id == "a"


def test_invalid_target_importance_rejected() -> None:
    with pytest.raises(ValidationError):
        Pir(id="pir_bad", title="bad", target_importance="critical")  # type: ignore[arg-type]


def test_pir_config_find_and_filter() -> None:
    p1 = Pir(id="a", title="A", enabled=True)
    p2 = Pir(id="b", title="B", enabled=False)
    cfg = PirConfig(priorities=[p1, p2])
    assert cfg.find("a") is p1
    assert cfg.find("z") is None
    enabled = cfg.enabled_priorities()
    assert len(enabled) == 1
    assert enabled[0].id == "a"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Pir(id="x", title="x", extra_field="not allowed")  # type: ignore[call-arg]
