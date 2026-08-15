"""source_store (ソース config の SSoT 層) の unit test。

flag (SOURCES_CONFIG_DB) による DB / yaml 切替、 load/save round-trip、 DB-first の
yaml fallback、 seed_all_if_absent の idempotency を検証する。 SQLite fallback を使うため
tmp に data/ を置き chdir する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.sources import source_store

_FEEDS_SEED = {
    "feeds": [
        {"name": "Seed A", "url": "https://a.example/feed", "enabled": True, "folder": "x"},
        {"name": "Seed B", "url": "https://b.example/feed", "enabled": False},
    ]
}


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cfg.mkdir()
    for _sub in ("sources", "cti", "delivery"):
        (cfg / _sub).mkdir(exist_ok=True)
    (cfg / "sources/feeds.yaml").write_text(
        yaml.safe_dump(_FEEDS_SEED, allow_unicode=True), encoding="utf-8"
    )
    (cfg / "sources/watchers.yaml").write_text("watchers: []\n", encoding="utf-8")
    (cfg / "sources/scrapers.yaml").write_text("scrapers: []\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_db_mode_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCES_CONFIG_DB", raising=False)
    assert source_store.is_db_mode() is True
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    assert source_store.is_db_mode() is False
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    assert source_store.is_db_mode() is True


def test_db_first_falls_back_to_seed_yaml(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    # DB 未投入 → seed yaml に fallback
    entries = source_store.load_entries("rss")
    assert [e["url"] for e in entries] == [
        "https://a.example/feed",
        "https://b.example/feed",
    ]


def test_db_roundtrip(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    new = [{"name": "Only", "url": "https://only.example/feed", "enabled": True}]
    version = source_store.save_entries("rss", new, note="t")
    assert version == 1
    assert source_store.load_entries("rss") == new
    # 2 回目の save で version が増える (履歴)
    v2 = source_store.save_entries("rss", new, note="t2")
    assert v2 == 2


def test_yaml_mode_writes_and_reads_file(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "0")
    new = [{"name": "Y", "url": "https://y.example/feed", "enabled": True}]
    version = source_store.save_entries("rss", new, note="t")
    assert version is None  # yaml mode は version を返さない
    # yaml file が書き換わっている
    raw = yaml.safe_load((_env / "config" / "sources/feeds.yaml").read_text())
    assert raw["feeds"] == new
    assert source_store.load_entries("rss") == new


def test_explicit_path_reads_yaml_regardless_of_flag(
    _env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")  # DB mode でも path 明示なら yaml 直読み
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        yaml.safe_dump({"feeds": [{"name": "C", "url": "https://c/f", "enabled": True}]}),
        encoding="utf-8",
    )
    entries = source_store.load_entries("rss", path=custom)
    assert [e["url"] for e in entries] == ["https://c/f"]


def test_seed_all_if_absent_idempotent(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCES_CONFIG_DB", "1")
    first = source_store.seed_all_if_absent()
    assert first == {"feeds": True, "watchers": True, "scrapers": True}
    # seed 後は DB が SSoT
    assert [e["url"] for e in source_store.load_entries("rss")] == [
        "https://a.example/feed",
        "https://b.example/feed",
    ]
    # 2 回目は既投入なので全て False (yaml の再取り込みはしない)
    second = source_store.seed_all_if_absent()
    assert second == {"feeds": False, "watchers": False, "scrapers": False}
