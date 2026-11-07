"""被害組織名の別名辞書 (ORG-D-IMPL): pairs との同値・dict の読み口・script の dry-run/apply。"""

from __future__ import annotations

import importlib.util
import itertools
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from src.config_loader import AppConfig
from src.cti.org_normalizer import org_match_key
from src.eventnews import org_identity as oi
from src.eventnews.identity.focus import (
    CLASS_CONFLICT,
    FocusArticle,
    compute_focus,
    match_focus,
)
from src.eventnews.models import ItemState, MemberArticle
from src.eventnews.org_alias_dict import build_dict_mapping, diff_mapping, masked_keys
from src.storage.run_history import RunHistoryRepository
from src.ui.services import eventnews_org_identity as svc
from src.ui.services.eventnews_org_alias import (
    ALIAS_SOURCE_ENV,
    load_org_alias,
    org_alias_source,
)

_PAIRS = {("aa", "bb"): 13.0, ("bb", "cc"): 14.0, ("xx", "yy"): -3.0}
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_org_alias_dict.py"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "t.db")
    r.put_org_identity_logits(_PAIRS)
    return r


def _fa(aid: str, *names: tuple[str, str]) -> FocusArticle:
    return FocusArticle(article_id=aid, names=frozenset(names))


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_org_alias_dict", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_source_is_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALIAS_SOURCE_ENV, raising=False)
    assert org_alias_source() == "pairs"
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "garbage")
    assert org_alias_source() == "pairs"


