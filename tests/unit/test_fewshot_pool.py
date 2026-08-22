"""較正格子 P5 (C8): retrieval few-shot プールのテスト。

few-shot は本番判定の前段に挟まるため、fail-open (どの経路が壊れても注入なしで従来
動作) と自己混入ガード (対象記事自身を教材に引かない = 答えの漏洩) をここで固定する。
既定 off (JUDGMENT_FEWSHOT 未設定) — flag ゲートは classify_judgment 側の試験で確認。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import src.tuning.fewshot_pool as fp
from src.storage.run_history import RunHistoryRepository
from src.tuning.fewshot_pool import get_fewshot_section, invalidate_fewshot_pool

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_pool() -> None:
    invalidate_fewshot_pool()


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "fewshot.db")


def _seed_labeled(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    truth: str,
    body: str,
    category: str = "ransomware",
) -> None:
    iso = _NOW.isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (iso,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " body, category) VALUES (?,?,?,?, 'posted', ?, ?, ?)",
            (
                rid,
                article_id,
                f"title-{article_id}",
                f"https://kuebiko.example/{article_id}",
                iso,
                body,
                category,
            ),
        )
    repo.record_tuning_label(
        dedup_key=f"feedmatch:{article_id}:{truth}",
        field="subject_actor",
        label_value=truth,
        source="E1",
        strength="strong",
        provenance="{}",
        article_id=article_id,
    )


class _FakeEmbedder:
    """テキスト先頭の記号で決め打ちベクトルを返す fake。"""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    async def embed(self, text: str, *, kind: str = "document") -> Any:
        vec = next((v for key, v in self.mapping.items() if key in text), [0.0, 0.0, 1.0])

        class _Resp:
            vector = vec

        return _Resp()


@pytest.fixture(autouse=True)
def no_alias_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """辞書/候補ゲートをテスト用に固定 (候補計算はプール整形の関心外)。"""

    class _Aliases:
        def find_all(self, text: str) -> list[Any]:
            return []

    class _Cand:
        def __init__(self, actor_id: str) -> None:
            self.id = actor_id

    monkeypatch.setattr("src.cti.actor_normalizer.load_actor_aliases", lambda: _Aliases())
    monkeypatch.setattr(
        "src.pipeline.persistence._relevant_actors", lambda found, cat: [_Cand("qilin")]
    )
    monkeypatch.setattr(fp, "_current_rubric_version", lambda: 3)


class TestFewshotSection:
    @pytest.mark.asyncio
    async def test_nearest_examples_are_injected(self, repo: RunHistoryRepository) -> None:
        _seed_labeled(repo, article_id="near", truth="qilin", body="ALPHA " + "x" * 600)
        _seed_labeled(repo, article_id="far", truth="akira", body="BETA " + "y" * 600)
        embedder = _FakeEmbedder(
            {
                "title-near": [1.0, 0.0, 0.0],
                "title-far": [0.0, 1.0, 0.0],
                # near に近いが自己混入ガード (0.97) には掛からない距離
                "QUERY": [0.7, 0.3, 0.0],
            }
        )
        section = await get_fewshot_section(
            title="QUERY 記事",
            body="z" * 300,
            category="ransomware",
            repo=repo,
            embedder=embedder,
            now=_NOW,
            k=1,
        )
        assert "title-near" in section
        assert "qilin" in section
        assert "title-far" not in section
        assert "確定事例" in section  # 例示層のヘッダ

    @pytest.mark.asyncio
    async def test_self_similarity_is_excluded(self, repo: RunHistoryRepository) -> None:
        """対象記事自身 (類似度 ~1.0) を教材に引かない — 答えの漏洩ガード。"""
        _seed_labeled(repo, article_id="self", truth="qilin", body="SAME " + "x" * 600)
        embedder = _FakeEmbedder(
            {"title-self": [1.0, 0.0, 0.0], "QUERY": [1.0, 0.0, 0.0]}  # 完全一致
        )
        section = await get_fewshot_section(
            title="QUERY",
            body="SAME " + "x" * 600,
            category="ransomware",
            repo=repo,
            embedder=embedder,
            now=_NOW,
        )
        assert section == ""  # 唯一の例が自己 → 注入なし

    @pytest.mark.asyncio
    async def test_no_embedder_falls_back_to_category(self, repo: RunHistoryRepository) -> None:
        _seed_labeled(
            repo, article_id="same-cat", truth="qilin", body="x" * 600, category="ransomware"
        )
        _seed_labeled(
            repo, article_id="other-cat", truth="akira", body="y" * 600, category="geopolitical"
        )
        section = await get_fewshot_section(
            title="q",
            body="z" * 300,
            category="ransomware",
            repo=repo,
            embedder=None,
            now=_NOW,
            k=1,
        )
        assert "title-same-cat" in section  # 同カテゴリ優先

    @pytest.mark.asyncio
    async def test_empty_pool_returns_empty(self, repo: RunHistoryRepository) -> None:
        assert (
            await get_fewshot_section(
                title="q", body="z", category=None, repo=repo, embedder=None, now=_NOW
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_broken_repo_fails_open(self) -> None:
        class _Broken:
            def fetch_subject_panel_cases(self, since_iso: str) -> list[Any]:
                raise RuntimeError("db down")

        assert (
            await get_fewshot_section(
                title="q", body="z", category=None, repo=_Broken(), embedder=None, now=_NOW
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_pool_cache_invalidates_on_rubric_version_change(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_labeled(repo, article_id="a1", truth="qilin", body="x" * 600)
        await get_fewshot_section(
            title="q",
            body="z" * 300,
            category="ransomware",
            repo=repo,
            embedder=None,
            now=_NOW,
        )
        # 版が動く → 追加した新例がプール再構築で見えるようになる
        _seed_labeled(repo, article_id="a2", truth="akira", body="y" * 600)
        monkeypatch.setattr(fp, "_current_rubric_version", lambda: 4)
        section = await get_fewshot_section(
            title="q",
            body="z" * 300,
            category="ransomware",
            repo=repo,
            embedder=None,
            now=_NOW,
            k=5,
        )
        assert "title-a2" in section


class TestClassifyInjectionGate:
    @pytest.mark.asyncio
    async def test_flag_off_does_not_inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.cti.judgment_classifier import classify_judgment

        monkeypatch.delenv("JUDGMENT_FEWSHOT", raising=False)

        async def _must_not_call(**kwargs: Any) -> str:
            raise AssertionError("flag off で few-shot を呼んではいけない")

        monkeypatch.setattr("src.tuning.fewshot_pool.get_fewshot_section", _must_not_call)

        captured: dict[str, str] = {}

        class _Llm:
            async def generate_structured(self, prompt: str, **kw: Any) -> Any:
                captured["prompt"] = prompt
                raise RuntimeError("stop here")  # 判定自体は不要

        await classify_judgment(
            _Llm(),  # type: ignore[arg-type]
            title="t",
            category="ransomware",
            body="b" * 300,
            published=None,
            candidates=[],
        )
        assert "確定事例" not in captured["prompt"]

    @pytest.mark.asyncio
    async def test_flag_on_appends_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.cti.judgment_classifier import classify_judgment

        monkeypatch.setenv("JUDGMENT_FEWSHOT", "1")

        async def _fake_section(**kwargs: Any) -> str:
            return "【過去の確定事例 (テスト)】"

        monkeypatch.setattr("src.tuning.fewshot_pool.get_fewshot_section", _fake_section)

        captured: dict[str, str] = {}

        class _Llm:
            async def generate_structured(self, prompt: str, **kw: Any) -> Any:
                captured["prompt"] = prompt
                raise RuntimeError("stop here")

        await classify_judgment(
            _Llm(),  # type: ignore[arg-type]
            title="t",
            category="ransomware",
            body="b" * 300,
            published=None,
            candidates=[],
        )
        assert captured["prompt"].endswith("【過去の確定事例 (テスト)】")
