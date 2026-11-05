"""運用者ローカルの上書き層 (config/local/) の読み込みのテスト。

- ローカルがあれば汎用 ∪ ローカルで分類する (移す前の公開版の一覧と同じ分類になる)
- 無ければ汎用の規則 (ccTLD) だけ
- どちらを使ったかはログに件数だけ出す (ドメイン名は出さない)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog.testing

from src import local_overlay
from src.cti import source_basis as sb
from src.tools import hostile_hosts as hh

# 移す前の公開版と同じ形 (cctlds + domains の mapping の list) の placeholder
_LOCAL_HOSTILE = """
domains:
  - {domain: ministry.kuebiko.example, origin: report, ref: X1}
  - {domain: state.kuebiko.example, origin: added, note: 例}
  - vendor.kuebiko.example
"""


@pytest.fixture
def local_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / "local"
    monkeypatch.setenv(local_overlay.ENV_LOCAL_CONFIG_DIR, str(d))
    hh.reset_cache()
    sb.invalidate_reliability_cache()
    yield d
    hh.reset_cache()
    sb.invalidate_reliability_cache()


class TestLocalPath:
    def test_default_dir_is_config_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(local_overlay.ENV_LOCAL_CONFIG_DIR, raising=False)
        assert local_overlay.local_config_dir().parts[-2:] == ("config", "local")

    @pytest.mark.parametrize("bad", ["/etc/passwd", "../x.yaml", ""])
    def test_rejects_names_outside_the_dir(self, bad: str) -> None:
        with pytest.raises(ValueError):
            local_overlay.local_path(bad)


class TestHostileOverlay:
    def test_local_present_classifies_listed_domains(self, local_dir: Path) -> None:
        # Arrange
        local_dir.mkdir()
        (local_dir / hh.LOCAL_FILE_NAME).write_text(_LOCAL_HOSTILE, encoding="utf-8")
        # Act
        v = hh.classify_host("https://www.ministry.kuebiko.example/a")
        bare = hh.classify_host("https://vendor.kuebiko.example/")
        # Assert
        assert v.hostile and v.reasons == (hh.REASON_DOMAIN,)
        assert bare.hostile
        # 汎用の ccTLD も残る (和集合)
        assert hh.classify_host("https://news.example.ru/").reasons == (hh.REASON_CCTLD,)
        assert not hh.classify_host("https://www.cisa.gov/").hostile

    def test_local_absent_uses_generic_rules_only(self, local_dir: Path) -> None:
        lists = hh.default_lists()
        assert lists.domains == hh.load_lists().domains  # 公開版 (SSoT) だけ
        assert lists.domains
        assert {"cn", "ru", "su", "xn--p1ai", "by", "kp", "ir"} <= lists.cctlds
        assert not hh.classify_host("https://www.ministry.kuebiko.example/").hostile
        assert hh.classify_host("https://news.example.cn/").hostile

    def test_local_can_add_cctlds(self, local_dir: Path) -> None:
        local_dir.mkdir()
        (local_dir / hh.LOCAL_FILE_NAME).write_text("cctlds: [zz]\n", encoding="utf-8")
        lists = hh.default_lists()
        assert "zz" in lists.cctlds and "ru" in lists.cctlds

    def test_broken_local_falls_back_to_generic(self, local_dir: Path) -> None:
        local_dir.mkdir()
        (local_dir / hh.LOCAL_FILE_NAME).write_text("domains: just-a-string\n", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            lists = hh.default_lists()
        assert lists.domains == hh.load_lists().domains
        assert "ru" in lists.cctlds
        assert any(e["event"] == "hostile_domains_local_invalid" for e in logs)

    def test_explicit_path_does_not_pick_up_local(self, local_dir: Path, tmp_path: Path) -> None:
        local_dir.mkdir()
        (local_dir / hh.LOCAL_FILE_NAME).write_text(_LOCAL_HOSTILE, encoding="utf-8")
        generic = tmp_path / "generic.yaml"
        generic.write_text("cctlds: [ru]\n", encoding="utf-8")
        assert hh.load_lists(generic).domains == frozenset()

    @pytest.mark.parametrize(("present", "source"), [(True, "generic+local"), (False, "generic")])
    def test_load_log_says_which_layer_without_domain_names(
        self, local_dir: Path, present: bool, source: str
    ) -> None:
        if present:
            local_dir.mkdir()
            (local_dir / hh.LOCAL_FILE_NAME).write_text(_LOCAL_HOSTILE, encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            hh.default_lists()
        loaded = [e for e in logs if e["event"] == "hostile_domains_loaded"]
        assert len(loaded) == 1
        assert loaded[0]["source"] == source
        shipped = len(hh.load_lists().domains)
        assert loaded[0]["domains"] == (shipped + 3 if present else shipped)
        assert "kuebiko.example" not in repr(logs)


class TestReliabilityOverlay:
    def test_local_hosts_are_merged_into_tiers(self, local_dir: Path) -> None:
        local_dir.mkdir()
        (local_dir / sb.LOCAL_RELIABILITY_NAME).write_text(
            "state_media:\n  hosts: [state.kuebiko.example]\n", encoding="utf-8"
        )
        assert sb.classify_source_tier("X", "https://www.state.kuebiko.example/") == "state_media"
        # 汎用の分類は残る
        assert sb.classify_source_tier("CISA", "https://www.cisa.gov/feed") == "official"

    def test_local_absent_keeps_the_shipped_adversary_hosts(self, local_dir: Path) -> None:
        p = sb.load_reliability_patterns(force_reload=True)
        assert p.adversary_official_hosts  # 公開版が SSoT
        assert sb.classify_source_tier("X", "https://www.state.kuebiko.example/") == "news"

    def test_load_log_says_which_layer(self, local_dir: Path) -> None:
        local_dir.mkdir()
        (local_dir / sb.LOCAL_RELIABILITY_NAME).write_text(
            "adversary_official:\n  hosts: [ministry.kuebiko.example]\n", encoding="utf-8"
        )
        with structlog.testing.capture_logs() as logs:
            sb.load_reliability_patterns(force_reload=True)
        loaded = [e for e in logs if e["event"] == "source_reliability_loaded"]
        assert loaded and loaded[0]["source"] == "generic+local"
        assert "kuebiko.example" not in repr(logs)

    def test_ui_override_flag_off_is_written_to_the_local_layer(
        self, local_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 通常は DB (test_source_basis)。rollback flag のときだけローカル層のファイルへ書く
        monkeypatch.setenv("SOURCE_RELIABILITY_OVERRIDES_DB", "0")
        url = "https://feeds.kuebiko.example/rss"
        assert sb.set_source_reliability_override(url, "research") == "research"
        written = local_dir / sb.LOCAL_OVERRIDES_NAME
        assert written.exists()
        assert url in written.read_text(encoding="utf-8")
        assert sb.classify_source_tier("X", url) == "research"
