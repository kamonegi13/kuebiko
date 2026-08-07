"""通知再設計: product_routing (curated product → channel の config 駆動) のテスト。

channel_registry / routing_rules と同じ DB(config_store)-正・built-in fail-safe パターン。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.storage.config_store import save_config
from src.tools.product_routing import (
    BUILTIN_PRODUCT_ROUTING,
    PRODUCT_ROUTING_CONFIG_KEY,
    invalidate_product_routing_cache,
    load_product_routing,
    product_channel,
    seed_product_routing_if_absent,
    validate_product_routing,
)


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    invalidate_product_routing_cache()
    yield tmp_path / "test_product_routing.db"
    invalidate_product_routing_cache()


class TestLoad:
    def test_builtin_defaults(self) -> None:
        assert BUILTIN_PRODUCT_ROUTING["morning_brief"] == "brief"
        assert BUILTIN_PRODUCT_ROUTING["evening_brief"] == "brief"
        assert BUILTIN_PRODUCT_ROUTING["weekly_recap"] == "brief"
        assert BUILTIN_PRODUCT_ROUTING["status_synthesis"] == "brief"
        assert BUILTIN_PRODUCT_ROUTING["pir_spotlight"] == "watch"

    def test_returns_builtin_when_db_empty(self, db_path: Path) -> None:
        assert load_product_routing(db_path=db_path) == BUILTIN_PRODUCT_ROUTING

    def test_db_overrides_and_merges_builtin(self, db_path: Path) -> None:
        # DB は一部だけ指定 → 既定にマージ (新 product 追加時に DB 未投入でも既定で動く)
        save_config(PRODUCT_ROUTING_CONFIG_KEY, {"morning_brief": "watch"}, db_path=db_path)
        invalidate_product_routing_cache()
        r = load_product_routing(db_path=db_path)
        assert r["morning_brief"] == "watch"  # DB override
        assert r["pir_spotlight"] == "watch"  # builtin 由来 (DB 未指定)

    def test_product_channel_fallback_for_unknown(self, db_path: Path) -> None:
        # 未知 product は web-only tier (watch) に安全側 fallback
        assert product_channel("nonexistent", db_path=db_path) == "watch"

    def test_product_channel_known(self, db_path: Path) -> None:
        assert product_channel("morning_brief", db_path=db_path) == "brief"
        assert product_channel("pir_spotlight", db_path=db_path) == "watch"

    def test_seed_if_absent(self, db_path: Path) -> None:
        assert seed_product_routing_if_absent(db_path=db_path) is True
        invalidate_product_routing_cache()
        assert load_product_routing(db_path=db_path)["weekly_recap"] == "brief"
        # 既に投入済みなら no-op
        assert seed_product_routing_if_absent(db_path=db_path) is False


class TestValidate:
    def test_valid(self) -> None:
        errs = validate_product_routing(
            {"morning_brief": "brief"}, known_channels={"brief", "watch"}
        )
        assert errs == []

    def test_unknown_channel_rejected(self) -> None:
        errs = validate_product_routing({"morning_brief": "ghost"}, known_channels={"brief"})
        assert any("ghost" in e for e in errs)

    def test_empty_rejected(self) -> None:
        assert validate_product_routing({}, known_channels={"brief"})

    def test_missing_channel_value_rejected(self) -> None:
        errs = validate_product_routing({"morning_brief": ""}, known_channels={"brief"})
        assert errs
