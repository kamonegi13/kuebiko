"""蒸留ヘッド v0 シャドー推論配線 (src/tuning/head_shadow.py) のテスト。

§14.3 の要件: 本番配信に影響しない fail-open (flag off / 成果物不在 / 版不一致は
黙って 0 件) と、triage 全判定 (棄却分含む) に対する記録・disagree_cutoff 判定。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tuning import head_shadow
from src.tuning.head_model import save_artifact, train_head

_DIM = 4
_EMBED_MODEL = "snowflake-arctic-embed2"
_KEEP = {"high", "medium"}

# クラスごとに明確に分離した合成ベクトル (ヘッドの予測を決定論化する)
_PROTO = {
    "high": [1.0, 0.0, 0.0, 0.0],
    "medium": [0.0, 1.0, 0.0, 0.0],
    "low": [0.0, 0.0, 1.0, 0.0],
}


@pytest.fixture(autouse=True)
def _reset_artifact_cache() -> Iterator[None]:
    head_shadow.reset_cache()
    yield
    head_shadow.reset_cache()


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "shadow.db")


def _make_artifact(directory: Path) -> None:
    rng = np.random.default_rng(7)
    vecs: list[list[float]] = []
    labels: list[str] = []
    for label, proto in _PROTO.items():
        for _ in range(30):
            noise = rng.normal(0.0, 0.05, size=_DIM)
            vecs.append([p + n for p, n in zip(proto, noise, strict=True)])
            labels.append(label)
    x = np.asarray(vecs, dtype=np.float32)
    cats: list[str | None] = [None] * len(labels)
    bundle, meta = train_head(
        x,
        labels,
        cats,
        x[:9],
        labels[:9],
        cats[:9],
        embedding_model=_EMBED_MODEL,
        dim=_DIM,
        trained_at=datetime(2026, 8, 22, tzinfo=UTC).isoformat(),
    )
    save_artifact(directory, bundle, meta)


def _article(aid: str) -> Article:
    return Article(
        id=aid,
        title=f"article {aid}",
        url=f"https://kuebiko.example/{aid}",
        summary_html="<p>s</p>",
        author=None,
        published=datetime.now(UTC),
        feed_title="Test Feed",
        feed_url="https://kuebiko.example/feed",
    )


def _embeddings(*items: tuple[str, str, str]) -> dict[str, tuple[str, list[float]]]:
    """(article_id, importance プロトタイプ, embedding モデル名) → 埋め込み dict。"""
    return {aid: (model, list(_PROTO[proto])) for aid, proto, model in items}


class TestFailOpen:
    def test_returns_zero_when_flag_disabled(
        self, tmp_path: Path, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(head_shadow.ENV_FLAG, "0")
        _make_artifact(tmp_path / "head")

        recorded = head_shadow.record_head_shadow(
            decisions=[(_article("a1"), "high", False)],
            embeddings=_embeddings(("a1", "high", _EMBED_MODEL)),
            kept_ids={"a1"},
            keep_importance=_KEEP,
            repo=repo,
            run_id=None,
            artifact_dir=tmp_path / "head",
        )

        assert recorded == 0
        assert repo.head_shadow_stats(days=7).total == 0

    def test_returns_zero_when_artifact_missing(
        self, tmp_path: Path, repo: RunHistoryRepository
    ) -> None:
        recorded = head_shadow.record_head_shadow(
            decisions=[(_article("a1"), "high", False)],
            embeddings=_embeddings(("a1", "high", _EMBED_MODEL)),
            kept_ids={"a1"},
            keep_importance=_KEEP,
            repo=repo,
            run_id=None,
            artifact_dir=tmp_path / "no-such-artifact",
        )

        assert recorded == 0

    def test_skips_vectors_from_different_embedding_model(
        self, tmp_path: Path, repo: RunHistoryRepository
    ) -> None:
        artifact = tmp_path / "head"
        _make_artifact(artifact)

        recorded = head_shadow.record_head_shadow(
            decisions=[(_article("a1"), "high", False)],
            embeddings=_embeddings(("a1", "high", "some-other-embedder")),
            kept_ids={"a1"},
            keep_importance=_KEEP,
            repo=repo,
            run_id=None,
            artifact_dir=artifact,
        )

        assert recorded == 0


class TestRecording:
    def test_records_all_triage_decisions_including_rejected(
        self, tmp_path: Path, repo: RunHistoryRepository
    ) -> None:
        artifact = tmp_path / "head"
        _make_artifact(artifact)
        # a-keep は triage 通過 (high)、a-cut は low 棄却 — 棄却分も記録される (§13-7)
        decisions = [
            (_article("a-keep"), "high", False),
            (_article("a-cut"), "low", False),
        ]

        recorded = head_shadow.record_head_shadow(
            decisions=decisions,
            embeddings=_embeddings(
                ("a-keep", "high", _EMBED_MODEL), ("a-cut", "low", _EMBED_MODEL)
            ),
            kept_ids={"a-keep"},
            keep_importance=_KEEP,
            repo=repo,
            run_id="42",
            artifact_dir=artifact,
        )

        assert recorded == 2
        stats = repo.head_shadow_stats(days=7)
        assert stats.total == 2
        # ヘッドと triage が同じ判定 (分離クラスタなので予測は決定論的) → 不一致 0
        assert stats.agree_with_triage == 2
        assert stats.disagree_cutoff == 0

    def test_flags_cutoff_disagreement_when_head_and_llm_cross_keep_boundary(
        self, tmp_path: Path, repo: RunHistoryRepository
    ) -> None:
        artifact = tmp_path / "head"
        _make_artifact(artifact)
        # embedding は high クラスタ → ヘッドは high (残す) と予測するが、
        # triage LLM は low (切る) と判定した — 足切り境界の不一致
        decisions = [(_article("a1"), "low", False)]

        recorded = head_shadow.record_head_shadow(
            decisions=decisions,
            embeddings=_embeddings(("a1", "high", _EMBED_MODEL)),
            kept_ids=set(),
            keep_importance=_KEEP,
            repo=repo,
            run_id=None,
            artifact_dir=artifact,
        )

        assert recorded == 1
        stats = repo.head_shadow_stats(days=7)
        assert stats.disagree_cutoff == 1
        assert stats.by_head_importance == {"high": 1}

    def test_recorded_probs_are_valid_json_distribution(
        self, tmp_path: Path, repo: RunHistoryRepository
    ) -> None:
        artifact = tmp_path / "head"
        _make_artifact(artifact)

        head_shadow.record_head_shadow(
            decisions=[(_article("a1"), "high", False)],
            embeddings=_embeddings(("a1", "high", _EMBED_MODEL)),
            kept_ids={"a1"},
            keep_importance=_KEEP,
            repo=repo,
            run_id=None,
            artifact_dir=artifact,
        )

        with repo._connect() as conn:  # noqa: SLF001 — 検証のための直接読み
            row = conn.execute(
                "SELECT head_importance_probs, artifact_version FROM head_shadow"
            ).fetchone()
        probs = json.loads(row["head_importance_probs"])
        assert set(probs) == {"high", "medium", "low"}
        assert abs(sum(probs.values()) - 1.0) < 1e-6
        assert row["artifact_version"]
