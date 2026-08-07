"""src/pir/integration.py: triage/routing 注入 helper のテスト。"""

from __future__ import annotations

import pytest

from src.pir.integration import (
    build_synthesis_pir_context,
    build_triage_high_criteria,
    build_triage_medium_criteria,
)
from src.pir.models import Pir, SpotlightConfig


def _pir(
    pid: str,
    title: str,
    importance: str = "high",
    enabled: bool = True,
    description: str = "",
) -> Pir:
    return Pir(
        id=pid,
        title=title,
        description=description,
        enabled=enabled,
        target_importance=importance,  # type: ignore[arg-type]
        spotlight=SpotlightConfig(),
    )


def test_build_triage_high_criteria_empty() -> None:
    assert build_triage_high_criteria([]) == ""


def test_build_triage_high_criteria_filters_disabled() -> None:
    pirs = [
        _pir("a", "Alpha", importance="high", enabled=True),
        _pir("b", "Beta", importance="high", enabled=False),  # disabled
        _pir("c", "Gamma", importance="medium", enabled=True),  # not high
    ]
    out = build_triage_high_criteria(pirs)
    assert "Alpha" in out
    assert "Beta" not in out
    assert "Gamma" not in out


def test_build_triage_high_criteria_with_description() -> None:
    pirs = [_pir("a", "Alpha title", description="alpha desc line one\nline two")]
    out = build_triage_high_criteria(pirs)
    assert "Alpha title" in out
    assert "alpha desc line one" in out
    # 2nd line should not appear (only 1st line is included as brief)
    assert "line two" not in out


def test_build_triage_medium_criteria() -> None:
    pirs = [_pir("m", "Medium item", importance="medium")]
    out = build_triage_medium_criteria(pirs)
    assert "Medium item" in out


def test_build_synthesis_pir_context() -> None:
    pirs = [
        _pir("a", "Alpha", description="alpha desc"),
        _pir("b", "Beta", enabled=False),  # filtered out
    ]
    ctx = build_synthesis_pir_context(pirs)
    assert len(ctx) == 1
    assert ctx[0]["id"] == "a"
    assert ctx[0]["title"] == "Alpha"
    assert ctx[0]["description"] == "alpha desc"


class TestPirDbFirst:
    """PIR も DB 正・未保存時 config/pir.yaml seed fallback (運用 config DB 化)。"""

    def test_load_current_prefers_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.pir.integration import load_current_pir_config

        cfg_dict = {"version": 1, "priorities": [{"id": "pir_x", "title": "X"}]}
        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: cfg_dict)
        cfg = load_current_pir_config()
        assert cfg.find("pir_x") is not None

    def test_load_current_yaml_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.pir.integration import load_current_pir_config

        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: None)
        cfg = load_current_pir_config()
        assert isinstance(cfg.priorities, list)  # yaml seed に degrade

    def test_corrupt_db_degrades_to_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.pir.integration import load_current_pir_config

        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: {"priorities": "not-a-list"})
        cfg = load_current_pir_config()
        assert isinstance(cfg.priorities, list)  # 破損 → seed

    def test_persist_saves_to_db_and_invalidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.pir.integration as integ
        import src.storage.config_store as cstore
        from src.pir.models import Pir, PirConfig

        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cstore,
            "save_config",
            lambda key, value, **kw: captured.update({"key": key, "value": value}) or 1,
        )
        # rebuild_pir_entities は独自テストで担保。ここでは persist の save+invalidate 契約のみ
        # 検証するため no-op に stub (rebuild は get_pir_config 経由で cache を再充填するため、
        # stub しないと invalidate 直後の再充填で _cached_config が None にならない)。
        import src.pir.persist as ppersist

        monkeypatch.setattr(ppersist, "rebuild_pir_entities", lambda **kw: {})
        integ.get_pir_config(force_reload=False)  # prime cache (best-effort)
        cfg = PirConfig(priorities=[Pir(id="pir_x", title="X")])
        version = integ.persist_pir_config(cfg)
        assert version == 1
        assert captured["key"] == "pir"
        assert integ._cached_config is None  # invalidate された
