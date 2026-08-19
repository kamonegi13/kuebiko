"""管理対象プロンプトのレジストリ (2026-08-20、層分けの一般化)。

summarizer (1 本目、2026-08-16) で確立した「編集層を DB (config_store) に置き、
UI 編集 + 版履歴 + 保存前 dry-run + 切替検証で運用する」方式を、他プロンプトへ
横展開するための SSoT。**プロンプトを 1 本追加する = ここに PromptSpec を 1 つ足す**
(store / API / UI / 週次統治ジョブはレジストリを読むだけで新プロンプトに追従する)。

合成方式は 2 種:

- ``field_rubric`` (summarizer): 出力スキーマのフィールドごとに判定基準を持ち、
  code 所有の骨格 (persona / 記事変数) と合成する。契約は出力スキーマが SSoT。
- ``block``: narrative 系 (.j2 が Jinja データ注入と指示散文の混在)。**skeleton
  (code 所有 — データ注入とマーカー行) + blocks (DB 所有 — 指示散文)** をマーカー
  置換で連結する。**seed 合成 = legacy .j2 と byte 一致**が golden 不変量
  (summarizer の期待差分契約より強くて単純 — 逸脱は UI 編集でのみ生まれる)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ComposerKind = Literal["field_rubric", "block"]
# 消費側 Environment との一致が要件 (StrictUndefined の有無・keep_trailing_newline の有無が違う)。
# dispatch = briefing 系 (StrictUndefined) / synthesis = generator 系 (寛容 Undefined) /
# grounded = src/synthesis/grounded/passes.py._render 系 (StrictUndefined あり・
# keep_trailing_newline 無し — synthesis との唯一の差分。層分け 7〜12 本目 2026-08-20)。
EnvStyle = Literal["dispatch", "synthesis", "grounded"]


@dataclass(frozen=True)
class PromptSpec:
    """管理対象プロンプト 1 本の宣言。"""

    prompt_id: str  # API path / UI / ログの識別子 (snake_case)
    title: str  # UI 表示名 (日本語)
    config_key: str  # config_store のキー (版履歴の単位)
    env_flag: str  # =0 で legacy .j2 に即時 rollback する env 変数
    seed_path: Path  # 初回 seed 専用 yaml (git 管理)
    legacy_path: Path  # rollback 用に据え置く legacy .j2
    kind: ComposerKind
    env_style: EnvStyle
    skeleton_path: Path | None = None  # kind="block" のみ: マーカー入り骨格 (code 所有)


_SPECS: tuple[PromptSpec, ...] = (
    PromptSpec(
        prompt_id="summarizer",
        title="記事要約・翻訳 (summarizer)",
        config_key="summarizer_rubric",
        env_flag="SUMMARIZER_COMPOSER",
        seed_path=Path("config/prompts/summarizer_rubric.yaml"),
        legacy_path=Path("prompts/briefing/summarizer.j2"),
        kind="field_rubric",
        env_style="dispatch",
    ),
    PromptSpec(
        prompt_id="status_synthesis",
        title="状況総括 (status_synthesis)",
        config_key="status_synthesis_rubric",
        env_flag="SYNTHESIS_COMPOSER",
        seed_path=Path("config/prompts/status_synthesis_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/status_synthesis.j2"),
        kind="block",
        env_style="synthesis",
        skeleton_path=Path("prompts/synthesis/status_synthesis_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="weekly_recap",
        title="週次リキャップ (weekly_recap)",
        config_key="weekly_recap_rubric",
        env_flag="WEEKLY_RECAP_COMPOSER",
        seed_path=Path("config/prompts/weekly_recap_rubric.yaml"),
        legacy_path=Path("prompts/digest/weekly_recap.j2"),
        kind="block",
        env_style="synthesis",
        skeleton_path=Path("prompts/digest/weekly_recap_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="pir_daily_focus",
        title="PIR Daily Focus (pir_daily_focus)",
        config_key="pir_daily_focus_rubric",
        env_flag="PIR_DAILY_FOCUS_COMPOSER",
        seed_path=Path("config/prompts/pir_daily_focus_rubric.yaml"),
        legacy_path=Path("prompts/digest/pir_daily_focus.j2"),
        kind="block",
        env_style="synthesis",
        skeleton_path=Path("prompts/digest/pir_daily_focus_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="deep_dive_rubric",
        title="深掘り選定ルーブリック (deep_dive_rubric)",
        config_key="deep_dive_rubric",
        env_flag="DEEP_DIVE_RUBRIC_COMPOSER",
        seed_path=Path("config/prompts/deep_dive_rubric.yaml"),
        legacy_path=Path("prompts/digest/deep_dive_rubric.j2"),
        kind="block",
        env_style="synthesis",
        skeleton_path=Path("prompts/digest/deep_dive_rubric_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="pir_spotlight",
        title="PIR Spotlight (pir_spotlight)",
        config_key="pir_spotlight_rubric",
        env_flag="PIR_SPOTLIGHT_COMPOSER",
        seed_path=Path("config/prompts/pir_spotlight_rubric.yaml"),
        legacy_path=Path("prompts/spotlight/pir_spotlight.j2"),
        kind="block",
        env_style="synthesis",
        skeleton_path=Path("prompts/spotlight/pir_spotlight_skeleton.j2"),
    ),
    # ---- grounded ACH 群 (7〜12 本目、2026-08-20) ----
    # 分析の核 (ACH の対称性・fail-closed・アンカリング禁止)。blocks は「編集して安全な
    # 指示散文」単位で切り、仮説 id / 証拠注入の Jinja ループは skeleton (code 所有) に残す。
    PromptSpec(
        prompt_id="ground_ach",
        title="証拠接地 + ACH 初回 (ground_ach)",
        config_key="ground_ach_rubric",
        env_flag="GROUND_ACH_COMPOSER",
        seed_path=Path("config/prompts/ground_ach_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/ground_ach.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/ground_ach_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="ground_incremental",
        title="証拠接地 増分 (ground_incremental)",
        config_key="ground_incremental_rubric",
        env_flag="GROUND_INCREMENTAL_COMPOSER",
        seed_path=Path("config/prompts/ground_incremental_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/ground_incremental.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/ground_incremental_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="nominate",
        title="情勢候補の指名 (nominate)",
        config_key="nominate_rubric",
        env_flag="NOMINATE_COMPOSER",
        seed_path=Path("config/prompts/nominate_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/nominate.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/nominate_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="detect_new",
        title="新規情勢の検出 (detect_new)",
        config_key="detect_new_rubric",
        env_flag="DETECT_NEW_COMPOSER",
        seed_path=Path("config/prompts/detect_new_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/detect_new.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/detect_new_skeleton.j2"),
    ),
    PromptSpec(
        prompt_id="adversarial",
        title="対称 adversarial 検証 (adversarial)",
        config_key="adversarial_rubric",
        env_flag="ADVERSARIAL_COMPOSER",
        seed_path=Path("config/prompts/adversarial_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/adversarial.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/adversarial_skeleton.j2"),
    ),
    # prompt_id/config_key/env_flag は "render" を避ける (一般語で API path として不明瞭)。
    PromptSpec(
        prompt_id="synthesis_render",
        title="narrative 射影 (synthesis_render)",
        config_key="synthesis_render_rubric",
        env_flag="SYNTHESIS_RENDER_COMPOSER",
        seed_path=Path("config/prompts/synthesis_render_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/render.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/render_skeleton.j2"),
    ),
)


def all_specs() -> tuple[PromptSpec, ...]:
    return _SPECS


def get_spec(prompt_id: str) -> PromptSpec | None:
    for spec in _SPECS:
        if spec.prompt_id == prompt_id:
            return spec
    return None
