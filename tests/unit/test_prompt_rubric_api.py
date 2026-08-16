"""summarizer 判定基準 API (``/api/v1/prompts/summarizer/*``) のテスト (WP2)。

設計書 §8 WP2 テスト表 ①〜⑫ を実装する。LLM は stub (実 LLM を呼ばない —
``model`` property 必須という既存の教訓を踏襲した ``FakeLLM``)。DB は
tmp SQLite (``rubric_store`` の WP1 テストと同じ fixture 形)。
"""

from __future__ import annotations

import inspect
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import src.prompts.rubric_store as rubric_store
import src.ui.api.prompt_rubric as prompt_rubric
from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_model import RubricSection, SummarizerRubric
from src.storage import config_store
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient, LLMError, LLMResponse
from src.ui.api import config_history
from src.ui.api.config_history import RevertRequest
from src.ui.api.pages import pages_api
from src.ui.api.prompt_rubric import PreviewRubricRequest, prompt_rubric_api
from src.ui.read_only_policy import build_read_only_middleware, is_read_only_blocked_get
from src.ui.services.file_editor import FileEditor

_T = TypeVar("_T", bound=BaseModel)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_SOURCE = _REPO_ROOT / "config" / "prompts" / "summarizer_rubric.yaml"
_PERSONA_SOURCE = _REPO_ROOT / "prompts" / "_persona.j2"

_ENDPOINT = "/api/v1/prompts/summarizer/rubric"


class FakeLLM(LLMClient):
    """§8 ⑥: 実 LLM を呼ばない。``model`` property 必須 (実装忘れの再発防止の教訓)。"""

    def __init__(self, output: SummaryOutput | None = None, fail: bool = False) -> None:
        self.calls = 0
        self.last_prompt = ""
        self._output = output
        self._fail = fail

    @property
    def model(self) -> str:
        return "fake-summarizer-model"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

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
        self.calls += 1
        self.last_prompt = prompt
        if self._fail:
            raise LLMError("boom")
        assert self._output is not None
        return cast(_T, self._output)


def _sample_output() -> SummaryOutput:
    return SummaryOutput(
        title_ja="テスト記事の和訳タイトル",
        importance="high",
        category="apt",
        summary="テスト要約。",
    )


def _rubric_payload(intro: str = "テスト用の導入文。") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "intro": intro,
        "sections": [
            {
                "field_id": "title_ja",
                "kind": "rubric",
                "title": "和訳タイトル",
                "body": "タイトルの和訳基準。",
                "note": "",
            },
            {
                "field_id": "summary",
                "kind": "rubric",
                "title": "要約",
                "body": "要約の基準。",
                "note": "",
            },
        ],
        "examples": [],
    }


def _add_article(repo: RunHistoryRepository, article_id: str, *, body: str) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="Real Article Title",
            url=f"https://news.kuebiko.example/{article_id}",
            status="posted",
            importance="high",
        ),
    )
    repo.update_article_body(article_id, body)


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """DB 隔離 (SQLite fallback を tmp に閉じ込める) + persona include の解決先。"""
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    shutil.copy(_SEED_SOURCE, tmp_path / "config" / "prompts" / "summarizer_rubric.yaml")
    (tmp_path / "prompts").mkdir()
    shutil.copy(_PERSONA_SOURCE, tmp_path / "prompts" / "_persona.j2")
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")
    monkeypatch.chdir(tmp_path)
    rubric_store.invalidate_summarizer_cache()
    yield tmp_path
    rubric_store.invalidate_summarizer_cache()


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "articles.db")


@pytest.fixture
def client(_env: Path, repo: RunHistoryRepository) -> TestClient:
    app = FastAPI()
    app.include_router(prompt_rubric_api)
    app.state.repo = repo
    return TestClient(app)


@pytest.fixture
def readonly_client(_env: Path, repo: RunHistoryRepository) -> TestClient:
    app = FastAPI()
    app.include_router(prompt_rubric_api)
    app.state.repo = repo
    app.middleware("http")(build_read_only_middleware(None))
    return TestClient(app)


# ---------- ① ② GET /rubric ----------


