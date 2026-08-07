"""src.storage.run_history.article_embeddings のテスト (Phase 3b)。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "embed.db")


def _seed_dedup(repo: RunHistoryRepository, url: str, h: str) -> None:
    """外部キー制約を満たすため dedup_seen_urls に先に登録。"""
    repo.mark_url_seen(url_hash=h, url=url, title="t")


class TestAddArticleEmbedding:
    def test_round_trip(self, repo: RunHistoryRepository) -> None:
        h = "h1" * 16  # 32 chars
        _seed_dedup(repo, "https://example.com/a", h)
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/a",
            vector=[0.1, 0.2, 0.3],
            model="m",
            title="A",
        )
        loaded = repo.get_embedding(h)
        assert loaded is not None
        assert loaded.shape == (3,)
        assert pytest.approx(loaded[0], rel=1e-5) == 0.1

    def test_overwrite(self, repo: RunHistoryRepository) -> None:
        h = "h2" * 16
        _seed_dedup(repo, "https://example.com/b", h)
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/b",
            vector=[1.0, 0.0],
            model="m",
        )
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/b",
            vector=[0.0, 1.0],
            model="m",
        )
        loaded = repo.get_embedding(h)
        assert loaded is not None
        assert pytest.approx(loaded.tolist()) == [0.0, 1.0]

    def test_count(self, repo: RunHistoryRepository) -> None:
        for i in range(3):
            h = f"h{i:032d}"
            _seed_dedup(repo, f"https://example.com/{i}", h)
            repo.add_article_embedding(
                url_hash=h,
                url=f"https://example.com/{i}",
                vector=[float(i), 0.0],
                model="m",
            )
        assert repo.count_embeddings() == 3
        assert repo.count_embeddings(model="m") == 3
        assert repo.count_embeddings(model="other") == 0

    def test_rejects_non_1d_vector(self, repo: RunHistoryRepository) -> None:
        h = "hh" * 16
        _seed_dedup(repo, "https://example.com/x", h)
        with pytest.raises(ValueError, match="1 次元"):
            repo.add_article_embedding(
                url_hash=h,
                url="https://example.com/x",
                vector=[[0.1, 0.2], [0.3, 0.4]],  # type: ignore[list-item]
                model="m",
            )


class TestFindSimilar:
    def test_returns_none_when_empty(self, repo: RunHistoryRepository) -> None:
        result = repo.find_similar_embedding([0.1, 0.2, 0.3], model="m")
        assert result is None

    def test_finds_identical_vector(self, repo: RunHistoryRepository) -> None:
        h = "h1" * 16
        _seed_dedup(repo, "https://example.com/a", h)
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/a",
            vector=[1.0, 0.0, 0.0],
            model="m",
        )
        result = repo.find_similar_embedding(
            [1.0, 0.0, 0.0],
            model="m",
            threshold=0.99,
        )
        assert result is not None
        assert result[0] == h
        assert result[1] == pytest.approx(1.0, rel=1e-5)

    def test_finds_orthogonal_no_match(self, repo: RunHistoryRepository) -> None:
        h = "h2" * 16
        _seed_dedup(repo, "https://example.com/b", h)
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/b",
            vector=[1.0, 0.0],
            model="m",
        )
        # 直交 (cos sim = 0)
        assert repo.find_similar_embedding([0.0, 1.0], model="m", threshold=0.5) is None

    def test_returns_best_match(self, repo: RunHistoryRepository) -> None:
        # 2 件保存。クエリにより近い方の url_hash が返る。
        h_p = "p" * 32
        h_q = "q" * 32
        for url, vec, h in [
            ("https://example.com/p", [1.0, 0.0], h_p),
            ("https://example.com/q", [0.7, 0.7], h_q),
        ]:
            _seed_dedup(repo, url, h)
            repo.add_article_embedding(
                url_hash=h,
                url=url,
                vector=vec,
                model="m",
            )
        # クエリは [1, 0] とほぼ同じ → /p の h が返る
        result = repo.find_similar_embedding([0.99, 0.05], model="m", threshold=0.9)
        assert result is not None
        assert result[0] == h_p
        assert result[1] >= 0.95

    def test_filters_by_model(self, repo: RunHistoryRepository) -> None:
        # 異なるモデルの embedding は混ざらない
        for model_name in ("m1", "m2"):
            h = f"h{model_name}" + "x" * (32 - 2 - len(model_name))
            _seed_dedup(repo, f"https://example.com/{model_name}", h)
            repo.add_article_embedding(
                url_hash=h,
                url=f"https://example.com/{model_name}",
                vector=[1.0, 0.0],
                model=model_name,
            )
        result = repo.find_similar_embedding([1.0, 0.0], model="m1", threshold=0.99)
        assert result is not None
        # m2 のエントリは無視される
        result_other = repo.find_similar_embedding(
            [1.0, 0.0],
            model="other",
            threshold=0.5,
        )
        assert result_other is None

    def test_handles_zero_query(self, repo: RunHistoryRepository) -> None:
        h = "hz" * 16
        _seed_dedup(repo, "https://example.com/z", h)
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/z",
            vector=[1.0, 0.0],
            model="m",
        )
        # ゼロベクトル query は None を返す (NaN を避ける)
        assert repo.find_similar_embedding([0.0, 0.0], model="m", threshold=0.5) is None


class TestMissingEmbedding:
    @staticmethod
    def _seed_article(repo: RunHistoryRepository, url: str) -> None:
        from datetime import UTC, datetime

        from src.storage.run_history import ArticleRecord, RunRecord

        rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(run_id=rid, article_id=url, title="t", url=url, status="posted")
        )

    def test_lists_article_urls_without_embedding(self, repo: RunHistoryRepository) -> None:
        # has: dedup + article + embedding / missing: dedup + article、embedding 無し
        repo.mark_url_seen(url_hash="a" * 32, url="https://e/has", title="has")
        self._seed_article(repo, "https://e/has")
        repo.add_article_embedding(
            url_hash="a" * 32, url="https://e/has", vector=[1.0, 0.0], model="m"
        )
        repo.mark_url_seen(url_hash="b" * 32, url="https://e/missing", title="missing")
        self._seed_article(repo, "https://e/missing")

        rows = repo.list_urls_missing_embedding(model="m")
        urls = {u for _h, u, _t, _f in rows}
        assert "https://e/missing" in urls
        assert "https://e/has" not in urls

    def test_excludes_dedup_urls_without_article(self, repo: RunHistoryRepository) -> None:
        # 記事化されなかった重複 URL (dedup のみ、article 無し) は対象外
        repo.mark_url_seen(url_hash="c" * 32, url="https://e/orphan", title="orphan")
        rows = repo.list_urls_missing_embedding(model="m")
        assert "https://e/orphan" not in {u for _h, u, _t, _f in rows}

    def test_other_model_counts_as_missing(self, repo: RunHistoryRepository) -> None:
        repo.mark_url_seen(url_hash="d" * 32, url="https://e/x", title="x")
        self._seed_article(repo, "https://e/x")
        repo.add_article_embedding(url_hash="d" * 32, url="https://e/x", vector=[1.0], model="old")
        # 別 model 視点では未生成扱い
        rows = repo.list_urls_missing_embedding(model="new")
        assert "https://e/x" in {u for _h, u, _t, _f in rows}


class TestCascadeDelete:
    def test_purge_dedup_cascades_to_embedding(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        from datetime import UTC, datetime, timedelta

        h = "h_old" + "x" * 27
        very_old = datetime.now(UTC) - timedelta(days=400)
        repo.mark_url_seen(
            url_hash=h,
            url="https://old.example.com/",
            when=very_old,
        )
        repo.add_article_embedding(
            url_hash=h,
            url="https://old.example.com/",
            vector=[1.0, 0.0],
            model="m",
            when=very_old,
        )
        assert repo.count_embeddings() == 1
        deleted = repo.purge_old_dedup_entries(days=365)
        assert deleted == 1
        # 外部キーカスケードで embedding も消える
        assert repo.count_embeddings() == 0


class TestNumpyTypes:
    def test_uses_float32(self, repo: RunHistoryRepository) -> None:
        """BLOB が float32 になっていることを byte 数で確認。"""
        h = "h_dtype" + "x" * 25
        _seed_dedup(repo, "https://example.com/d", h)
        # 4 dim float32 → 16 bytes
        repo.add_article_embedding(
            url_hash=h,
            url="https://example.com/d",
            vector=[0.1, 0.2, 0.3, 0.4],
            model="m",
        )
        loaded = repo.get_embedding(h)
        assert loaded is not None
        # nbytes = 4 dim * 4 bytes/float32 = 16
        assert loaded.nbytes == 16
        assert loaded.dtype.itemsize == 4
        # NaN や Inf が混じっていない
        assert not math.isnan(float(loaded[0]))
