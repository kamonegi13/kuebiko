"""要約埋込の永続化 (2026-09-21)。

⚠ 以前はシャドー観測用に**その場で作って捨てて**いた。事象どうしの統合を毎時
回すには永続化が要る — 全期間で総当たりすると 13,000 件を毎回作り直すことになり、
ML は一瞬なのに埋込生成で数十分かかっていた。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHistoryRepository:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return RunHistoryRepository(db_path=tmp_path / "t.db")


def test_roundtrip_preserves_the_vector(repo: RunHistoryRepository) -> None:
    v = np.array([0.1, -0.2, 0.3], dtype=np.float32)

    assert repo.save_summary_embeddings({"a1": v}, model="m") == 1
    got = repo.load_summary_embeddings(["a1"])

    assert np.allclose(got["a1"], v)


def test_missing_ids_are_simply_absent(repo: RunHistoryRepository) -> None:
    """⭐ 無いものは黙って欠ける — 呼び手が「足りない分だけ作る」ため。"""
    repo.save_summary_embeddings({"a1": np.array([1.0], dtype=np.float32)}, model="m")

    got = repo.load_summary_embeddings(["a1", "a2"])

    assert set(got) == {"a1"}


def test_upsert_replaces_the_previous_vector(repo: RunHistoryRepository) -> None:
    repo.save_summary_embeddings({"a1": np.array([1.0, 0.0], dtype=np.float32)}, model="m1")
    repo.save_summary_embeddings({"a1": np.array([0.0, 1.0], dtype=np.float32)}, model="m2")

    got = repo.load_summary_embeddings(["a1"])

    assert np.allclose(got["a1"], [0.0, 1.0])


def test_empty_input_touches_nothing(repo: RunHistoryRepository) -> None:
    assert repo.save_summary_embeddings({}, model="m") == 0
    assert repo.load_summary_embeddings([]) == {}


def test_dimension_mismatch_is_dropped_not_returned_wrong(repo: RunHistoryRepository) -> None:
    """⚠ 列ずれと同じ思想 — 壊れた行を黙って使わない。"""
    repo.save_summary_embeddings({"a1": np.array([1.0, 2.0], dtype=np.float32)}, model="m")
    with repo._connect() as conn:  # noqa: SLF001 — 破損を仕込む
        conn.execute("UPDATE summary_embeddings SET dim = 99 WHERE article_id = 'a1'")
        conn.commit()

    assert repo.load_summary_embeddings(["a1"]) == {}
