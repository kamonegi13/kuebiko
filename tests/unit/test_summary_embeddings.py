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


def test_embedding_text_is_title_blank_line_summary() -> None:
    """要約埋込の入力の形は 1 か所で決める (群化・補完・割当の関門で食い違った、2026-09-24)。"""
    from src.storage.repo_summary_embeddings import summary_embedding_text

    assert summary_embedding_text("見出し", "要約") == "見出し\n\n要約"
    assert summary_embedding_text("見出し", None) == "見出し"
    assert summary_embedding_text("", "") == ""


def test_summary_embedding_inputs_reads_title_and_summary(repo: RunHistoryRepository) -> None:
    from src.storage.repo_summary_embeddings import summary_embedding_text

    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status)"
            " VALUES ('2026-09-24T00:00:00+00:00', 't', 0, 'done')"
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, summary, url, status, created_at)"
            " VALUES (?, 'a1', '見出し', '要約', 'https://kuebiko.example/1', 'posted',"
            " '2026-09-24T00:00:00+00:00')",
            (rid,),
        )
    got = repo.summary_embedding_inputs(["a1", "missing"])

    assert got == {"a1": summary_embedding_text("見出し", "要約")}
