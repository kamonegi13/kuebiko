"""block 方式プロンプトの store (DB SSoT + seed yaml + cache + fail-safe)。

``rubric_store`` (summarizer 専用、1 本目) と同じ責務の **spec 汎用版**。プロンプトを
追加するときは ``registry.py`` に PromptSpec を足すだけで、この store / API / 週次統治
ジョブが追従する。summarizer は実証済みの専用経路 (rubric_store) を凍結して使い続け、
legacy .j2 撤去 (観察 1 か月後) のタイミングでこちらへ合流させる。

失敗時の作法は rubric_store と同一: env flag / DB 障害 / 壊れた値 / 合成失敗はすべて
None に倒して呼び出し側が legacy .j2 を使う。**黙って倒さない** — 理由を WARNING と
``fallback_reason()`` に残す。ログにプロンプト本文を出さない (§4)。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2
import yaml

from src.logging_config import get_logger
from src.prompts.block_composer import compose_blocks, validate_blocks
from src.prompts.registry import PromptSpec
from src.prompts.rubric_model import SummarizerRubric
from src.prompts.summarizer_composer import build_environment as _dispatch_environment

_log = get_logger(__name__)


@dataclass
class _State:
    rubric: SummarizerRubric | None = None
    template: jinja2.Template | None = None
    template_key: str = ""
    fallback_reason: str = ""
    skeleton: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_STATES: dict[str, _State] = {}


def _state(spec_id: str) -> _State:
    return _STATES.setdefault(spec_id, _State())


def invalidate_prompt_cache(spec_id: str | None = None) -> None:
    """process cache を落とす (保存・revert 後、tests)。None = 全プロンプト。"""
    if spec_id is None:
        _STATES.clear()
    else:
        _STATES.pop(spec_id, None)


def fallback_reason(spec_id: str) -> str:
    """直近に legacy へ倒れた理由 (空文字 = 合成が使われている)。"""
    return _state(spec_id).fallback_reason


def is_prompt_composer_enabled(spec: PromptSpec) -> bool:
    """合成を使うか (既定 ON)。``<ENV_FLAG>=0`` で legacy .j2 へ即時 rollback。"""
    return os.environ.get(spec.env_flag, "1") != "0"


def _validate_value(spec: PromptSpec, value: Any, *, source: str) -> SummarizerRubric | None:
    if not isinstance(value, dict):
        _log.warning(
            "prompt_rubric_value_invalid",
            prompt=spec.prompt_id,
            source=source,
            reason="dict ではありません",
        )
        return None
    try:
        return SummarizerRubric.model_validate(value)
    except Exception as e:  # noqa: BLE001  ValidationError は本文を含むため型名のみ
        _log.warning(
            "prompt_rubric_value_invalid",
            prompt=spec.prompt_id,
            source=source,
            reason="スキーマ検証に失敗",
            error_type=type(e).__name__,
        )
        return None


def load_seed_rubric(spec: PromptSpec) -> SummarizerRubric | None:
    """seed yaml を読む (存在しない / 壊れていれば None、fail-safe)。"""
    try:
        raw: Any = yaml.safe_load(spec.seed_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _log.warning(
            "prompt_rubric_seed_read_failed",
            prompt=spec.prompt_id,
            path=str(spec.seed_path),
            error_type=type(e).__name__,
        )
        return None
    return _validate_value(spec, raw, source="seed")


def _load_from_db(spec: PromptSpec) -> SummarizerRubric | None:
    try:
        from src.storage.config_store import get_config

        value = get_config(spec.config_key)
    except Exception as e:  # noqa: BLE001  DB 障害でも legacy 運用は継続させる
        _log.warning("prompt_rubric_db_read_failed", prompt=spec.prompt_id, error=str(e))
        return None
    if value is None:
        return None
    return _validate_value(spec, value, source="db")


def load_prompt_rubric(spec: PromptSpec) -> SummarizerRubric | None:
    """現在の編集層。DB → seed yaml → None の順に解決する。"""
    state = _state(spec.prompt_id)
    if state.rubric is not None:
        return state.rubric
    rubric = _load_from_db(spec) or load_seed_rubric(spec)
    if rubric is not None:
        state.rubric = rubric
    return rubric


def seed_prompt_if_absent(spec: PromptSpec) -> bool:
    """DB 未投入なら seed yaml を version 1 として取り込む (起動時、full instance)。"""
    rubric = load_seed_rubric(spec)
    if rubric is None:
        _log.warning("prompt_rubric_seed_missing", prompt=spec.prompt_id, path=str(spec.seed_path))
        return False
    try:
        from src.storage.config_store import seed_config_if_absent

        seeded = seed_config_if_absent(
            spec.config_key,
            rubric.model_dump(),
            note=f"初期 seed ({spec.seed_path.name})",
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("prompt_rubric_seed_failed", prompt=spec.prompt_id, error=str(e))
        return False
    if seeded:
        invalidate_prompt_cache(spec.prompt_id)
    return seeded


def load_skeleton(spec: PromptSpec) -> str | None:
    """skeleton (code 所有・静的) を読む。プロセス内 1 回だけ。"""
    state = _state(spec.prompt_id)
    if state.skeleton is not None:
        return state.skeleton
    if spec.skeleton_path is None:
        return None
    try:
        state.skeleton = spec.skeleton_path.read_text(encoding="utf-8")
    except OSError as e:
        _log.warning(
            "prompt_skeleton_read_failed",
            prompt=spec.prompt_id,
            path=str(spec.skeleton_path),
            error_type=type(e).__name__,
        )
        return None
    return state.skeleton


def build_prompt_environment(spec: PromptSpec, path: Path) -> jinja2.Environment:
    """消費側と同一構成の Environment (差分ゼロが要件 — env_style で分岐)。"""
    if spec.env_style == "none":
        # python_block kind は Jinja を一切使わない (候補リストは code が f-string で組む)。
        # 呼び間違い (kind 分岐漏れ) を早期に落とすためのガード。
        raise ValueError(
            f"env_style=none (python_block) は Jinja Environment を使いません: {spec.prompt_id}"
        )
    if spec.env_style == "dispatch":
        return _dispatch_environment(path)
    prompts_root = path.parent.parent if path.parent.name != "prompts" else path.parent
    if spec.env_style == "grounded":
        # grounded: src/synthesis/grounded/passes.py._render の env と完全一致。
        # synthesis との唯一の差分は StrictUndefined あり (未定義変数を沈黙させない) と
        # keep_trailing_newline 無し。
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(prompts_root)),
            autoescape=False,  # noqa: S701  プロンプトはテキストでありエスケープしない
            undefined=jinja2.StrictUndefined,
        )
    # synthesis: src/synthesis/generator.py の env と完全一致 (StrictUndefined 無し)
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(prompts_root)),
        autoescape=False,  # noqa: S701  プロンプトはテキストでありエスケープしない
        keep_trailing_newline=True,
    )


def compose_prompt_source(spec: PromptSpec, rubric: SummarizerRubric) -> str | None:
    """合成原文 (Jinja タグ未展開)。block 検証に失敗したら None。"""
    skeleton = load_skeleton(spec)
    if skeleton is None:
        return None
    errors = validate_blocks(skeleton, rubric)
    if errors:
        _log.warning("prompt_blocks_invalid", prompt=spec.prompt_id, errors=len(errors))
        return None
    return compose_blocks(skeleton, rubric)


def validate_block_rubric(spec: PromptSpec, rubric: SummarizerRubric) -> list[str]:
    """保存前検証: slot/block 1:1 → Jinja 構文 → サンプル context での render (最後の砦)。

    エラー文の list を返す (空 = 保存可)。render まで通すのは summarizer WP2 の教訓
    (「検証は preview、保存は PUT」と分けると検証だけが preview に残る事故が起きる)。
    """
    if spec.kind == "python_block":
        return validate_python_block_rubric(spec, rubric)
    skeleton = load_skeleton(spec)
    if skeleton is None:
        return ["skeleton が読めません (code 側の欠陥 — 保存不可)"]
    errors = validate_blocks(skeleton, rubric)
    if errors:
        return errors
    source = compose_blocks(skeleton, rubric)
    try:
        template = build_prompt_environment(spec, spec.legacy_path).from_string(source)
    except Exception as e:  # noqa: BLE001 — 構文エラーは行番号だけ拾い本文は出さない
        lineno = getattr(e, "lineno", None)
        suffix = f" (行 {lineno})" if lineno else ""
        return [f"Jinja 構文エラー: {type(e).__name__}{suffix}"]
    from src.prompts.sample_context import sample_context_for

    context = sample_context_for(spec.prompt_id)
    if context is None:
        return [f"サンプル context 未定義: {spec.prompt_id} (sample_context.py に追加が必要)"]
    try:
        template.render(**context)
    except Exception as e:  # noqa: BLE001
        return [f"サンプル context での render に失敗: {type(e).__name__}"]
    return []


def _fallback(spec: PromptSpec, reason: str, **fields: object) -> None:
    _state(spec.prompt_id).fallback_reason = reason
    _log.warning("prompt_composer_fallback", prompt=spec.prompt_id, reason=reason, **fields)


def build_prompt_template(spec: PromptSpec, path: Path) -> jinja2.Template | None:
    """合成テンプレート。flag OFF / rubric 不在 / 検証・合成失敗なら None (= legacy へ)。"""
    state = _state(spec.prompt_id)
    if not is_prompt_composer_enabled(spec):
        state.fallback_reason = f"{spec.env_flag}=0 (legacy .j2 を使用)"
        return None
    key = str(path)
    if state.template is not None and state.template_key == key:
        return state.template
    rubric = load_prompt_rubric(spec)
    if rubric is None:
        _fallback(spec, "編集層が DB にも seed yaml にも見つかりません", path=key)
        return None
    source = compose_prompt_source(spec, rubric)
    if source is None:
        _fallback(spec, "skeleton / block の検証に失敗しました", path=key)
        return None
    try:
        template = build_prompt_environment(spec, path).from_string(source)
    except Exception as e:  # noqa: BLE001  合成失敗でも本番は legacy で走り続ける
        _fallback(spec, "テンプレートの構文が不正です", error_type=type(e).__name__)
        return None
    state.template = template
    state.template_key = key
    state.fallback_reason = ""
    return template


# ---------- python_block kind (skeleton を持たない Python 所有プロンプト) ----------
#
# group 4 (ioc_llm_verifier、2026-08-21) 以降のための対象モジュール dispatch。合成の実体
# (blocks + 動的データ → プロンプト文字列) は対象モジュールが持ち、ここは prompt_id で
# そこへ委譲するだけ。1 本目のみなので小さな表に留める (YAGNI — 複数本になったら
# Protocol 化を検討。他の kind と違い skeleton ファイルが無いため slot 定義も対象
# モジュール側の定数 (SLOT_IDS) から取る)。

_PythonBlockComposeFn = Callable[[dict[str, str], list[dict[str, str]]], str]


def _python_block_compose_fn(spec: PromptSpec) -> _PythonBlockComposeFn | None:
    """python_block kind の合成関数を prompt_id で解決する (対象モジュールへの委譲)。"""
    if spec.prompt_id == "ioc_llm_verifier":
        from src.cti.ioc_llm_verifier import compose_from_blocks

        return compose_from_blocks
    return None


def python_block_slot_ids(spec: PromptSpec) -> tuple[str, ...]:
    """python_block kind の slot id (skeleton が無いため対象モジュールの固定表から取る)。"""
    if spec.prompt_id == "ioc_llm_verifier":
        from src.cti.ioc_llm_verifier import SLOT_IDS

        return SLOT_IDS
    return ()


def python_block_sample_targets(spec: PromptSpec) -> list[dict[str, str]]:
    """保存前検証・preview 用のサンプル候補 (対象モジュールが持つ非空ダミー、各 type 1 件)。"""
    if spec.prompt_id == "ioc_llm_verifier":
        from src.cti.ioc_llm_verifier import SAMPLE_TARGETS

        # 共有参照を返すと呼び手の変異が全 preview/検証を汚染する — 都度コピー (イミュータブル規約)
        return [dict(t) for t in SAMPLE_TARGETS]
    return []


def compose_python_block(
    spec: PromptSpec, rubric: SummarizerRubric, targets: list[dict[str, str]]
) -> str:
    """python_block kind の合成 (pure)。合成関数未登録 / 実行時例外は呼び出し側が握る。"""
    compose_fn = _python_block_compose_fn(spec)
    if compose_fn is None:
        raise ValueError(f"python_block の合成関数が未登録です: {spec.prompt_id}")
    blocks = {s.field_id: s.body for s in rubric.sections}
    return compose_fn(blocks, targets)


def validate_python_block_rubric(spec: PromptSpec, rubric: SummarizerRubric) -> list[str]:
    """python_block kind の保存前検証: slot/block 1:1 → サンプル候補での合成 (最後の砦)。"""
    slots = python_block_slot_ids(spec)
    if not slots:
        return [f"python_block の slot 定義が見つかりません (code 側の欠陥): {spec.prompt_id}"]
    slot_set = set(slots)
    block_ids = [s.field_id for s in rubric.sections]
    errors: list[str] = []
    for duplicate in {b for b in block_ids if block_ids.count(b) > 1}:
        errors.append(f"block id が重複しています: {duplicate}")
    for missing in [s for s in slots if s not in set(block_ids)]:
        errors.append(f"slot に対応する block がありません: {missing}")
    for unknown in [b for b in block_ids if b not in slot_set]:
        errors.append(f"未知の block です (書いても出力されません): {unknown}")
    if rubric.examples:
        errors.append("python_block 方式は examples を使いません (出力例は該当 block の本文へ)")
    if errors:
        return errors
    try:
        composed = compose_python_block(spec, rubric, python_block_sample_targets(spec))
    except Exception as e:  # noqa: BLE001 — 種類を問わず保存を拒否する
        return [f"サンプル候補での合成に失敗しました: {type(e).__name__}"]
    if not composed.strip():
        return ["合成結果が空文字列です"]
    return []


def build_python_block_source(spec: PromptSpec, targets: list[dict[str, str]]) -> str | None:
    """python_block kind の runtime 合成 (fail-safe)。flag OFF / rubric 不在 / 合成失敗は None。

    対象モジュールはこの結果が None なら legacy 経路 (frozen 関数) へ倒す (rollback 実体)。
    """
    state = _state(spec.prompt_id)
    if not is_prompt_composer_enabled(spec):
        state.fallback_reason = f"{spec.env_flag}=0 (legacy を使用)"
        return None
    rubric = load_prompt_rubric(spec)
    if rubric is None:
        _fallback(spec, "編集層が DB にも seed yaml にも見つかりません")
        return None
    try:
        result = compose_python_block(spec, rubric, targets)
    except Exception as e:  # noqa: BLE001  合成失敗でも本番は legacy で走り続ける
        _fallback(spec, "python 合成に失敗しました", error_type=type(e).__name__)
        return None
    if not result.strip():
        _fallback(spec, "python 合成結果が空です")
        return None
    state.fallback_reason = ""
    return result
