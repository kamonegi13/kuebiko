"""SIR 別の要点 v6.2 (s24c 用、2026-10-22)。

s24c は SIR 別の要点を「字数・文数の指示なし・SIR に当たらない記事は当たらないと明示する」
v6.2 の描画で学習した (教師 = ``data/eval/S24B_switch_prompts/pir_daily_focus_v6_2.json`` と同一)。
学習と本番の入力を揃えるため、**s24c のとき (または ``PIR_FOCUS_PROMPT_V6=1``) だけ**
この文面・長い出力の許容に切り替える。それ以外 (本番の s23i) は従来の DB 版 (190 字以内) のまま。

- 文面は DB を介さず ``config/prompts/pir_daily_focus_rubric_v6_2.yaml`` を直接合成する
  (DB の ``pir_daily_focus_rubric`` は書き換えない = 切替・戻しがモデル割当だけで済む)。
- ``PIR_FOCUS_PROMPT_V6=0`` で s24c でも従来の文面へ戻す (緊急の切り分け用)。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import jinja2
import yaml

from src.logging_config import get_logger

_log = get_logger(__name__)

FLAG_ENV = "PIR_FOCUS_PROMPT_V6"

#: v6.2 で学習した系統 (前方一致。補間タグ ``kuebiko-sft:s24ci50`` 等も含む)
V6_MODEL_FAMILIES: tuple[str, ...] = ("kuebiko-sft:s24c",)

RUBRIC_PATH = Path("config/prompts/pir_daily_focus_rubric_v6_2.yaml")

#: v6.2 の出力は 470 字前後 (最大 600 字弱、c3 80 件の試験) で、従来の 256 トークンでは切れる。
#: 暴走の上限も兼ねる (日本語 1 字 ≈ 1-1.5 トークン、600 字でも 900 を超えない)。
MAX_TOKENS = 1536

#: 「この記事は SIR に当たらない」という明示の文 (教師の ``NA_MARKER`` と同じ語)
_NOT_APPLICABLE = re.compile(r"該当しない|当たらない|関係しない|無関係")

#: 本文を包んだ JSON のうち本文が入る欄 (s24b の v6 試験で実際に出た形)
_TEXT_KEYS: tuple[str, ...] = ("summary", "pir_focus", "article_summary", "focus")


def is_enabled(model: str) -> bool:
    """この呼び出しで v6.2 を使うか。flag が明示されればそれが優先、無ければ s24c 系のみ。"""
    raw = os.environ.get(FLAG_ENV, "").strip()
    if raw == "0":
        return False
    if raw == "1":
        return True
    return str(model).strip().startswith(V6_MODEL_FAMILIES)


@lru_cache(maxsize=1)
def load_template() -> jinja2.Template | None:
    """v6.2 の合成テンプレート (失敗したら None = 呼び出し側は従来の文面へ)。"""
    from src.prompts.prompt_store import build_prompt_environment, compose_prompt_source
    from src.prompts.registry import get_spec
    from src.prompts.rubric_model import SummarizerRubric

    spec = get_spec("pir_daily_focus")
    if spec is None:
        return None
    try:
        raw: Any = yaml.safe_load(RUBRIC_PATH.read_text(encoding="utf-8"))
        rubric = SummarizerRubric.model_validate(raw)
        source = compose_prompt_source(spec, rubric)
        if source is None:
            return None
        return build_prompt_environment(spec, spec.legacy_path).from_string(source)
    except Exception as e:  # noqa: BLE001  合成失敗でも朝ブリーフは従来の文面で走らせる
        _log.warning("pir_focus_v6_template_failed", error_type=type(e).__name__)
        return None


def build_prompt(pir: Any, matches: list[Any]) -> str | None:
    """v6.2 のプロンプト (テンプレートが作れなければ None)。"""
    template = load_template()
    if template is None:
        return None
    return template.render(pir=pir, matches=matches)


def unwrap_lenient(text: str) -> str:
    """本文を ``{"article_id": …, "summary": "本文"}`` のように包んだ JSON から本文を取り出す。

    s24b は v6 の文面で要約欄のスキーマに引きずられ、複数キーの JSON で答えることがあった
    (c3 80 件中 10 件)。既知の本文キーの値が非空の文字列なら中身を返す。それ以外はそのまま。
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    if not isinstance(obj, dict):
        return text
    for key in _TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return text


def is_not_applicable(sentence: str) -> bool:
    """「本 SIR には該当しない」の明示文か。"""
    return bool(_NOT_APPLICABLE.search(sentence))
