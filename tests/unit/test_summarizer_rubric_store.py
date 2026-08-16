"""判定基準 store (DB SSoT + seed + cache + fail-safe) の unit test (WP1)。

**無音で legacy に倒れないこと**が本 module の存在理由なので、fallback の各経路が
``None`` を返し WARNING を残すこと、かつログにプロンプト本文が乗らないことまで固定する。
SQLite fallback を使うため tmp に data/ を置いて chdir する (test_source_store と同型)。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import jinja2
import pytest

from src.prompts import rubric_store
from src.prompts.rubric_model import SummarizerRubric
from src.storage import config_store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_SOURCE = _REPO_ROOT / "config" / "prompts" / "summarizer_rubric.yaml"
_TEMPLATE_PATH = Path("prompts/briefing/summarizer.j2")


class _LogRecorder:
    """structlog の BoundLogger 差し替え用 (warning のみ記録)。"""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))

    def info(self, event: str, **fields: object) -> None:
        return None


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    shutil.copy(_SEED_SOURCE, tmp_path / "config" / "prompts" / "summarizer_rubric.yaml")
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("DATABASE_URL", raising=False)  # SQLite fallback を tmp に閉じ込める
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")
    monkeypatch.chdir(tmp_path)
    rubric_store.invalidate_summarizer_cache()
    yield tmp_path
    rubric_store.invalidate_summarizer_cache()


def _db_rubric(intro: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "intro": intro,
        "sections": [{"field_id": "summary", "kind": "rubric", "body": "DB 版の基準。"}],
        "examples": [],
    }


def test_db_absent_falls_back_to_seed_yaml(_env: Path) -> None:
    rubric = rubric_store.load_rubric()

    assert rubric is not None
    assert len(rubric.sections) == 24
    assert rubric.sections[0].field_id == "title_ja"


def test_db_value_wins_over_seed(_env: Path) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric("DB 由来の導入文"), note="t")
    rubric_store.invalidate_summarizer_cache()

    rubric = rubric_store.load_rubric()

    assert rubric is not None
    assert rubric.intro == "DB 由来の導入文"


def test_broken_db_value_falls_back_to_seed(_env: Path) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, ["dict ではない"], note="broken")
    rubric_store.invalidate_summarizer_cache()

    rubric = rubric_store.load_rubric()

    assert rubric is not None
    assert len(rubric.sections) == 24  # seed に fail-safe している


def test_seed_rubric_if_absent_is_idempotent(_env: Path) -> None:
    assert rubric_store.seed_rubric_if_absent() is True
    # 2 回目は既投入なので yaml を再取り込みしない
    assert rubric_store.seed_rubric_if_absent() is False
    stored = config_store.get_config(rubric_store.CONFIG_KEY)
    assert isinstance(stored, dict)
    assert len(stored["sections"]) == 24


def test_cache_holds_until_invalidated(_env: Path) -> None:
    first = rubric_store.load_rubric()
    assert first is not None
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric("新しい導入文"), note="t")

    # invalidate 前は cache が効いている
    assert rubric_store.load_rubric() is first

    rubric_store.invalidate_summarizer_cache()
    reloaded = rubric_store.load_rubric()
    assert reloaded is not None
    assert reloaded.intro == "新しい導入文"


def test_flag_off_returns_none(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "0")

    assert rubric_store.is_composer_enabled() is False
    assert rubric_store.build_summarizer_template(_TEMPLATE_PATH) is None
    assert "SUMMARIZER_COMPOSER=0" in rubric_store.last_fallback_reason()


def test_compose_failure_returns_none_and_warns_without_prompt_body(
    _env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(rubric_store, "_log", recorder)

    def _boom(rubric: SummarizerRubric, *, path: Path) -> jinja2.Template:
        # jinja2 の TemplateSyntaxError は該当行 (= プロンプト本文) を str に含む
        raise ValueError("template body: **記事タイトルの日本語訳 (60 字以内)**")

    monkeypatch.setattr(rubric_store, "build_template", _boom)

    assert rubric_store.build_summarizer_template(_TEMPLATE_PATH) is None
    events = [event for event, _ in recorder.warnings]
    assert "summarizer_composer_fallback" in events
    # CLAUDE.md §4: プロンプト本文をログに出さない (理由・型名・件数のみ)
    logged = " ".join(str(v) for _, fields in recorder.warnings for v in fields.values())
    assert "記事タイトルの日本語訳" not in logged
    assert "ValueError" in logged
    assert "合成に失敗" in rubric_store.last_fallback_reason()


def test_success_path_caches_template_and_clears_reason(_env: Path) -> None:
    template = rubric_store.build_summarizer_template(_TEMPLATE_PATH)

    assert template is not None
    assert rubric_store.build_summarizer_template(_TEMPLATE_PATH) is template
    assert rubric_store.last_fallback_reason() == ""