class TestGetRubric:
    def test_returns_rubric_contract_and_runtime(self, client: TestClient) -> None:
        resp = client.get(_ENDPOINT)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["rubric"]["sections"]) == 24  # WP1 seed の全 24 field 被覆
        assert len(body["contract"]["fields"]) == 24
        assert body["runtime"]["active_source"] in ("composed", "legacy_file")
        assert body["runtime"]["composer_enabled"] is True
        assert body["runtime"]["legacy_path"] == "prompts/briefing/summarizer.j2"

    def test_contract_has_importance_enum(self, client: TestClient) -> None:
        resp = client.get(_ENDPOINT)
        fields = {f["name"]: f for f in resp.json()["contract"]["fields"]}

        assert set(fields["importance"]["enum"]) == {"high", "medium", "low"}
        assert fields["importance"]["required"] is True
        # 宣言状況との突合 (declared/kind/unknown/missing) は preview 応答に一本化
        # (GET 側に持つと消費者ゼロの write-only になるため返さない)。
        assert "declared" not in fields["title_ja"]

    def test_guide_covers_every_section(self, client: TestClient) -> None:
        """全セクションに group と「効く先」が付く (UI のグルーピングが欠けない)。"""
        guide = client.get(_ENDPOINT).json()["guide"]

        assert len(guide["fields"]) == 24
        assert guide["unmapped_fields"] == []
        by_id = {f["field_id"]: f for f in guide["fields"]}
        assert by_id["importance"]["group"] == "classification"
        assert by_id["importance"]["effect"]  # 空でない
        assert by_id["diamond"]["group"] == "suppressed"  # kind=suppressed が優先される


# ---------- ③ ④ PUT /rubric ----------


class TestPutRubric:
    def test_broken_jinja_in_body_is_rejected_before_save(self, client: TestClient) -> None:
        """I6: section body の壊れた Jinja 構文は保存前の render 検証 (400) で落とす。

        code-review 2026-08-16 HIGH #1 の再発防止 — これが無いと壊れた判定基準が
        「保存成功」トーストとともに version 保存され、次 run から legacy へ落ちる。
        """
        payload = _rubric_payload()
        payload["sections"][0]["body"] = "broken jinja {{ nonexistent_var here"

        resp = client.put(_ENDPOINT, json={"rubric": payload, "note": "probe"})

        assert resp.status_code == 400
        assert "レンダリングに失敗" in resp.json()["detail"]
        # 保存されていないこと (version が付いていない = 履歴ゼロ)
        assert config_store.list_history("summarizer_rubric", limit=1) == []

    def test_saving_increments_version(self, client: TestClient) -> None:
        first = client.put(_ENDPOINT, json={"rubric": _rubric_payload(), "note": "first"})
        assert first.status_code == 200
        assert first.json()["version"] == 1

        second = client.put(
            _ENDPOINT, json={"rubric": _rubric_payload(intro="second"), "note": "second"}
        )
        assert second.json()["version"] == 2
        assert "composed_chars" in second.json()
        assert "legacy_chars" in second.json()

    def test_rejects_unknown_field(self, client: TestClient) -> None:
        payload = _rubric_payload()
        payload["sections"].append(
            {"field_id": "victim_org", "kind": "rubric", "body": "typo フィールド。"}
        )

        resp = client.put(_ENDPOINT, json={"rubric": payload, "note": "bad"})

        assert resp.status_code == 400
        assert "victim_org" in resp.json()["detail"]

    def test_saved_value_is_readable_via_get(self, client: TestClient) -> None:
        client.put(
            _ENDPOINT, json={"rubric": _rubric_payload(intro="保存確認用の導入文"), "note": "x"}
        )

        rubric_store.invalidate_summarizer_cache()
        resp = client.get(_ENDPOINT)

        assert resp.json()["rubric"]["intro"] == "保存確認用の導入文"


# ---------- ⑤ POST /rubric/preview ----------


