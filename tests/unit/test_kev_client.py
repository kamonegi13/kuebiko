"""Phase 1 K5: CISA KEV client (cache 参照 / TTL gated refresh / fail-safe)。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import src.tools.kev_client as kc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # cache を tmp に隔離し、process 内 cache をリセット
    monkeypatch.setattr(kc, "_CACHE_PATH", tmp_path / "kev.json")
    monkeypatch.setattr(kc, "_mem_set", None)
    monkeypatch.setattr(kc, "_mem_dates", None)


@pytest.mark.unit
def test_get_set_empty_without_cache_and_no_fetch() -> None:
    # cache 無し → 空集合 (get_kev_cve_set は fetch しない)
    def _must_not() -> dict[str, str]:
        raise AssertionError("get_kev_cve_set must not fetch")

    # _fetch を踏めば失敗するが、get_kev_cve_set は呼ばないはず
    kc._fetch_kev_entries = _must_not
    assert kc.get_kev_cve_set() == frozenset()
    assert not kc.is_cve_on_kev("CVE-2026-1")


@pytest.mark.unit
def test_refresh_fetches_and_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc, "_fetch_kev_entries", lambda: {"CVE-2026-1": "", "CVE-2026-2": ""})
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
    monkeypatch.setattr(kc, "_fetch_kev_entries", lambda: {"CVE-2026-5": ""})
    kc.refresh_kev_catalog()  # cache 作成
    monkeypatch.setattr(kc, "_mem_set", None)  # mem reset → disk 経路を確認
    monkeypatch.setattr(kc, "_mem_dates", None)

    def _must_not() -> dict[str, str]:
        raise AssertionError("must not fetch when cache fresh")

    monkeypatch.setattr(kc, "_fetch_kev_entries", _must_not)
    assert kc.refresh_kev_catalog() == 1  # fresh → fetch せず件数返す
    assert "CVE-2026-5" in kc.get_kev_cve_set()


@pytest.mark.unit
def test_refresh_degrades_on_fetch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, str]:
        raise RuntimeError("net down")

    monkeypatch.setattr(kc, "_fetch_kev_entries", _boom)
    # cache 無し + fetch 失敗 → 空集合、routing を壊さない
    assert kc.refresh_kev_catalog() == 0
    assert kc.get_kev_cve_set() == frozenset()


def _load(monkeypatch: pytest.MonkeyPatch, entries: dict[str, str]) -> None:
    monkeypatch.setattr(kc, "_fetch_kev_entries", lambda: entries)
    kc.refresh_kev_catalog()


@pytest.mark.unit
def test_at_reference_uses_date_added_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: 2022-03-01 に掲載された CVE
    _load(monkeypatch, {"CVE-2022-1": "2022-03-01"})
    # Act / Assert: 掲載直後の記事は根拠、何年も後の再報は根拠にしない
    assert kc.any_cve_on_kev_at(["CVE-2022-1"], date(2022, 3, 10)) is True
    assert kc.any_cve_on_kev_at(["CVE-2022-1"], date(2026, 10, 1)) is False
    assert kc.any_cve_on_kev_at(["CVE-2022-1"], date(2022, 6, 1)) is False  # 92 日 → 窓外
    # 掲載より前 (当時は未掲載) の記事: 7 日の先行までは許し、それ以前は根拠にしない
    assert kc.any_cve_on_kev_at(["CVE-2022-1"], date(2022, 2, 26)) is True
    assert kc.any_cve_on_kev_at(["CVE-2022-1"], date(2021, 12, 1)) is False


@pytest.mark.unit
def test_at_reference_falls_back_when_date_or_reference_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _load(monkeypatch, {"CVE-2022-2": ""})
    assert kc.any_cve_on_kev_at(["CVE-2022-2"], date(2026, 10, 1)) is True  # 掲載日不明
    assert kc.any_cve_on_kev_at(["CVE-2022-2"], None) is True  # 従来どおり
    assert kc.any_cve_on_kev_at(["CVE-2099-1"], date(2026, 10, 1)) is False


@pytest.mark.unit
def test_legacy_list_cache_has_unknown_dates(tmp_path: Path) -> None:
    (tmp_path / "kev.json").write_text('["CVE-2020-1"]', encoding="utf-8")
    assert kc.get_kev_added_dates() == {"CVE-2020-1": None}
    assert kc.any_cve_on_kev_at(["CVE-2020-1"], date(2026, 1, 1)) is True
