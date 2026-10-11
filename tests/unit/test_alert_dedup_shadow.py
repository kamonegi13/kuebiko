"""重複 alert の ML の影の記録 (R1h): 配信を変えない・flag・repo・API。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.cti import alert_dedup_shadow as shadow
from src.cti.alert_dedup_features import FEATURE_NAMES
from src.cti.alert_dedup_model import AlertDedupModel
from src.eventnews.pair_model import PairModel
from src.storage.repo_alert_dedup_shadow import AlertDedupShadowRow
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.discord_publisher import BriefingMessage
from src.tools.url_normalizer import url_hash
from tests.unit.test_ui_app import _bootstrap_project

_EMBED_MODEL = "test-embed-model"
_VEC = [1.0, 0.0, 0.0, 0.0]


def _tree(proba: float) -> dict[str, list[float]]:
    return {"left": [-1], "right": [-1], "feature": [-2], "threshold": [0.0], "proba": [proba]}


def _pair_model() -> PairModel:
    return PairModel(
        feature_names=tuple(f"f{i}" for i in range(25)),
        threshold_llm_on=0.5,
        threshold_llm_off=0.5,
        _trees=(_tree(0.9),),
    )


def _alert_model(proba: float = 0.8) -> AlertDedupModel:
    inner = PairModel(
        feature_names=FEATURE_NAMES,
        threshold_llm_on=0.5,
        threshold_llm_off=0.5,
        _trees=(_tree(proba),),
    )
    return AlertDedupModel("v-test", 0.5, inner)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "shadow.db")


@pytest.fixture(autouse=True)
def _models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "load_model", _pair_model)
    monkeypatch.setattr(shadow, "load_alert_dedup_model", lambda: _alert_model())
    monkeypatch.delenv(shadow.SHADOW_ENV, raising=False)


def _seed_prior(
    repo: RunHistoryRepository, article_id: str = "prior", hours_ago: float = 2.0
) -> None:
    url = f"https://example.com/{article_id}"
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="先行",
            summary="先行の要約",
            url=url,
            category="apt",
            feed_title="feed",
            status="posted",
            posted_channel="alert",
            created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        )
    )
    h = url_hash(url)
    repo.mark_url_seen(url_hash=h, url=url, article_id=article_id, title="先行")
    repo.add_article_embedding(url_hash=h, url=url, vector=_VEC, model=_EMBED_MODEL)


def _msg() -> BriefingMessage:
    return BriefingMessage(
        title="今回", bluf="", importance="high", category="apt", summary="要約", metadata={}
    )


def _record(repo: RunHistoryRepository, msg: BriefingMessage) -> bool:
    return shadow.record_alert_dedup_shadow(
        msg=msg,
        article=None,
        article_id="cur",
        embedding=(_EMBED_MODEL, list(_VEC)),
        repo=repo,
    )


class TestRecord:
    def test_records_row_and_leaves_msg_untouched(self, repo: RunHistoryRepository) -> None:
        _seed_prior(repo)
        msg = _msg()
        before = msg.model_dump()

        recorded = _record(repo, msg)

        assert recorded is True
        assert msg.model_dump() == before
        rows = repo.list_alert_dedup_shadow(days=1)
        assert len(rows) == 1
        assert rows[0].prior_article_id == "prior"
        assert rows[0].model_version == "v-test"
        assert rows[0].would_suppress is True
        assert list(json.loads(rows[0].features_json)) == list(FEATURE_NAMES)

    def test_flag_off_records_nothing(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_prior(repo)
        monkeypatch.setenv(shadow.SHADOW_ENV, "0")

        assert _record(repo, _msg()) is False
        assert repo.list_alert_dedup_shadow(days=1) == []

    def test_no_prior_makes_no_row(self, repo: RunHistoryRepository) -> None:
        assert _record(repo, _msg()) is False
        assert repo.list_alert_dedup_shadow(days=1) == []

    def test_no_embedding_or_repo_is_noop(self, repo: RunHistoryRepository) -> None:
        msg = _msg()
        no_embedding = shadow.record_alert_dedup_shadow(
            msg=msg, article=None, article_id="cur", embedding=None, repo=repo
        )
        no_repo = shadow.record_alert_dedup_shadow(
            msg=msg, article=None, article_id="cur", embedding=(_EMBED_MODEL, _VEC), repo=None
        )

        assert no_embedding is False
        assert no_repo is False

    def test_model_missing_is_noop(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_prior(repo)
        monkeypatch.setattr(shadow, "load_alert_dedup_model", lambda: None)

        assert _record(repo, _msg()) is False

    def test_exception_is_logged_not_raised(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_prior(repo)

        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(repo, "record_alert_dedup_shadow", boom)

        assert _record(repo, _msg()) is False

    def test_candidate_must_be_alert_posted(self, repo: RunHistoryRepository) -> None:
        _seed_prior(repo)
        # 同じ ベクトルでも watch 投稿は先行にならない
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="w",
                title="w",
                url="https://example.com/w",
                status="posted",
                posted_channel="watch",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        h = url_hash("https://example.com/w")
        repo.mark_url_seen(url_hash=h, url="https://example.com/w", article_id="w", title="w")
        repo.add_article_embedding(
            url_hash=h, url="https://example.com/w", vector=_VEC, model=_EMBED_MODEL
        )

        _record(repo, _msg())

        assert [r.prior_article_id for r in repo.list_alert_dedup_shadow(days=1)] == ["prior"]


class TestRepo:
    def test_purge_removes_old_rows(self, repo: RunHistoryRepository) -> None:
        repo.record_alert_dedup_shadow(AlertDedupShadowRow("a", "b", 0.3, "v", "{}", False))

        assert repo.purge_alert_dedup_shadow(days=0) == 1
        assert repo.alert_dedup_shadow_p_values(days=7) == []


@pytest.fixture
def api_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, RunHistoryRepository]]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    from src.ui import app as app_module

    with TestClient(app_module.create_app()) as c:
        yield c, RunHistoryRepository()


class TestApi:
    def test_summary_counts_and_histogram(
        self, api_client: tuple[TestClient, RunHistoryRepository]
    ) -> None:
        client, repo = api_client
        for i, (p, w) in enumerate([(0.1, False), (0.95, True), (0.7, True)]):
            repo.record_alert_dedup_shadow(
                AlertDedupShadowRow(f"a{i}", "prior", p, "v1", json.dumps({"cos": 0.9}), w)
            )

        data = client.get("/api/v1/alert-dedup-shadow/summary?days=7").json()

        assert data["total"] == 3
        assert data["would_suppress"] == 2
        assert data["p_histogram"][1] == 1
        assert data["p_histogram"][9] == 1
        assert len(data["recent"]) == 3
        assert data["recent"][0]["features"] == {"cos": 0.9}

    def test_summary_is_not_public(self) -> None:
        from src.ui.read_only_policy import is_read_only_blocked_get

        assert is_read_only_blocked_get("/api/v1/alert-dedup-shadow/summary") is True


def test_fill_rate_metric_registered() -> None:
    from src.ui.services.fill_rate_audit import METRICS

    assert "alert_dedup_shadow" in {m.key for m in METRICS}
