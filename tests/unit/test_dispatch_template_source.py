"""``dispatch._load_template`` がどちらの層からプロンプトを得るかの unit test (WP1)。

I1/I2/I3: ``_load_template`` は Grok 経路・本文再取得 backlog・scripts の 6 経路が共有する
唯一の入口なので、**差し込みは内側だけ / 戻り値の型と使い方は不変 / 失敗時は必ず legacy** を
固定する。DB には触らせない (config_store.get_config を差し替えて seed yaml 経路に閉じる)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import jinja2
import pytest

from src.pipeline import dispatch
from src.prompts import rubric_store
from src.prompts.rubric_model import SummarizerRubric
from src.prompts.sample_article import SAMPLE_ARTICLE, SAMPLE_BODY
from src.prompts.summarizer_composer import RUBRIC_HEADING
from src.storage import config_store

_LEGACY_HEADING = "# 出力スキーマ (JSON のみ、前置き・解説不要)"


def _no_db_config(key: str, *, db_path: Path | None = None) -> None:
    """DB を触らずに「未投入」を返す (seed yaml 経路に閉じる)。"""
    return None


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(config_store, "get_config", _no_db_config)
    rubric_store.invalidate_summarizer_cache()
    yield
    rubric_store.invalidate_summarizer_cache()


def _render(template: jinja2.Template) -> str:
    return template.render(article=SAMPLE_ARTICLE, body=SAMPLE_BODY)


def test_composed_template_is_used_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")

    rendered = _render(dispatch._load_template())

    assert RUBRIC_HEADING in rendered
    assert _LEGACY_HEADING not in rendered
    assert "JSON のみ出力してください。" not in rendered
    assert SAMPLE_ARTICLE.title in rendered


def test_legacy_file_is_used_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "0")

    rendered = _render(dispatch._load_template())

    assert _LEGACY_HEADING in rendered
    assert "JSON のみ出力してください。" in rendered
    assert RUBRIC_HEADING not in rendered


def test_compose_failure_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")

    def _boom(rubric: SummarizerRubric, *, path: Path) -> jinja2.Template:
        raise RuntimeError("compose failed")

    monkeypatch.setattr(rubric_store, "build_template", _boom)

    rendered = _render(dispatch._load_template())

    assert _LEGACY_HEADING in rendered
    assert rubric_store.last_fallback_reason() != ""


def test_non_default_path_always_uses_legacy_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")
    custom_dir = tmp_path / "prompts" / "briefing"
    custom_dir.mkdir(parents=True)
    custom = custom_dir / "custom.j2"
    custom.write_text("CUSTOM {{ body }}", encoding="utf-8")

    template = dispatch._load_template(custom)

    rendered = template.render(article=SAMPLE_ARTICLE, body=SAMPLE_BODY)
    assert rendered.startswith("CUSTOM ")
    assert RUBRIC_HEADING not in rendered
