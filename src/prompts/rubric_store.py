"""summarizer 判定基準の store (DB SSoT + seed yaml + cache + fail-safe)。

routing / PIR / ソース config と同じく **config_store (DB, app_config_versions) を runtime
SSoT** にし、``config/prompts/summarizer_rubric.yaml`` は初回 seed 専用にする。

本 module の唯一の役目は「``_load_template`` が **必ず** テンプレートを得られること」であり、
env flag / DB 障害 / 壊れた値 / 合成失敗 はすべてここで吸収して ``None`` を返す
(= 呼び出し側は legacy .j2 に倒す)。**ただし黙って倒さない** — 失敗は必ず WARNING を残し、
理由は ``last_fallback_reason()`` から UI で参照できる。

CLAUDE.md §4: プロンプト本文・記事本文をログに出さない。ログに出すのは理由と型名のみ。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import jinja2
import yaml

from src.logging_config import get_logger
from src.prompts.rubric_model import SummarizerRubric
from src.prompts.summarizer_composer import build_template

_log = get_logger(__name__)

CONFIG_KEY = "summarizer_rubric"
SEED_PATH = Path("config/prompts/summarizer_rubric.yaml")
ENV_FLAG = "SUMMARIZER_COMPOSER"

_RUBRIC_CACHE: SummarizerRubric | None = None
_TEMPLATE_CACHE: jinja2.Template | None = None
_TEMPLATE_CACHE_KEY: str = ""
_FALLBACK_REASON: str = ""


def is_composer_enabled() -> bool:
    """構造化判定基準を使うか (既定 ON)。``SUMMARIZER_COMPOSER=0`` で legacy .j2 へ rollback。"""
    return os.environ.get(ENV_FLAG, "1") != "0"


def last_fallback_reason() -> str:
    """直近に legacy へ倒れた理由 (空文字 = 合成が使われている)。"""
    return _FALLBACK_REASON


def invalidate_summarizer_cache() -> None:
    """rubric / テンプレートの process cache を落とす (保存・revert 後に呼ぶ)。"""
    global _RUBRIC_CACHE, _TEMPLATE_CACHE, _TEMPLATE_CACHE_KEY, _FALLBACK_REASON  # noqa: PLW0603
    _RUBRIC_CACHE = None
    _TEMPLATE_CACHE = None
    _TEMPLATE_CACHE_KEY = ""
    _FALLBACK_REASON = ""


def load_seed_rubric(path: Path | None = None) -> SummarizerRubric | None:
    """seed yaml を読む (存在しない / 壊れていれば None、fail-safe)。"""
    seed_path = path or SEED_PATH
    try:
        raw: Any = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        # 例外文字列は yaml の該当行 (= プロンプト本文) を含みうるため型名のみ残す
        _log.warning(
            "summarizer_rubric_seed_read_failed",
            path=str(seed_path),
            error_type=type(e).__name__,
        )
        return None
    return _validate_rubric_value(raw, source="seed")


def _validate_rubric_value(value: Any, *, source: str) -> SummarizerRubric | None:
    if not isinstance(value, dict):
        _log.warning(
            "summarizer_rubric_value_invalid",
            source=source,
            reason="dict ではありません",
            value_type=type(value).__name__,
        )
        return None
    try:
        return SummarizerRubric.model_validate(value)
    except Exception as e:  # noqa: BLE001  pydantic ValidationError は本文を含むため型名のみ
        _log.warning(
            "summarizer_rubric_value_invalid",
            source=source,
            reason="スキーマ検証に失敗",
            error_type=type(e).__name__,
        )
        return None


def _load_from_db() -> SummarizerRubric | None:
    try:
        from src.storage.config_store import get_config

        value = get_config(CONFIG_KEY)
    except Exception as e:  # noqa: BLE001  DB 障害でも legacy 運用は継続させる
        _log.warning("summarizer_rubric_db_read_failed", error=str(e))
        return None
    if value is None:
        return None
    return _validate_rubric_value(value, source="db")


def load_rubric() -> SummarizerRubric | None:
    """現在の判定基準。DB → seed yaml → None の順に解決する (壊れた値は seed に fail-safe)。"""
    global _RUBRIC_CACHE  # noqa: PLW0603
    if _RUBRIC_CACHE is not None:
        return _RUBRIC_CACHE
    rubric = _load_from_db() or load_seed_rubric()
    if rubric is not None:
        _RUBRIC_CACHE = rubric
    return rubric


def seed_rubric_if_absent() -> bool:
    """DB 未投入なら seed yaml を version 1 として取り込む (起動時、full instance)。"""
    rubric = load_seed_rubric()
    if rubric is None:
        _log.warning("summarizer_rubric_seed_missing", path=str(SEED_PATH))
        return False
    try:
        from src.storage.config_store import seed_config_if_absent

        seeded = seed_config_if_absent(
            CONFIG_KEY,
            rubric.model_dump(),
            note=f"初期 seed ({SEED_PATH.name})",
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("summarizer_rubric_seed_failed", error=str(e))
        return False
    if seeded:
        invalidate_summarizer_cache()
    return seeded


def _fallback(reason: str, **fields: object) -> None:
    global _FALLBACK_REASON  # noqa: PLW0603
    _FALLBACK_REASON = reason
    _log.warning("summarizer_composer_fallback", reason=reason, **fields)


def build_summarizer_template(path: Path) -> jinja2.Template | None:
    """合成テンプレートを返す。flag OFF / rubric 不在 / 合成失敗 なら None (= legacy へ倒す)。"""
    global _TEMPLATE_CACHE, _TEMPLATE_CACHE_KEY, _FALLBACK_REASON  # noqa: PLW0603
    if not is_composer_enabled():
        # 意図的な rollback なので WARNING にはしない (理由だけ残して UI に出す)
        _FALLBACK_REASON = f"{ENV_FLAG}=0 (legacy .j2 を使用)"
        return None
    key = str(path)
    if _TEMPLATE_CACHE is not None and key == _TEMPLATE_CACHE_KEY:
        return _TEMPLATE_CACHE
    rubric = load_rubric()
    if rubric is None:
        _fallback("判定基準が DB にも seed yaml にも見つかりません", path=key)
        return None
    try:
        template = build_template(rubric, path=path)
    except Exception as e:  # noqa: BLE001  合成失敗でも本番は legacy で走り続ける
        # 例外文字列はテンプレート該当行 (= プロンプト本文) を含みうるため型名のみ
        _fallback(
            "判定基準の合成に失敗しました",
            error_type=type(e).__name__,
            sections=len(rubric.sections),
        )
        return None
    _TEMPLATE_CACHE = template
    _TEMPLATE_CACHE_KEY = key
    _FALLBACK_REASON = ""
    return template
