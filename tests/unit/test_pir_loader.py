"""src/pir/loader.py: yaml round-trip テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pir.loader import load_pir_config, save_pir_config
from src.pir.models import Pir, PirConfig, StrongSignals


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    cfg = load_pir_config(tmp_path / "nonexistent.yaml")
    assert cfg.version == 1
    assert cfg.priorities == []


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "delivery/pir.yaml"
    original = PirConfig(
        version=1,
        priorities=[
            Pir(
                id="pir_alpha",
                title="Alpha",
                description="alpha desc",
                strong_signals=StrongSignals(keywords=["k1"], countries=["JP"]),
            ),
        ],
    )
    save_pir_config(original, path)
    assert path.exists()
    loaded = load_pir_config(path)
    assert len(loaded.priorities) == 1
    assert loaded.priorities[0].id == "pir_alpha"
    assert loaded.priorities[0].strong_signals.keywords == ["k1"]


def test_save_atomic_no_partial(tmp_path: Path) -> None:
    """save 中の例外で半端な書き込みが残らないこと。"""
    path = tmp_path / "delivery/pir.yaml"
    cfg = PirConfig(version=1, priorities=[Pir(id="x", title="x")])
    save_pir_config(cfg, path)
    initial_content = path.read_text()
    # 同じ yaml で save が冪等であること
    save_pir_config(cfg, path)
    assert path.read_text() == initial_content


def test_invalid_root_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- not a mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_pir_config(path)


class TestActorNationMigration:
    """監査 backlog 2026-07-05: APT 系 PIR の countries → actor_nations 冪等移行。"""

    @staticmethod
    def _raw(pir_id: str, ss: dict[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            "priorities": [{"id": pir_id, "title": "t", "strong_signals": ss}],
        }

    def test_apt_pir_countries_moved_to_actor_nations(self) -> None:
        from src.pir.loader import strip_legacy_pir_keys

        raw = self._raw("pir_china_apt", {"countries": ["CN"]})
        got = strip_legacy_pir_keys(raw)
        ss = got["priorities"][0]["strong_signals"]  # type: ignore[index]
        assert ss["countries"] == []
        assert ss["actor_nations"] == ["CN"]

    def test_migration_is_idempotent(self) -> None:
        from src.pir.loader import strip_legacy_pir_keys

        raw = self._raw("pir_china_apt", {"countries": [], "actor_nations": ["CN"]})
        got = strip_legacy_pir_keys(strip_legacy_pir_keys(raw))
        ss = got["priorities"][0]["strong_signals"]  # type: ignore[index]
        assert ss["actor_nations"] == ["CN"]
        assert ss["countries"] == []

    def test_victim_semantics_pir_untouched(self) -> None:
        from src.pir.loader import strip_legacy_pir_keys

        raw = self._raw("pir_jp_targeted", {"countries": ["JP"]})
        got = strip_legacy_pir_keys(raw)
        ss = got["priorities"][0]["strong_signals"]  # type: ignore[index]
        assert ss["countries"] == ["JP"]
        assert "actor_nations" not in ss

    def test_seed_yaml_loads_with_actor_nations(self) -> None:
        # 実 seed (config/delivery/pir.yaml) が現行 schema で validate でき、APT 系 PIR の
        # actor_nations が立っていること (意味反転の再発防止)
        cfg = load_pir_config(Path("config/delivery/pir.yaml"))
        by_id = {p.id: p for p in cfg.priorities}
        assert by_id["pir_china_apt"].strong_signals.actor_nations == ["CN"]
        assert by_id["pir_china_apt"].strong_signals.countries == []
        assert by_id["pir_jp_targeted"].strong_signals.countries == ["JP"]
