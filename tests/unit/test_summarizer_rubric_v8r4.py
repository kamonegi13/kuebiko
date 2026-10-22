"""要約の基準 v8r4 (s24c) の切替 flag (2026-10-22)。

``SUMMARIZER_RUBRIC_V8R4=1`` のときだけ DB より優先して v8r4 を読む。flag 無し (本番の s23i) は
従来どおり DB → seed のままであることを固定する。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.prompts import rubric_store
from src.prompts.rubric_model import SummarizerRubric, validate_rubric
from src.storage import config_store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS = _REPO_ROOT / "config" / "prompts"
_TEMPLATE_PATH = Path("prompts/briefing/summarizer.j2")
_FLAG = rubric_store.V8R4_ENV_FLAG


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    for name in ("summarizer_rubric.yaml", "summarizer_rubric_v8r4.yaml"):
        shutil.copy(_PROMPTS / name, tmp_path / "config" / "prompts" / name)
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.setenv("SUMMARIZER_COMPOSER", "1")
    monkeypatch.chdir(tmp_path)
    rubric_store.invalidate_summarizer_cache()
    yield tmp_path
    rubric_store.invalidate_summarizer_cache()


def _db_rubric() -> dict[str, object]:
    return {
        "schema_version": 1,
        "intro": "DB 由来の導入文",
        "sections": [{"field_id": "summary", "kind": "rubric", "body": "DB 版の基準。"}],
        "examples": [],
    }


def test_v8r4_yaml_is_valid_and_composes() -> None:
    import yaml

    raw = yaml.safe_load((_PROMPTS / "summarizer_rubric_v8r4.yaml").read_text(encoding="utf-8"))
    rubric = SummarizerRubric.model_validate(raw)
    assert len(rubric.sections) == 24
    assert validate_rubric(rubric).errors == ()


def test_flag_unset_keeps_db_rubric(_env: Path) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric(), note="t")
    rubric = rubric_store.load_rubric()
    assert rubric is not None and rubric.intro == "DB 由来の導入文"


def test_flag_unset_without_db_keeps_seed(_env: Path) -> None:
    seed = rubric_store.load_seed_rubric()
    assert rubric_store.load_rubric() == seed


def test_flag_on_wins_over_db(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric(), note="t")
    monkeypatch.setenv(_FLAG, "1")
    rubric = rubric_store.load_rubric()
    assert rubric is not None
    assert rubric.intro != "DB 由来の導入文"
    assert rubric == rubric_store.load_seed_rubric(rubric_store.V8R4_PATH)
    assert rubric != rubric_store.load_seed_rubric()  # seed (旧版) とも別物


def test_flag_zero_keeps_db(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric(), note="t")
    monkeypatch.setenv(_FLAG, "0")
    rubric = rubric_store.load_rubric()
    assert rubric is not None and rubric.intro == "DB 由来の導入文"


def test_flag_on_missing_file_falls_back_to_db_with_warning(
    _env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_store.save_config(rubric_store.CONFIG_KEY, _db_rubric(), note="t")
    (_env / "config" / "prompts" / "summarizer_rubric_v8r4.yaml").unlink()
    monkeypatch.setenv(_FLAG, "1")
    rubric = rubric_store.load_rubric()
    assert rubric is not None and rubric.intro == "DB 由来の導入文"


def test_template_follows_flag(_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copytree(_REPO_ROOT / "prompts", _env / "prompts")
    off = rubric_store.build_summarizer_template(_TEMPLATE_PATH)
    monkeypatch.setenv(_FLAG, "1")
    rubric_store.invalidate_summarizer_cache()
    on = rubric_store.build_summarizer_template(_TEMPLATE_PATH)
    assert off is not None and on is not None and off is not on