def test_pairs_source_equals_legacy_mapping(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.delenv(ALIAS_SOURCE_ENV, raising=False)
    legacy = dict(oi.build_alias_map(repo.list_org_identity_same_pairs()).mapping)
    assert load_org_alias(repo) == legacy == {"aa": "aa", "bb": "aa", "cc": "aa"}


def test_dict_built_from_pairs_is_equivalent_to_pairs(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.delenv(ALIAS_SOURCE_ENV, raising=False)
    from_pairs = load_org_alias(repo)
    _script().refresh(repo, apply=True)
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    assert load_org_alias(repo) == from_pairs


def test_dry_run_writes_nothing(repo: RunHistoryRepository) -> None:
    summary = _script().refresh(repo, apply=False)
    assert summary["added"] == 3
    assert repo.load_org_alias_dict() == {}


def test_apply_replaces_only_pairs_source(repo: RunHistoryRepository) -> None:
    repo.replace_org_alias_dict({"zz": "zz"}, source="manual")
    repo.replace_org_alias_dict({"old": "old"}, source="pairs")
    _script().refresh(repo, apply=True)
    assert repo.load_org_alias_dict() == {"aa": "aa", "bb": "aa", "cc": "aa", "zz": "zz"}


def test_masked_names_are_excluded_even_as_bridge() -> None:
    masked = masked_keys(["Fake Co ****", "Plain Co"])
    assert masked == {org_match_key("Fake Co ****")}
    pairs = [("aa", "mm"), ("mm", "cc")]
    assert build_dict_mapping(pairs, frozenset({"mm"})) == {}
    assert build_dict_mapping(pairs) != {}


def test_diff_mapping() -> None:
    assert diff_mapping({"a": "a", "b": "a"}, {"a": "a", "b": "b", "c": "a"}) == (
        ["c"],
        ["b"],
        [],
    )


def test_deterministic_key_folds_case_width_legal_suffix() -> None:
    assert org_match_key("株式会社Ｔｉｍｅｓ　Car") == org_match_key("times car, Inc.")


def _article(aid: str, key: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=key,
        url=f"https://x.example/{aid}",
        feed_title="f",
        feed_url="u",
        host="x.example",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=datetime(2026, 10, 1, tzinfo=UTC),
        summary="",
        body=key,
        entities=frozenset({("victim_org", key)}),
    )


@pytest.mark.asyncio
async def test_grouping_dict_source_remaps_without_judging(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.setenv("EVENTNEWS_ORG_NORM", "1")
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "1")
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    _script().refresh(repo, apply=True)

    async def boom(*a: object, **k: object) -> float | None:
        raise AssertionError("dict source must not call the judge")

    monkeypatch.setattr(oi, "judge_name_pair", boom)
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    state = ItemState(
        item_id="i",
        first_reported_at=ts,
        last_reported_at=ts,
        status="new",
        importance="high",
        current_version=0,
        member_ids=("m",),
    )
    new_c, new_ex = await svc.resolve_org_identity(
        repo, AppConfig(), [_article("c", "cc")], [(state, [_article("m", "bb")])], {}
    )
    assert new_c[0].entities == new_ex[0][1][0].entities == frozenset({("victim_org", "aa")})


def _count_judge(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake(a: str, ca: str, b: str, cb: str, **_: object) -> float | None:
        calls.append(a)
        return 14.0

    monkeypatch.setattr(oi, "judge_name_pair", fake)
    return calls


def _grouping_inputs() -> tuple[MemberArticle, ItemState, MemberArticle]:
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    state = ItemState(
        item_id="i",
        first_reported_at=ts,
        last_reported_at=ts,
        status="new",
        importance="high",
        current_version=0,
        member_ids=("m",),
    )
    return _article("c", "cc"), state, _article("m", "bb")


@pytest.mark.asyncio
async def test_shadow_with_dict_source_still_judges_and_records(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.setenv("EVENTNEWS_ORG_NORM", "1")
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "shadow")
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    _script().refresh(repo, apply=True)
    calls = _count_judge(monkeypatch)
    monkeypatch.setattr(
        svc, "_victim_names", lambda r, ids: {"c": ["Fake Corp"], "m": ["フェイク社"]}
    )
    cand, state, member = _grouping_inputs()
    vectors: dict[str, list[float]] = {"c": [1.0, 0.0], "m": [1.0, 0.0]}

    new_c, new_ex = await svc.resolve_org_identity(
        repo, AppConfig(), [cand], [(state, [member])], vectors
    )

    assert calls
    assert new_c[0].entities == cand.entities
    assert new_ex[0][1][0].entities == member.entities
    assert repo.get_org_identity_logits([oi.pair_key("Fake Corp", "フェイク社")])


@pytest.mark.asyncio
async def test_dict_source_with_empty_dictionary_warns_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENTNEWS_ORG_NORM", "1")
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "1")
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    empty = RunHistoryRepository(db_path=tmp_path / "e.db")
    warnings: list[str] = []
    monkeypatch.setattr(svc._log, "warning", lambda event, **kw: warnings.append(event))
    cand, state, member = _grouping_inputs()

    new_c, _ = await svc.resolve_org_identity(empty, AppConfig(), [cand], [(state, [member])], {})

    assert new_c[0].entities == cand.entities
    assert "org_alias_dict_empty" in warnings


def test_manual_row_survives_pairs_refresh(repo: RunHistoryRepository) -> None:
    repo.replace_org_alias_dict({"aa": "manual-canon"}, source="manual")

    _script().refresh(repo, apply=True)

    mapping = repo.load_org_alias_dict()
    assert mapping["aa"] == "manual-canon"
    assert mapping["bb"] == "aa"
    # 2 回目の refresh でも奪わない
    _script().refresh(repo, apply=True)
    assert repo.load_org_alias_dict()["aa"] == "manual-canon"


def test_refresh_excludes_masked_victim_from_real_entity_table(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "m.db")
    masked_name = "Sample Corp ****"
    key = org_match_key(masked_name)
    repo.put_org_identity_logits({(key, "zz"): 13.0, ("aa", "bb"): 13.0})
    with repo._connect() as conn:
        conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("a1", "victim_org", masked_name, "2026-10-01T00:00:00+00:00"),
        )

    summary = _script().refresh(repo, apply=True)

    mapping = repo.load_org_alias_dict()
    assert key not in mapping and "zz" not in mapping
    assert mapping == {"aa": "aa", "bb": "aa"}
    assert summary["masked_dropped"] == 2


def test_load_org_alias_error_returns_empty_and_warns(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    from src.ui.services import eventnews_org_alias as mod

    def boom() -> dict[str, str]:
        raise RuntimeError("db down")

    warnings: list[str] = []
    monkeypatch.setattr(mod._log, "warning", lambda event, **kw: warnings.append(event))
    monkeypatch.setattr(repo, "load_org_alias_dict", boom)
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")

    assert load_org_alias(repo) == {}
    assert warnings == ["eventnews_focus_org_alias_failed"]


def test_alias_only_removes_conflicts_never_adds_them() -> None:
    names = ["orgx", "orgy", "orgz"]
    alias = {"orgy": "orgx", "orgz": "orgx"}
    for x, y in itertools.product(names, names):
        a = compute_focus([_fa("a", ("victim_org", x), ("cve", "CVE-2026-0001"))])
        b = compute_focus([_fa("b", ("victim_org", y), ("cve", "CVE-2026-0001"))])
        without = match_focus(a, b)
        with_alias = match_focus(a, b, org_alias=alias)
        if without.klass != CLASS_CONFLICT:
            assert with_alias == without  # 衝突しない組は別名で何も変わらない
        else:
            assert set(with_alias.conflicts) <= set(without.conflicts)
    a = compute_focus([_fa("a", ("victim_org", "orgx"))])
    b = compute_focus([_fa("b", ("victim_org", "orgy"))])
    assert match_focus(a, b).klass == CLASS_CONFLICT
    assert match_focus(a, b, org_alias=alias).klass != CLASS_CONFLICT
