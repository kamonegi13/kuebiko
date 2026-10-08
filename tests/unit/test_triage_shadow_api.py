"""triage-shadow summary API (GET /api/v1/triage-shadow/summary) のテスト (2026-10-08、M4)。

docs/importance_relevance_redesign.md §6b の go/no-go 判断用 read API。運用系の観測データ
なので公開 instance からは default-deny で遮断される (ops_notices と同型)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.storage.repo_triage_shadow import TriageShadowRow
from src.storage.run_history import RunHistoryRepository
from src.ui.read_only_policy import is_read_only_blocked_get
from tests.unit.test_ui_app import _bootstrap_project


@pytest.fixture
def shadow_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, RunHistoryRepository]]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    from src.ui import app as app_module

    fresh_app = app_module.create_app()
    with TestClient(fresh_app) as c:
        yield c, RunHistoryRepository()


def _row(
    i: int,
    *,
    current_kept: bool,
    new_kept: bool,
    hint_reasons: tuple[str, ...] = (),
    title: str = "",
) -> TriageShadowRow:
    return TriageShadowRow(
        article_id=f"rss:{i}",
        url=f"https://a.example/{i}",
        title=title or f"記事 {i}",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="high" if current_kept else "low",
        current_kept=current_kept,
        flat_importance="medium" if new_kept else "low",
        hint_fired=bool(hint_reasons),
        hint_reasons=hint_reasons,
        new_kept=new_kept,
    )


def test_summary_counts_2x2(shadow_client: tuple[TestClient, RunHistoryRepository]) -> None:
    client, repo = shadow_client
    repo.record_triage_shadow(
        [
            _row(1, current_kept=True, new_kept=True),
            _row(2, current_kept=True, new_kept=False),
            _row(3, current_kept=False, new_kept=True),
        ]
    )

    res = client.get("/api/v1/triage-shadow/summary?days=7")

    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["both_kept"] == 1
    assert data["summary"]["current_only"] == 1
    assert data["summary"]["new_only"] == 1
    assert data["summary"]["total"] == 3


def test_summary_lists_disagreements(
    shadow_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    client, repo = shadow_client
    repo.record_triage_shadow(
        [
            _row(1, current_kept=True, new_kept=True, title="一致"),
            _row(2, current_kept=True, new_kept=False, title="食い違い記事"),
        ]
    )

    res = client.get("/api/v1/triage-shadow/summary?days=7")

    data = res.json()
    assert len(data["disagreements"]) == 1
    assert data["disagreements"][0]["title"] == "食い違い記事"


def test_japan_dropped_by_new_rule_field_is_present(
    shadow_client: tuple[TestClient, RunHistoryRepository],
) -> None:
    """``japan_dropped_by_new_rule`` は非負整数で返る (0 件なら 0)。

    タイトルからの国名判定 (``src.cti.nation_gazetteer``) 自体の正しさは
    ``tests/unit/test_ingest_relevance.py`` が固定する — この bootstrap 用の
    テスト project は ``config/cti/countries.yaml`` を持たないため、ここでは
    フィールドの型/存在だけを確かめる。
    """
    client, repo = shadow_client
    repo.record_triage_shadow([_row(1, current_kept=True, new_kept=False, title="食い違い記事")])

    res = client.get("/api/v1/triage-shadow/summary?days=7")

    assert isinstance(res.json()["japan_dropped_by_new_rule"], int)
    assert res.json()["japan_dropped_by_new_rule"] >= 0


def test_summary_v2_field_present(shadow_client: tuple[TestClient, RunHistoryRepository]) -> None:
    client, repo = shadow_client
    rescued = TriageShadowRow(
        article_id="rss:1",
        url="https://a.example/1",
        title="rescued",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="low",
        current_kept=False,
        flat_importance="low",
        hint_fired=False,
        hint_reasons=(),
        new_kept=False,
        jp_prob=0.5,
        jp_ml_fired=True,
        jp_cascade=True,
        new_kept_v2=True,
    )
    repo.record_triage_shadow([rescued])

    res = client.get("/api/v1/triage-shadow/summary?days=7")

    data = res.json()
    assert "summary_v2" in data
    for key in (
        "both_kept",
        "current_only",
        "new_only",
        "both_dropped",
        "total",
        "rescued",
        "rescued_label_unknown",
    ):
        assert key in data["summary_v2"]
    assert data["summary_v2"]["rescued"] == 1


def test_not_in_public_get_allowlist() -> None:
    assert is_read_only_blocked_get("/api/v1/triage-shadow/summary") is True
