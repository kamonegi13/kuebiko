"""毎時の群化への差し込み (resolve_org_identity)。判定役と名前の取得は差し替える。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.config_loader import AppConfig
from src.eventnews import org_identity as oi
from src.eventnews.models import ItemState, MemberArticle
from src.storage.run_history import RunHistoryRepository
from src.ui.services import eventnews_org_identity as svc


def _article(aid: str, key: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"{key} で漏えい",
        url=f"https://x.example/{aid}",
        feed_title="f",
        feed_url="u",
        host="x.example",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=datetime(2026, 10, 1, tzinfo=UTC),
        summary="",
        body=f"{key} で漏えいが起きた。",
        entities=frozenset({("victim_org", key)}),
    )


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "t.db")


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("EVENTNEWS_ORG_NORM", "1")
    monkeypatch.setattr(
        svc,
        "_victim_names",
        lambda repo, ids: {"c": ["Times Car"], "m": ["タイムズカー"]},
    )
    calls: list[str] = []

    async def fake_judge(a: str, ca: str, b: str, cb: str, **_: object) -> float | None:
        calls.append(a)
        return 14.0

    monkeypatch.setattr(oi, "judge_name_pair", fake_judge)
    return calls


_VECTORS: dict[str, list[float]] = {"c": [1.0, 0.0], "m": [1.0, 0.0]}


def _state(member_ids: tuple[str, ...]) -> ItemState:
    ts = datetime(2026, 10, 1, tzinfo=UTC)
    return ItemState(
        item_id="i",
        first_reported_at=ts,
        last_reported_at=ts,
        status="new",
        importance="high",
        current_version=0,
        member_ids=member_ids,
    )


async def _run(
    repo: RunHistoryRepository,
) -> tuple[list[MemberArticle], list[tuple[ItemState, list[MemberArticle]]]]:
    cand = _article("c", "timescar")
    member = _article("m", "タイムズカー")
    return await svc.resolve_org_identity(
        repo,
        AppConfig(),
        [cand],
        [(_state(("m",)), [member])],
        _VECTORS,
    )


@pytest.mark.asyncio
async def test_off_does_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.delenv(oi.ORG_IDENTITY_ENV, raising=False)
    new_c, _ = await _run(repo)
    assert new_c[0].entities == frozenset({("victim_org", "timescar")})
    assert wiring == []


@pytest.mark.asyncio
async def test_shadow_judges_and_stores_but_does_not_apply(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "shadow")
    new_c, _ = await _run(repo)
    assert new_c[0].entities == frozenset({("victim_org", "timescar")})
    assert repo.get_org_identity_logits([oi.pair_key("Times Car", "タイムズカー")])


@pytest.mark.asyncio
async def test_on_remaps_victim_keys_of_candidates_and_members(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "1")
    new_c, new_existing = await _run(repo)
    assert new_c[0].entities == new_existing[0][1][0].entities


@pytest.mark.asyncio
async def test_second_run_reads_the_cache_instead_of_judging(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "shadow")
    await _run(repo)
    await _run(repo)
    assert len(wiring) == 1


@pytest.mark.asyncio
async def test_requires_org_norm(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "1")
    monkeypatch.setenv("EVENTNEWS_ORG_NORM", "0")
    await _run(repo)
    assert wiring == []


@pytest.mark.asyncio
async def test_judge_failure_leaves_candidates_untouched(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository, wiring: list[str]
) -> None:
    monkeypatch.setenv(oi.ORG_IDENTITY_ENV, "1")

    async def boom(*_: object, **__: object) -> float | None:
        raise RuntimeError("down")

    monkeypatch.setattr(oi, "judge_name_pair", boom)
    new_c, _ = await _run(repo)
    assert new_c[0].entities == frozenset({("victim_org", "timescar")})
