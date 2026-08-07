"""Phase 1 K5: CISA KEV client (cache 参照 / TTL gated refresh / fail-safe)。"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.tools.kev_client as kc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # cache を tmp に隔離し、process 内 cache をリセット
    monkeypatch.setattr(kc, "_CACHE_PATH", tmp_path / "kev.json")
    monkeypatch.setattr(kc, "_mem_set", None)


@pytest.mark.unit
def test_get_set_empty_without_cache_and_no_fetch() -> None:
    # cache 無し → 空集合 (get_kev_cve_set は fetch しない)
    def _must_not() -> set[str]:
        raise AssertionError("get_kev_cve_set must not fetch")

    # _fetch を踏めば失敗するが、get_kev_cve_set は呼ばないはず
    kc._fetch_kev_cve_ids = _must_not
    assert kc.get_kev_cve_set() == frozenset()
    assert not kc.is_cve_on_kev("CVE-2026-1")


@pytest.mark.unit
def test_refresh_fetches_and_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc, "_fetch_kev_cve_ids", lambda: {"CVE-2026-1", "CVE-2026-2"})
    assert kc.refresh_kev_catalog() == 2
    s = kc.get_kev_cve_set()
    assert "CVE-2026-1" in s
    assert kc.is_cve_on_kev("cve-2026-1")  # case-insensitive
    assert not kc.is_cve_on_kev("CVE-2099-9")
    assert kc.any_cve_on_kev(["CVE-2099-9", "CVE-2026-2"]) is True
    assert kc.any_cve_on_kev(["CVE-2099-9"]) is False
    assert kc.any_cve_on_kev([]) is False


@pytest.mark.unit
def test_refresh_skips_fetch_when_cache_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc, "_fetch_kev_cve_ids", lambda: {"CVE-2026-5"})
    kc.refresh_kev_catalog()  # cache 作成
    monkeypatch.setattr(kc, "_mem_set", None)  # mem reset → disk 経路を確認

    def _must_not() -> set[str]:
        raise AssertionError("must not fetch when cache fresh")

    monkeypatch.setattr(kc, "_fetch_kev_cve_ids", _must_not)
    assert kc.refresh_kev_catalog() == 1  # fresh → fetch せず件数返す
    assert "CVE-2026-5" in kc.get_kev_cve_set()


@pytest.mark.unit
def test_refresh_degrades_on_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> set[str]:
        raise RuntimeError("net down")

    monkeypatch.setattr(kc, "_fetch_kev_cve_ids", _boom)
    # cache 無し + fetch 失敗 → 空集合、routing を壊さない
    assert kc.refresh_kev_catalog() == 0
    assert kc.get_kev_cve_set() == frozenset()
