"""articles_feed の facet 解決 (_build_facets) と repo.entity_article_ids のテスト。

step3 (PIR / affected_vendor facet 化) の中核 glue: vendor→CVE 逆引き→entity_filters、
PIR の entity 写像、importance「medium=以上」正規化、no-match 番兵。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import src.tools.nvd_client as nc
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.api.articles_feed import _build_facets, _parse_since_iso


class _BaseKwargs(TypedDict):
    """``_build_facets`` の必須キーワード引数 (splat 元) に一致させる型。"""

    importance: str | None
    category: str | None
    feed: str | None
    channel: str | None
    cve: str | None
    malware: str | None
    intent: str | None
    since_hours: int
    status: str | None


_BASE: _BaseKwargs = {
    "importance": None,
    "category": None,
    "feed": None,
    "channel": None,
    "cve": None,
    "malware": None,
    "intent": None,
    "since_hours": 0,
    "status": "posted",
}


def test_build_facets_pir_and_vendor(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        nc,
        "_mem_cache",
        {
            "CVE-2026-1": {"vendors": ["fortinet"]},
            "CVE-2026-2": {"vendors": ["fortinet", "cisco"]},
        },
    )
    f = _build_facets(**_BASE, pir="pir_china_apt", affected_vendor="Fortinet")
    assert ("pir", ("pir_china_apt",)) in f.entity_filters
    # vendor → 該当 CVE 群 (sorted) が ('cve', [...]) として合成される
    assert ("cve", ("CVE-2026-1", "CVE-2026-2")) in f.entity_filters
    assert not f.is_empty()


def test_build_facets_vendor_no_match_uses_sentinel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(nc, "_mem_cache", {})
    f = _build_facets(**_BASE, affected_vendor="unknownvendor")
    # 該当 CVE 無し → 空 IN で全件化しないよう番兵を入れる
    assert ("cve", ("__no_cve_for_vendor__",)) in f.entity_filters


def test_build_facets_actor() -> None:
    f = _build_facets(**_BASE, actor="volt_typhoon")
    assert ("actor", ("volt_typhoon",)) in f.entity_filters


def test_build_facets_affected_matches_product(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        nc, "_mem_cache", {"CVE-2026-1": {"vendors": ["fortinet"], "products": ["fortios"]}}
    )
    # product 名 (fortios) でも affected facet が CVE に解決される
    f = _build_facets(**_BASE, affected_vendor="fortios")
    assert ("cve", ("CVE-2026-1",)) in f.entity_filters


def test_build_facets_cve_and_malware_both_apply() -> None:
    # 旧挙動 (cve 優先で malware 無視) は廃止。両方 entity filter になる (AND)。
    kw: _BaseKwargs = {**_BASE, "cve": "cve-2024-3400", "malware": "LockBit"}
    f = _build_facets(**kw)
    assert ("cve", ("CVE-2024-3400",)) in f.entity_filters
    assert ("malware_family", ("LockBit",)) in f.entity_filters


def test_build_facets_importance_medium_is_inclusive() -> None:
    kw: _BaseKwargs = {**_BASE, "importance": "medium"}
    f = _build_facets(**kw)
    assert f.importance_in == ("medium", "high")


def test_parse_since_iso_adds_utc_when_naive() -> None:
    got = _parse_since_iso("2026-06-28T10:00:00")
    assert got is not None
    assert got.tzinfo is not None  # naive 入力に UTC を補う
    assert got.year == 2026 and got.hour == 10


def test_parse_since_iso_preserves_tz() -> None:
    got = _parse_since_iso("2026-06-28T10:00:00+09:00")
    assert got is not None
    assert got.utcoffset() is not None


def test_parse_since_iso_rejects_garbage() -> None:
    assert _parse_since_iso("not-a-date") is None
    assert _parse_since_iso("") is None
    assert _parse_since_iso(None) is None


def test_build_facets_since_iso_overrides_since_hours() -> None:
    # W2: 絶対 since (カーソル) があれば相対 since_hours より優先。
    kw: _BaseKwargs = {**_BASE, "since_hours": 24}
    f = _build_facets(**kw, since_iso="2026-06-01T00:00:00+00:00")
    assert f.since is not None
    assert f.since.year == 2026 and f.since.month == 6 and f.since.day == 1


def test_build_facets_falls_back_to_since_hours_when_no_iso() -> None:
    # since_iso 不正/未指定 → 従来の since_hours 相対窓を使う。
    kw: _BaseKwargs = {**_BASE, "since_hours": 24}
    f = _build_facets(**kw, since_iso="garbage")
    assert f.since is not None  # since_hours=24 由来の相対 since


def test_build_facets_no_since_when_neither_set() -> None:
    kw: _BaseKwargs = {**_BASE, "since_hours": 0}
    f = _build_facets(**kw, since_iso=None)
    assert f.since is None


def test_entity_article_ids_or_within_values(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "ent.db")
    when = datetime.now(UTC)
    rid = repo.start_run(RunRecord(started_at=when, pipeline="x", dry_run=False))
    for aid in ("a1", "a2", "a3"):
        repo.add_article(
            ArticleRecord(run_id=rid, article_id=aid, title=aid, url=f"u/{aid}", status="posted")
        )
    repo.add_article_entities("a1", [("cve", "CVE-2026-1")], when=when)
    repo.add_article_entities("a2", [("cve", "CVE-2026-2")], when=when)
    repo.add_article_entities("a3", [("cve", "CVE-2026-9")], when=when)
    # values 内は OR、LOWER 一致
    got = repo.entity_article_ids("cve", ["cve-2026-1", "CVE-2026-2"])
    assert got == {"a1", "a2"}
    assert repo.entity_article_ids("cve", []) == set()
