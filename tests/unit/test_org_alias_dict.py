"""被害組織名の別名辞書 (ORG-D-IMPL): pairs との同値・dict の読み口・script の dry-run/apply。"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from src.config_loader import AppConfig
from src.cti.org_normalizer import org_match_key
from src.eventnews import org_identity as oi
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