class TestPreviewRubric:
    def test_always_200_and_reports_errors_for_broken_rubric(self, client: TestClient) -> None:
        payload = _rubric_payload()
        payload["sections"].append(
            {"field_id": "victim_org", "kind": "rubric", "body": "typo フィールド。"}
        )

        resp = client.post(f"{_ENDPOINT}/preview", json={"rubric": payload})

        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert any("victim_org" in e for e in body["errors"])
        assert body["composed"]
        # issues は errors/warnings と同内容を局在化情報つきで返す (後方互換で両方存在)
        unknown = [i for i in body["issues"] if i["code"] == "unknown_field"]
        assert len(unknown) == 1
        assert unknown[0]["field_id"] == "victim_org"
        assert unknown[0]["severity"] == "error"
        assert unknown[0]["message"] in body["errors"]

    def test_example_mismatch_is_localised_to_the_example(self, client: TestClient) -> None:
        """出力例の schema 不一致が「何番目の例のどのキーか」まで分かる形で返る。

        実際に踏んだ欠陥の再現: 有効値に無い ``article_type`` を few-shot が教えていた。
        他のキーは正しい例にして、報告が不正な 1 キーに絞られることを見る。
        """
        payload = _rubric_payload()
        payload["examples"] = [
            {
                "label": "壊れた例",
                "json_text": json.dumps(
                    {
                        "title_ja": "テスト記事",
                        "importance": "low",
                        "category": "geopolitical",
                        "summary": "要約本文。",
                        "article_type": "informational",  # ArticleType Literal に無い値
                    },
                    ensure_ascii=False,
                ),
            }
        ]

        body = client.post(f"{_ENDPOINT}/preview", json={"rubric": payload}).json()

        mismatch = [i for i in body["issues"] if i["code"] == "example_schema_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0]["example_index"] == 1
        assert mismatch[0]["keys"] == ["article_type"]
        assert mismatch[0]["severity"] == "warning"  # 保存は拒否しない

    def test_valid_rubric_renders_sample_with_persona(self, client: TestClient) -> None:
        resp = client.post(f"{_ENDPOINT}/preview", json={"rubric": _rubric_payload()})

        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["errors"] == []
        assert "CTI" in body["rendered_sample"]  # persona include が解決されている
        assert "Threat actor" in body["rendered_sample"]  # SAMPLE_ARTICLE のタイトル

    def test_omitted_rubric_uses_current_saved_value(self, client: TestClient) -> None:
        client.put(_ENDPOINT, json={"rubric": _rubric_payload(intro="現行値の導入文"), "note": "x"})
        rubric_store.invalidate_summarizer_cache()

        resp = client.post(f"{_ENDPOINT}/preview", json={})

        assert resp.status_code == 200
        assert "現行値の導入文" in resp.json()["composed"]


# ---------- ⑥ ⑦ POST /rubric/test (LLM dry-run) ----------


class TestDryRunTest:
    def test_stub_llm_reports_output_and_empty_fields(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLLM(output=_sample_output())
        monkeypatch.setattr(prompt_rubric, "build_llm_for", lambda *a, **k: fake)

        resp = client.post(f"{_ENDPOINT}/test", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model"] == "fake-summarizer-model"
        assert body["output"]["title_ja"] == "テスト記事の和訳タイトル"
        assert "iocs" in body["empty_fields"]
        assert fake.calls == 1

    def test_llm_failure_returns_200_ok_false(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeLLM(fail=True)
        monkeypatch.setattr(prompt_rubric, "build_llm_for", lambda *a, **k: fake)

        resp = client.post(f"{_ENDPOINT}/test", json={})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["output"] is None
        assert "boom" in body["error"]

    def test_unknown_article_id_returns_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*a: Any, **k: Any) -> LLMClient:
            raise AssertionError("記事未発見で 404 になる前に LLM が構築された")

        monkeypatch.setattr(prompt_rubric, "build_llm_for", _boom)

        resp = client.post(f"{_ENDPOINT}/test", json={"article_id": "does-not-exist"})

        assert resp.status_code == 404

    def test_invalid_rubric_returns_400(self, client: TestClient) -> None:
        payload = _rubric_payload()
        payload["sections"].append(
            {"field_id": "victim_org", "kind": "rubric", "body": "typo フィールド。"}
        )

        resp = client.post(f"{_ENDPOINT}/test", json={"rubric": payload})

        assert resp.status_code == 400

    def test_real_article_id_is_used_in_prompt(
        self,
        client: TestClient,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _add_article(repo, "a1", body="Real article body text about a threat actor.")
        fake = FakeLLM(output=_sample_output())
        monkeypatch.setattr(prompt_rubric, "build_llm_for", lambda *a, **k: fake)

        resp = client.post(f"{_ENDPOINT}/test", json={"article_id": "a1"})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "Real article body text about a threat actor." in fake.last_prompt


# ---------- ⑧ ⑨ 公開面の遮断 ----------


class TestFieldStats:
    def test_returns_every_field(self, client: TestClient, _env: Path) -> None:
        # 集計は既定 DB (cwd 配下の SQLite fallback) を見るので、そこに schema を作る。
        RunHistoryRepository()
        resp = client.get(f"{_ENDPOINT}/field-stats?days=30")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["fields"]) == 24  # カードから消えるフィールドを作らない
        assert body["window"]["days"] == 30
        assert body["denominator"]["note"]  # 母集団の説明が必ず付く (I-8)

    def test_days_out_of_range_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{_ENDPOINT}/field-stats?days=0").status_code == 422
        assert client.get(f"{_ENDPOINT}/field-stats?days=91").status_code == 422

    def test_endpoint_is_sync_def(self) -> None:
        """重い集計を async で書くと event loop を占有し UI 全体が固まる (PIR 画面の教訓)。"""
        assert inspect.iscoroutinefunction(prompt_rubric.field_stats) is False


class TestReadOnlyExposure:
    def test_denylist_prefix_blocks_rubric_get(self) -> None:
        assert is_read_only_blocked_get(_ENDPOINT) is True

    def test_denylist_prefix_blocks_field_stats(self) -> None:
        assert is_read_only_blocked_get(f"{_ENDPOINT}/field-stats") is True

    def test_readonly_get_is_blocked_anonymously(self, readonly_client: TestClient) -> None:
        resp = readonly_client.get(_ENDPOINT)
        assert resp.status_code == 403

    def test_readonly_put_is_blocked(self, readonly_client: TestClient) -> None:
        resp = readonly_client.put(_ENDPOINT, json={"rubric": _rubric_payload()})
        assert resp.status_code == 403

    def test_readonly_preview_and_test_are_blocked(self, readonly_client: TestClient) -> None:
        assert readonly_client.post(f"{_ENDPOINT}/preview", json={}).status_code == 403
        assert readonly_client.post(f"{_ENDPOINT}/test", json={}).status_code == 403


# ---------- ⑩ ⑪ config-history 連携 ----------


class TestConfigHistoryIntegration:
    def test_known_keys_include_summarizer_rubric(self) -> None:
        assert "summarizer_rubric" in config_history._KNOWN_KEYS

    async def test_revert_invalidates_composer_cache(self, _env: Path, client: TestClient) -> None:
        client.put(_ENDPOINT, json={"rubric": _rubric_payload(intro="v1 の導入文"), "note": "v1"})
        client.put(_ENDPOINT, json={"rubric": _rubric_payload(intro="v2 の導入文"), "note": "v2"})
        before = rubric_store.load_rubric()
        assert before is not None
        assert before.intro == "v2 の導入文"

        await config_history.revert_config("summarizer_rubric", RevertRequest(version=1))

        after = rubric_store.load_rubric()
        assert after is not None
        assert after.intro == "v1 の導入文"


# ---------- ⑫ prompts_save の rollback 注記 ----------


@pytest.fixture
def pages_client(tmp_path: Path) -> TestClient:
    (tmp_path / "prompts" / "briefing").mkdir(parents=True)
    (tmp_path / "prompts" / "briefing" / "summarizer.j2").write_text(
        "legacy content", encoding="utf-8"
    )
    (tmp_path / "prompts" / "_persona.j2").write_text("persona", encoding="utf-8")
    app = FastAPI()
    app.include_router(pages_api)
    app.state.file_editor = FileEditor(project_root=tmp_path)
    return TestClient(app)


class TestPromptsSaveRollbackNotice:
    def test_summarizer_save_reports_ineffective_when_composer_active(
        self, pages_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rubric_store, "build_summarizer_template", lambda path: object())

        resp = pages_client.post(
            "/api/v1/prompts/save",
            data={"path": "prompts/briefing/summarizer.j2", "content": "edited"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] is True
        assert body["path"] == "prompts/briefing/summarizer.j2"
        assert body["effective"] is False
        assert "構造化編集" in body["notice"]

    def test_summarizer_save_has_no_notice_when_composer_inactive(
        self, pages_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rubric_store, "build_summarizer_template", lambda path: None)

        resp = pages_client.post(
            "/api/v1/prompts/save",
            data={"path": "prompts/briefing/summarizer.j2", "content": "edited"},
        )

        assert resp.json() == {"saved": True, "path": "prompts/briefing/summarizer.j2"}

    def test_other_prompt_save_never_gets_notice(
        self, pages_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rubric_store, "build_summarizer_template", lambda path: object())

        resp = pages_client.post(
            "/api/v1/prompts/save",
            data={"path": "prompts/_persona.j2", "content": "edited persona"},
        )

        assert resp.json() == {"saved": True, "path": "prompts/_persona.j2"}


# ---------- I8: API は .j2 へ一切書き戻さない ----------


class TestLegacyFileUntouched:
    """判定基準の保存/プレビュー/テストは常に DB のみを触り、実 .j2 は変更しない。"""

    async def test_preview_never_writes_real_legacy_file(self) -> None:
        legacy = _REPO_ROOT / "prompts" / "briefing" / "summarizer.j2"
        before = legacy.read_bytes()
        before_mtime = legacy.stat().st_mtime

        rubric = SummarizerRubric(
            intro="不変性確認用",
            sections=[RubricSection(field_id="summary", body="要約の基準。")],
        )
        result = await prompt_rubric.preview_rubric(PreviewRubricRequest(rubric=rubric))

        assert result["valid"] is True
        assert legacy.read_bytes() == before
        assert legacy.stat().st_mtime == before_mtime

    def test_put_and_preview_never_create_legacy_file_in_isolated_env(
        self, client: TestClient, _env: Path
    ) -> None:
        legacy = _env / "prompts" / "briefing" / "summarizer.j2"
        assert not legacy.exists()

        client.put(_ENDPOINT, json={"rubric": _rubric_payload(), "note": "n"})
        client.post(f"{_ENDPOINT}/preview", json={"rubric": _rubric_payload()})

        assert not legacy.exists()
