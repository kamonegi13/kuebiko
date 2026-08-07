"""Unit tests for src.watchers.yaml_registry."""

from __future__ import annotations

from pathlib import Path

from src.watchers.yaml_registry import (
    WatcherDef,
    build_sitemap_watcher,
    load_watchers_config,
    reset_cache,
)


class TestLoadWatchersConfig:
    def test_loads_yaml_file(self, tmp_path: Path) -> None:
        yaml_text = """
watchers:
  - name: test-a
    type: sitemap
    enabled: true
    sitemap_urls:
      - https://example.com/sitemap.xml
    url_include_pattern: '^https://example\\.com/news/[\\w\\-]+'
    feed_title: "Test A"
    max_posts_per_run: 5
  - name: test-b
    type: sitemap
    enabled: false
    sitemap_urls:
      - https://b.example/sitemap.xml
    url_include_pattern: '^https://b\\.example/'
    feed_title: "Test B"
"""
        p = tmp_path / "watchers.yaml"
        p.write_text(yaml_text)
        cfg = load_watchers_config(p)
        assert len(cfg.watchers) == 2
        assert cfg.watchers[0].name == "test-a"
        assert cfg.watchers[0].enabled is True
        assert cfg.watchers[1].enabled is False

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        cfg = load_watchers_config(tmp_path / "missing.yaml")
        assert cfg.watchers == []


class TestBuildSitemapWatcher:
    def test_builds_simple_watcher(self, tmp_path: Path) -> None:
        d = WatcherDef(
            name="test-watcher",
            type="sitemap",
            enabled=True,
            sitemap_urls=["https://example.com/sitemap.xml"],
            url_include_pattern=r"^https://example\.com/news/",
            feed_title="Test Watcher",
            max_posts_per_run=5,
        )
        watcher = build_sitemap_watcher(d, state_dir=tmp_path)
        assert watcher.name == "test-watcher"
        assert watcher.sitemap_urls == ("https://example.com/sitemap.xml",)
        assert watcher.feed_title == "Test Watcher"
        assert watcher.max_posts_per_run == 5
        # state file は kebab → snake で変換
        assert watcher.state_file == tmp_path / "test_watcher_seen.json"

    def test_includes_exclude_pattern(self, tmp_path: Path) -> None:
        d = WatcherDef(
            name="x",
            sitemap_urls=["https://x.example/sitemap.xml"],
            url_include_pattern=r"^https://x\.example/news/",
            url_exclude_pattern=r"/draft/",
            feed_title="X",
        )
        watcher = build_sitemap_watcher(d, state_dir=tmp_path)
        assert watcher.url_exclude_pattern is not None
        assert watcher.url_exclude_pattern.search("https://x.example/news/draft/post-1")

    def test_no_exclude_when_omitted(self, tmp_path: Path) -> None:
        d = WatcherDef(
            name="x",
            sitemap_urls=["https://x.example/sitemap.xml"],
            url_include_pattern=r"^https://x\.example/",
            feed_title="X",
        )
        watcher = build_sitemap_watcher(d, state_dir=tmp_path)
        assert watcher.url_exclude_pattern is None


class TestProductionYamlIntegrity:
    """config/watchers.yaml が実 watcher 数と一致するか。"""

    def test_production_yaml_loads(self) -> None:
        reset_cache()
        cfg = load_watchers_config()
        assert len(cfg.watchers) > 0, "watchers.yaml に少なくとも 1 件期待"

    def test_production_yaml_unique_names(self) -> None:
        cfg = load_watchers_config()
        names = [w.name for w in cfg.watchers]
        assert len(names) == len(set(names)), "watcher name 重複あり"

    def test_production_watchers_buildable(self, tmp_path: Path) -> None:
        """yaml の全 entry が build_sitemap_watcher で error なくビルド可能。"""
        cfg = load_watchers_config()
        for w in cfg.watchers:
            try:
                build_sitemap_watcher(w, state_dir=tmp_path)
            except Exception as e:  # noqa: BLE001
                msg = f"yaml entry {w.name} がビルド失敗: {e}"
                raise AssertionError(msg) from e
