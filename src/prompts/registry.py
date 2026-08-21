"""管理対象プロンプトのレジストリ (2026-08-20、層分けの一般化)。

summarizer (1 本目、2026-08-16) で確立した「編集層を DB (config_store) に置き、
UI 編集 + 版履歴 + 保存前 dry-run + 切替検証で運用する」方式を、他プロンプトへ
横展開するための SSoT。**プロンプトを 1 本追加する = ここに PromptSpec を 1 つ足す**
(store / API / UI / 週次統治ジョブはレジストリを読むだけで新プロンプトに追従する)。

合成方式は 3 種:

- ``field_rubric`` (summarizer): 出力スキーマのフィールドごとに判定基準を持ち、
  code 所有の骨格 (persona / 記事変数) と合成する。契約は出力スキーマが SSoT。
- ``block``: narrative 系 (.j2 が Jinja データ注入と指示散文の混在)。**skeleton
  (code 所有 — データ注入とマーカー行) + blocks (DB 所有 — 指示散文)** をマーカー
  置換で連結する。**seed 合成 = legacy .j2 と byte 一致**が golden 不変量
  (summarizer の期待差分契約より強くて単純 — 逸脱は UI 編集でのみ生まれる)。
- ``python_block`` (ioc_llm_verifier / judgment_classifier / article_triage、group 4、
  2026-08-21、全 3 本で完遂): .j2 を持たず **Python コードが構造を所有する動的プロンプト**
  (候補リストや区切り情報が実行時に決まり Jinja ループで表現しない設計判断)。skeleton の
  代わりに対象モジュールが blocks (DB 所有の指示散文) を受け取る合成関数を持ち、
  ``src/prompts/prompt_store.py`` が prompt_id でそこへ dispatch する (3 本とも小さな
  if 分岐表で足りている、Protocol 化は次の追加時に再検討)。``skeleton_path=None`` /
  ``env_style="none"`` (Jinja を一切使わない)。golden 不変量は block 方式と同じ「seed の
  blocks で合成した結果が legacy 関数の出力と byte 一致」。動的データの形は対象モジュール
  ごとに異なってよい (ioc_llm_verifier は候補 list、judgment_classifier は ``.format()``
  context の dict、article_triage は直接組み立て用の dict — 生 brace ``{high, medium, low}``
  を含む block をそのまま扱えるのが直接組み立て方式を選ぶ理由) — dispatch 層は ``Any`` で
  中継し、各対象モジュールの合成関数が自分の形として解釈する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ComposerKind = Literal["field_rubric", "block", "python_block"]
# 消費側 Environment との一致が要件 (StrictUndefined の有無・keep_trailing_newline の有無が違う)。
# dispatch = briefing 系 (StrictUndefined) / synthesis = generator 系 (寛容 Undefined) /
# grounded = src/synthesis/grounded/passes.py._render 系 (StrictUndefined あり・
# keep_trailing_newline 無し — synthesis との唯一の差分。層分け 7〜12 本目 2026-08-20) /
# none = kind="python_block" 専用 (Jinja を使わないため Environment を構築しない)。
EnvStyle = Literal["dispatch", "synthesis", "grounded", "none"]


@dataclass(frozen=True)
class PromptSpec:
    """管理対象プロンプト 1 本の宣言。"""

    prompt_id: str  # API path / UI / ログの識別子 (snake_case)
    title: str  # UI 表示名 (日本語、括弧 id は付けない — UI が prompt_id を副行に出す)
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
        title="記事要約・翻訳",
        config_key="summarizer_rubric",
        env_flag="SUMMARIZER_COMPOSER",
        seed_path=Path("config/prompts/summarizer_rubric.yaml"),
        legacy_path=Path("prompts/briefing/summarizer.j2"),
        kind="field_rubric",
        env_style="dispatch",
    ),
    PromptSpec(
        prompt_id="status_synthesis",
        title="状況総括",
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
        title="週次リキャップ",
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
        title="PIR Daily Focus",
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
        title="深掘り選定ルーブリック",
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
        title="PIR Spotlight",
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
        title="証拠接地 + ACH 初回",
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
        title="証拠接地 増分",
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
        title="情勢候補の指名",
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
        title="新規情勢の検出",
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
        title="対称 adversarial 検証",
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
        title="narrative 射影",
        config_key="synthesis_render_rubric",
        env_flag="SYNTHESIS_RENDER_COMPOSER",
        seed_path=Path("config/prompts/synthesis_render_rubric.yaml"),
        legacy_path=Path("prompts/synthesis/render.j2"),
        kind="block",
        env_style="grounded",
        skeleton_path=Path("prompts/synthesis/render_skeleton.j2"),
    ),
    # ---- group 4: Python 所有プロンプト (1〜3 本目、2026-08-21、全 3 本で完遂) ----
    # .j2 を持たない動的プロンプト。legacy_path は rollback 用の frozen 関数
    # (_build_prompt_legacy) を持つ実ファイルを指す (UI の「実ファイル」表示・統治レポート用
    # のラベルにすぎず、Web からの直接編集は許可しない — allowlist は FileEditor 側の
    # ``prompts/**/*.j2`` 固定で、.py はそもそも対象外なので構造的に安全)。
    PromptSpec(
        prompt_id="ioc_llm_verifier",
        title="IoC LLM 検証",
        config_key="ioc_llm_verifier_rubric",
        env_flag="IOC_LLM_VERIFIER_COMPOSER",
        seed_path=Path("config/prompts/ioc_llm_verifier_rubric.yaml"),
        legacy_path=Path("src/cti/ioc_llm_verifier.py"),
        kind="python_block",
        env_style="none",
        skeleton_path=None,
    ),
    PromptSpec(
        prompt_id="judgment_classifier",
        title="統合判断分類器",
        config_key="judgment_rubric",
        env_flag="JUDGMENT_COMPOSER",
        seed_path=Path("config/prompts/judgment_rubric.yaml"),
        legacy_path=Path("src/cti/judgment_classifier.py"),
        kind="python_block",
        env_style="none",
        skeleton_path=None,
    ),
    # group 4 の 3 本目 (最終、2026-08-21)。層分けは PIR-driven 経路のみ (high/medium 基準は
    # PIR レイヤが既に動的注入する編集可能面のため触らない)。legacy_path は
    # ``_build_prompt_pir_driven_legacy`` (rollback 用 frozen 実装) を持つ実ファイルを指す。
    PromptSpec(
        prompt_id="article_triage",
        title="記事 triage (重要度判定)",
        config_key="triage_rubric",
        env_flag="TRIAGE_COMPOSER",
        seed_path=Path("config/prompts/triage_rubric.yaml"),
        legacy_path=Path("src/tools/article_triage.py"),
        kind="python_block",
        env_style="none",
        skeleton_path=None,
    ),
)


def all_specs() -> tuple[PromptSpec, ...]:
    return _SPECS


def get_spec(prompt_id: str) -> PromptSpec | None:
    for spec in _SPECS:
        if spec.prompt_id == prompt_id:
            return spec
    return None
