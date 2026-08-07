"""記事詳細の単記事操作 API (src/ui/api/article_ops.py) のテスト。

POST /api/v1/articles/{id}/translate — body_ja キャッシュ / 404 / LLM 失敗時の非キャッシュ。
GET  /api/v1/articles/{id}/stix      — 単記事 STIX 2.1 bundle (entities + body 由来 IoC)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import src.ui.api.article_ops as article_ops
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.llm_client import LLMClient, LLMError, LLMResponse

_T = TypeVar("_T", bound=BaseModel)


class FakeLLM(LLMClient):
    def __init__(self, text: str = "翻訳された本文", fail: bool = False) -> None:
        self.calls = 0
        self._text = text
        self._fail = fail

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self._fail:
            raise LLMError("boom")
        return LLMResponse(text=self._text, model="fake-model")

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
        max_attempts: int = 3,
    ) -> _T:
        raise NotImplementedError


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "ops.db")


@pytest.fixture
def client(repo: RunHistoryRepository) -> TestClient:
    app = FastAPI()
    app.include_router(article_ops.article_ops_api)
    app.state.repo = repo
    return TestClient(app)


def _add(repo: RunHistoryRepository, aid: str, body: str | None = None) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"Title {aid}",
            url=f"https://example.com/{aid}",
            status="posted",
            importance="high",
        ),
    )
    if body is not None:
        repo.update_article_body(aid, body)


# ---------- POST /translate ----------


def test_translate_unknown_article_404(client: TestClient) -> None:
    resp = client.post("/api/v1/articles/nope/translate")
    assert resp.status_code == 404


def test_translate_without_body_404(client: TestClient, repo: RunHistoryRepository) -> None:
    _add(repo, "a1", body=None)
    resp = client.post("/api/v1/articles/a1/translate")
    assert resp.status_code == 404
    assert "本文" in resp.json()["detail"]


def test_translate_success_caches(
    client: TestClient, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(repo, "a1", body="English body text.")
    fake = FakeLLM(text="日本語の訳文")
    monkeypatch.setattr(article_ops, "build_llm_for", lambda *a, **k: fake)

    resp = client.post("/api/v1/articles/a1/translate")

    assert resp.status_code == 200
    data = resp.json()
    assert data["body_ja"] == "日本語の訳文"
    assert data["cached"] is False
    assert repo.get_article_body_ja("a1") == "日本語の訳文"


def test_translate_cache_hit_skips_llm(
    client: TestClient, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(repo, "a1", body="English body text.")
    repo.update_article_body_ja("a1", "既存の訳")

    def _boom(*a: Any, **k: Any) -> LLMClient:
        raise AssertionError("cache hit なのに LLM が構築された")

    monkeypatch.setattr(article_ops, "build_llm_for", _boom)
    resp = client.post("/api/v1/articles/a1/translate")

    assert resp.status_code == 200
    assert resp.json() == {"body_ja": "既存の訳", "cached": True}


def test_translate_llm_failure_502_and_no_cache(
    client: TestClient, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(repo, "a1", body="English body text.")
    monkeypatch.setattr(article_ops, "build_llm_for", lambda *a, **k: FakeLLM(fail=True))

    resp = client.post("/api/v1/articles/a1/translate")

    assert resp.status_code == 502
    # 失敗時は部分訳・原文をキャッシュしない
    assert repo.get_article_body_ja("a1") is None


def test_translate_japanese_body_rejected_400(
    client: TestClient, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 日本語原文の「翻訳」は逆方向 (日→英) に壊れるため 400 で拒否
    _add(repo, "a1", body="これは既に日本語で書かれた記事本文です。")

    def _boom(*a: Any, **k: Any) -> LLMClient:
        raise AssertionError("日本語原文なのに LLM が構築された")

    monkeypatch.setattr(article_ops, "build_llm_for", _boom)
    resp = client.post("/api/v1/articles/a1/translate")

    assert resp.status_code == 400
    assert "日本語" in resp.json()["detail"]


def test_translate_llm_init_failure_503(
    client: TestClient, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(repo, "a1", body="English body text.")

    def _no_llm(*a: Any, **k: Any) -> LLMClient:
        raise RuntimeError("ollama down")

    monkeypatch.setattr(article_ops, "build_llm_for", _no_llm)
    resp = client.post("/api/v1/articles/a1/translate")
    assert resp.status_code == 503


# ---------- GET /stix ----------


def test_stix_unknown_article_404(client: TestClient) -> None:
    resp = client.get("/api/v1/articles/nope/stix")
    assert resp.status_code == 404


def test_stix_bundle_from_entities_and_body(client: TestClient, repo: RunHistoryRepository) -> None:
    # body には IP、entities には CVE — 両ソースが bundle に合流することを検証
    # (TEST-NET 系 IP / .example ドメインは extractor が非ルーティング可能として
    # 弾くため、実在形式の値を使う)
    _add(repo, "a1", body="C2 server at 45.77.33.12 was observed.")
    repo.add_article_entities(
        "a1", [("cve", "CVE-2026-12345"), ("ioc_domain", "evil-c2-infra.net")]
    )

    resp = client.get("/api/v1/articles/a1/stix")

    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    bundle = resp.json()
    assert bundle["type"] == "bundle"
    dumped = str(bundle["objects"])
    assert "45.77.33.12" in dumped  # body 由来
    assert "CVE-2026-12345" in dumped  # entities 由来
    assert "evil-c2-infra.net" in dumped


def test_stix_works_without_body(client: TestClient, repo: RunHistoryRepository) -> None:
    # 90 日 retention で body 消滅後も entities だけで export できる
    _add(repo, "a1", body=None)
    repo.add_article_entities("a1", [("ioc_ip", "91.219.236.5")])

    resp = client.get("/api/v1/articles/a1/stix")

    assert resp.status_code == 200
    assert "91.219.236.5" in str(resp.json()["objects"])
