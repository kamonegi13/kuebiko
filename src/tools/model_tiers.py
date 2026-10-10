"""LLM モデルの能力ティア (capability tier) 割当と解決。

本質: **ツールが「step → 能力ティア」を決め (このモジュールが所有)、ユーザは
「ティア → 実モデル」を決める (config_store DB, UI 編集可)**。この分業により、
per-article 要約が 26B・synthesis narrative が 31B といった step ごとの生スロット参照
(旧 ``ollama_main_model`` / ``ollama_synthesis_model`` の散在) を1箇所に集約する。

ティアは5つ:
- ``reasoning``  : 構造化分析       — 台帳 ACH (nominate/採点/増分/adversarial/射影)。
                                       think 常時 OFF (2026-07-24 narrative から分離)
- ``narrative``  : 散文生成         — synthesis 総括 / PIR spotlight。外部モデル割当時は
                                       think 設定可 (narrative_think: auto/off、A/B 2026-07-24)
- ``fast``       : MoE/軽量/速い    — per-article要約, triage/detect, 抽出, deep-dive,
                                       PIR focus, actor訳 (収集系バッチ、呼出 ~2500 回/日)
- ``dialog``     : 対話系           — 分析チャット, LLM支援検索, PIR compile, selector提案
                                       (user-facing・低頻度。2026-07-19 新設)
- ``embedding``  : 埋込             — 意味的重複排除・検索

3ティアで十分かの検討 (2026-07-08): 当時の棚卸しでは能力クラスが3つに収束し、4ティア案は
「分離要求の実シグナルが無い」ため不採用 (YAGNI) とした。ただし ``STEP_REGISTRY`` は
enum 1行 + step 再マップ数行で4ティア化できる構造として予約。
→ **2026-07-19 に実シグナルが発生**: 外部 LLM 開放により「対話系だけ外部 (Claude) に、
収集系バッチはローカルのまま」という割当が実需になった (fast 全体の外部化は月 200-300M
入力トークンでコスト不成立、対話系のみなら実測 ~1% 未満)。予約どおり ``dialog`` を新設。

timeout の所在 (本質): 同じ fast(26B) でも detect-new=900s / PIR focus=120s / 検索=180s と
**入力量で変わる** → timeout は **step 属性** (``STEP_REGISTRY`` が所有)。ティアが所有するのは
``{model, num_ctx}`` のみ。pipeline wallclock timeout は別レイヤ (pipeline_runner)。

whitelist の位置づけ: 中華系 denylist は **コード所有のセキュリティ基盤** (CLAUDE.md §4)。config 化
すると防御自体を無効化できてしまうため、per-tier のモデル**選択のみ** config・選択は許可集合に
制約する (3層防御: UI dropdown 除外 / 保存時検証 / 構築時 ``validate_model_name``)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from src.logging_config import get_logger
from src.storage.db_backend import raise_if_infrastructure

if TYPE_CHECKING:
    from src.config_loader import AppConfig
    from src.tools.llm_client import LLMClient, UsageRecorder

_log = get_logger(__name__)

# config_store の key。DB (app_config_versions) を runtime SSoT とする。
MODEL_TIERS_CONFIG_KEY = "model_tiers"

# 外部 LLM (2026-07-18 §4 改訂): ティア割当が ``anthropic:<model>`` のとき Anthropic
# Messages API を使う。prefix 無しは従来通りローカル Ollama モデル名。既定 (BUILTIN) は
# ローカルのまま — 外部送信は利用者がモデルティア画面で明示割当した場合のみ発生する。
ANTHROPIC_MODEL_PREFIX = "anthropic:"

# UI dropdown に出す外部モデルの curated 選択肢 (ANTHROPIC_API_KEY 設定時のみ表示)。
# 中華系 denylist はプロバイダ横断で適用される (validate_model_name が最終防御)。
ANTHROPIC_MODEL_CHOICES: tuple[str, ...] = (
    f"{ANTHROPIC_MODEL_PREFIX}claude-sonnet-5",
    f"{ANTHROPIC_MODEL_PREFIX}claude-haiku-4-5",
    f"{ANTHROPIC_MODEL_PREFIX}claude-opus-4-8",
)

# Claude Code サブスク経由 (2026-07-19): ホスト側 bridge (scripts/claude_code_bridge.py)
# を介して claude -p で推論する。API クレジット不要 (Pro/Max のサブスク枠を消費)。
# UI には bridge 疎通時のみ表示。レート上限があるため低頻度ティア (reasoning/dialog) 向け。
CLAUDECODE_MODEL_PREFIX = "claudecode:"
CLAUDE_CODE_MODEL_CHOICES: tuple[str, ...] = (
    f"{CLAUDECODE_MODEL_PREFIX}sonnet",
    f"{CLAUDECODE_MODEL_PREFIX}haiku",
    f"{CLAUDECODE_MODEL_PREFIX}opus",
)

# 外部プロバイダ prefix の一覧 (埋込ティア拒否などの横断検証に使う)
_EXTERNAL_PREFIXES: tuple[str, ...] = (ANTHROPIC_MODEL_PREFIX, CLAUDECODE_MODEL_PREFIX)


def is_external_model(model: str) -> bool:
    """model 名が外部プロバイダ解決かを判定する (narrative の think 方針切替等に使う)。

    組み込み prefix (anthropic:/claudecode:) に加え、ユーザ定義接続先
    (llm_endpoints レジストリ) の prefix も外部扱い。FallbackLLMClient の
    composite 名 ("claudecode:sonnet→gemma4:31b") も外部扱いで正しい —
    fallback ローカルアームは think を常時 False にクランプするため。
    """
    if model.startswith(_EXTERNAL_PREFIXES):
        return True
    if ":" not in model:
        return False
    from src.tools.llm_endpoints import resolve_endpoint  # 遅延 import (起動コスト回避)

    return resolve_endpoint(model.split(":", 1)[0]) is not None


# rollback flag: 0 で DB を無視しレガシー env (ollama_*_model) から直接導出する。
_DB_FLAG_ENV = "MODEL_TIERS_CONFIG_DB"


class Tier(StrEnum):
    """能力ティア。ユーザが「ティア → 実モデル」を config で割り当てる単位。"""

    REASONING = "reasoning"
    NARRATIVE = "narrative"
    FAST = "fast"
    DIALOG = "dialog"
    EMBEDDING = "embedding"


class Step(StrEnum):
    """LLM を呼ぶ処理 step。``STEP_REGISTRY`` で ``{tier, timeout}`` に写像する。"""

    # --- fast tier (収集系バッチ) ---
    ARTICLE_SUMMARY = "article_summary"  # per-article 要約・翻訳 (RSS/Grok)
    # GROK_EXTRACT は撤去 (2026-09-04)。Grok は Phase 2.6a で LLM 抽出を廃し JSONL
    # パーサへ移行したため、この step を解決する呼出は 1 つも無かった。登録だけが残ると
    # 「ここを割り当てれば Grok の抽出モデルが変わる」という誤認を生む (config/agents.yaml
    # を撤去したのと同じ理由)。復活させるならスケルトンから書き直すこと。
    TRIAGE = "triage"  # 重要度 pre-filter (per-article。dispatch が本 step で解決する)
    # 地政学の救済 (2026-10-02): triage が地政学・軍事・外交を理由に落とした記事だけを
    # 同じ基準で判定し直す。**triage と別のモデルを割り当てるための step** — triage の
    # SFT モデルは「一般地政学 = low」を学習しており、同じモデルでは救えない (素の 26B を割当)
    TRIAGE_GEO_RESCUE = "triage_geo_rescue"
    PAIR_JUDGE = "pair_judge"  # 事象ペアの同一性判定 (R 型 = 関係判定。S 族 SFT の適用対象外)
    EVENT_KIND = "event_kind"  # 記事種別分類 (ML pair モデルの特徴量供給 — 供給元を固定する)
    # 深刻度の軸 (広がり・被害の性質・実害の確認・悪用状況・行為者・標的、2026-09-25)。
    # detect ML の特徴量供給。**ML は軸を付けたモデルの癖ごと学習する**ので供給元を固定する
    SEVERITY_AXES = "severity_axes"
    # weekly-recap 深掘りは LLM を 2 回呼ぶ。**課題の種類が違うので step を分ける**
    # (2026-09-20、借用禁止の衛生。detect で s17 が 3 日中 2 日 0 件開設 =
    #  判定特化モデルは選定で極端に保守的になると実測された直後に分離した):
    #   _SELECT = 60 件のプールから 12 件を選ぶ rubric 採点 (**判定**)
    #   本体     = 選ばれた 12 件から本文を書く (**生成**)
    DIGEST_DEEP_DIVE_SELECT = "digest_deep_dive_select"  # weekly-recap 選定 (rubric 採点)
    DIGEST_DEEP_DIVE = "digest_deep_dive"  # weekly-recap 本文 (narrative)
    SYNTHESIS_DETECT = "synthesis_detect"  # synthesis 内 detect-new (大量入力 triage)
    PIR_DAILY_FOCUS = "pir_daily_focus"  # PIR daily focus 要点
    PIR_LLM_JUDGE = "pir_llm_judge"  # 概念 PIR の主題判定 (夜間バッチ、候補ゲート通過分のみ)
    ACTOR_SYNC = "actor_sync"  # MITRE actor alias 和訳・提案
    # 日本関連性 ML カスケード (M4、2026-10-08、triage shadow 専用)。確信度が低い帯
    # (``src.cti.relevance_ml.in_uncertain_band``) の記事だけに「日本関与か」を聞く
    # 小さな構造化判定。triage 本体と別 step にする理由は EVENT_KIND と同じ (借用禁止の衛生) —
    # 将来ここだけ別モデルを割り当てたくなった時に triage の上書きを継承しない
    RELEVANCE_JP_CASCADE = "relevance_jp_cascade"
    # 記事本文の日本語全訳 (UI オンデマンド + 毎時バックログ)。2026-07-25 の haiku vs 26B
    # 品質比較でローカル 26B が同等以上と確定 → 翻訳系既存 step と同じ fast に置く
    # (dialog に置くと外部モデル割当時にバッチ翻訳が外部消費になるため)。
    ARTICLE_TRANSLATE = "article_translate"
    # --- dialog tier (user-facing 対話、2026-07-19 fast から分離) ---
    PIR_COMPILE = "pir_compile"  # PIR description → structured 抽出 (対話)
    SELECTOR_PROPOSAL = "selector_proposal"  # scraper CSS selector 提案 (対話)
    PRECISE_SEARCH = "precise_search"  # LLM 支援検索 precise mode (対話)
    ASSISTANT_CHAT = "assistant_chat"  # 分析チャット plan+answer (対話)
    # --- reasoning tier (構造化分析、think 常時 OFF) ---
    SYNTHESIS_ANALYSIS = "synthesis_analysis"  # 台帳 ACH: nominate/採点/増分/adversarial/射影
    # --- narrative tier (散文生成 + 夜間精査、think は外部モデル割当時に設定可) ---
    SYNTHESIS_NARRATIVE = "synthesis_narrative"  # status synthesis 散文 (legacy 経路)
    # 事象単位ニュース精製 (docs/event_news_design.md §9/§14b)。当初 fast(26B) に置いたが、
    # 同一入力・同一プロンプトの A/B で **26B は 5 事象中 3 件の日本語破損** (中国語・ラオ文字
    # の混入、「2,300 件」→「2,30 封」)、31B は 0 件。内容精度も 31B が上 (被害社の件数内訳、
    # 帰属の言語証拠まで拾う)。頻度は 0.25 回/時なので 65 秒/回でも毎時運用に収まる。
    EVENT_NEWS = "event_news"
    PIR_SPOTLIGHT = "pir_spotlight"  # PIR 縦断 narrative (本番 31b 踏襲)
    # --- embedding tier ---
    EMBED = "embed"  # 意味的重複排除・検索埋込


@dataclass(frozen=True)
class StepSpec:
    """step の割当。``tier`` がモデルを決め、``timeout_seconds`` はこの step 固有。"""

    tier: Tier
    timeout_seconds: float


# step → (tier, timeout)。散在していた 120/180/300/600/900 の magic number を集約。
# timeout は同一ティアでも入力量で異なる (step 属性であることの証左)。
STEP_REGISTRY: dict[Step, StepSpec] = {
    Step.ARTICLE_SUMMARY: StepSpec(Tier.FAST, 300.0),
    Step.TRIAGE: StepSpec(Tier.FAST, 300.0),
    Step.TRIAGE_GEO_RESCUE: StepSpec(Tier.FAST, 300.0),
    # pair 判定は 1 呼出 = 小さな関係判定 (max_tokens 300)。専用 step の理由: 以前は
    # Step.TRIAGE を借用しており、triage の step 上書きを黙って継承していた (借用禁止の衛生)。
    # 実測では pair 判定は SFT に不変 (base/s1/s15/s16/N1 の 5 腕が held-out 149 で全問一致、
    # SYNTHESIS.md §27) — serving は base 維持、学習ミックスにも入れない (不活性)。
    Step.PAIR_JUDGE: StepSpec(Tier.FAST, 120.0),
    # kind 分類は 1 記事 = 小さな分類呼出。ML pair モデルは base 産の kind を特徴量として
    # 学習しているため、供給元モデルの無断シフト (step 借用による継承) を遮断する。
    Step.EVENT_KIND: StepSpec(Tier.FAST, 120.0),
    # 軸は 1 記事 = 7 欄の選択 (max_tokens 400)。実測 ~3 秒/件 (s17)。
    Step.SEVERITY_AXES: StepSpec(Tier.FAST, 120.0),
    Step.DIGEST_DEEP_DIVE_SELECT: StepSpec(Tier.FAST, 900.0),
    Step.DIGEST_DEEP_DIVE: StepSpec(Tier.FAST, 900.0),
    Step.SYNTHESIS_DETECT: StepSpec(Tier.FAST, 900.0),
    Step.PIR_DAILY_FOCUS: StepSpec(Tier.FAST, 120.0),
    # 主題判定は 1 件 = 小さな分類呼出 (max_tokens 300)。候補ゲート通過分のみに走る。
    Step.PIR_LLM_JUDGE: StepSpec(Tier.FAST, 120.0),
    # spotlight は PIR 縦断 narrative (散文生成)。timeout は step 固有 600s。
    Step.PIR_SPOTLIGHT: StepSpec(Tier.NARRATIVE, 600.0),
    Step.ACTOR_SYNC: StepSpec(Tier.FAST, 300.0),
    # 1 件 = タイトル+概要の小さな bool 判定 (max_tokens 200)。triage と同じ fast ティア。
    Step.RELEVANCE_JP_CASCADE: StepSpec(Tier.FAST, 120.0),
    # 対話系 (dialog): user-facing・低頻度。外部 LLM を割り当てても収集系バッチ (fast) の
    # コストに波及しない。timeout は従来 (fast 時代) の値を踏襲。
    Step.PIR_COMPILE: StepSpec(Tier.DIALOG, 180.0),
    Step.SELECTOR_PROPOSAL: StepSpec(Tier.DIALOG, 300.0),
    Step.PRECISE_SEARCH: StepSpec(Tier.DIALOG, 180.0),
    # 分析チャットは 1 turn = plan + answer の 2 call。answer はツール結果込みで
    # 入力が嵩むため timeout は広め。
    Step.ASSISTANT_CHAT: StepSpec(Tier.DIALOG, 300.0),
    # 本文翻訳 (オンデマンド + バックログ)。timeout は 1 チャンク (≤5k 字) あたり。
    # 長文は body_translator がチャンク分割して複数回呼ぶ。
    Step.ARTICLE_TRANSLATE: StepSpec(Tier.FAST, 300.0),
    Step.SYNTHESIS_NARRATIVE: StepSpec(Tier.NARRATIVE, 900.0),
    # 事象ニュース精製 (§14b)。実測 65 秒/回 (31B)。毎時 0.25 回なので timeout は 600s で足りる。
    Step.EVENT_NEWS: StepSpec(Tier.NARRATIVE, 600.0),
    # 夜間精査は「think を使う ACH」= narrative ティア経由でモデルと think 設定を継承する
    # (reasoning のモデルを factory 外で wrap すると UI の think 1:1 原則が壊れる)。
    # 台帳 ACH (増分評価は 1 呼出数百秒級) — narrative から分離 (2026-07-24 think ティア分離)。
    Step.SYNTHESIS_ANALYSIS: StepSpec(Tier.REASONING, 900.0),
    Step.EMBED: StepSpec(Tier.EMBEDDING, 120.0),
}

# ティア → num_ctx。現状は全 None (Ollama 既定に委譲 = 現挙動維持)。実測 262144 で
# 我々の prompt (~21k char) は非律速のため設定しない。将来 num_ctx を絞る必要が出たら
# ここに値を入れれば OllamaClient 経由で反映される (seam のみ用意、値は YAGNI で未設定)。
TIER_NUM_CTX: dict[Tier, int | None] = {
    Tier.REASONING: None,
    Tier.NARRATIVE: None,
    Tier.FAST: None,
    Tier.DIALOG: None,
    Tier.EMBEDDING: None,
}

# ティア → 実モデルの **コード既定** (bootstrap + fail-safe)。runtime の SSoT は DB
# (config_store "model_tiers", UI「モデル」タブ)。channels / product_routing と同じく
# 「BUILTIN を base に DB で上書き」= .env を経由しない。初回起動でこれを seed し、以後は
# UI で編集する (運用中に .env を触る必要はない)。fresh DB / rollback 時の bootstrap 値。
# embedding は推奨モデルを既定に (未 pull なら _try_build_embedder が graceful degradation)。
BUILTIN_MODEL_TIERS: dict[str, str] = {
    Tier.REASONING.value: "gemma4:31b",
    # narrative は reasoning から分離 (2026-07-24)。既定は同モデル (挙動保存)。DB に
    # narrative キーが無い既存環境も BUILTIN fallback で migration 不要 (dialog 分離と同型)。
    Tier.NARRATIVE.value: "gemma4:31b",
    Tier.FAST.value: "gemma4:26b",
    # dialog は fast と同モデルを既定に (2026-07-19 分離時の挙動保存。DB 未保存でも
    # resolve_tier_model が本 BUILTIN に fallback するため既存環境で migration 不要)
    Tier.DIALOG.value: "gemma4:26b",
    Tier.EMBEDDING.value: "snowflake-arctic-embed2",
}

# load 結果のプロセス内キャッシュ (db_path 別、product_routing と同パターン)。
_CACHE: dict[str, dict[str, str]] = {}


def _db_enabled() -> bool:
    """DB を runtime SSoT とするか (既定 True)。``MODEL_TIERS_CONFIG_DB=0`` で BUILTIN 直行。"""
    return os.environ.get(_DB_FLAG_ENV, "1").strip() not in ("0", "false", "False", "")


def _load_tier_map(*, db_path: Path | None) -> dict[str, str]:
    """DB (config_store) からティア割当を読む。障害/未投入時は空 dict。"""
    cache_key = str(db_path)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    result: dict[str, str] = {}
    try:
        from src.storage.config_store import get_config

        raw = get_config(MODEL_TIERS_CONFIG_KEY, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — DB 障害でモデル解決を壊さない
        # 基盤の失敗 (DB 未達) は seed へ degrade しない — 出荷時の既定で
        # 走った結果が永久に保存されるため (2026-08-25)
        raise_if_infrastructure(e, context="model_tiers")
        _log.warning("model_tiers_db_load_failed", error=str(e))
        raw = None
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str):
                result[k] = v
    _CACHE[cache_key] = result
    return result


def resolve_tier_model(tier: Tier, *, db_path: Path | None = None) -> str:
    """ティアに割り当てられた実モデル名を返す。

    解決順: DB (config_store) → ``BUILTIN_MODEL_TIERS`` (コード既定)。``.env`` は経由しない
    (channels / product_routing と同じ DB-正・built-in fail-safe パターン)。
    ``MODEL_TIERS_CONFIG_DB=0`` なら DB を飛ばして BUILTIN に直行 (rollback)。DB に当該ティアの
    値が無い場合も BUILTIN に fallback するため seed 前でも壊れない。
    """
    if not _db_enabled():
        return BUILTIN_MODEL_TIERS[tier.value]
    model = _load_tier_map(db_path=db_path).get(tier.value, "")
    return model if model else BUILTIN_MODEL_TIERS[tier.value]


def resolve_embedding_model(*, db_path: Path | None = None) -> str:
    """埋込ティアの実モデル名 (空文字なら埋込は無効)。"""
    return resolve_tier_model(Tier.EMBEDDING, db_path=db_path)


# step 単位のモデル上書き (2026-09-06、2 族分割 SFT の配備単位)。model_tiers config doc
# 内の予約キー ``step:<step名>``。**フラットな str→str** — ネスト dict は
# ``_load_tier_map`` の型フィルタで黙って落ちるため使わない。不在・空文字は
# ティア解決に fallback (挙動保存 = 既存環境で migration 不要)。
STEP_OVERRIDE_PREFIX = "step:"


def resolve_step_model(step: Step, *, db_path: Path | None = None) -> str:
    """step の実効モデル名。解決順: step override (DB) → ティア割当 (DB → BUILTIN)。"""
    spec = STEP_REGISTRY[step]
    if _db_enabled():
        override = _load_tier_map(db_path=db_path).get(f"{STEP_OVERRIDE_PREFIX}{step.value}", "")
        if override:
            return override
    return resolve_tier_model(spec.tier, db_path=db_path)


def load_step_overrides(*, db_path: Path | None = None) -> dict[str, str]:
    """有効な step override の一覧 (UI GET 用)。空文字 (解除) は含めない。"""
    if not _db_enabled():
        return {}
    out: dict[str, str] = {}
    for key, value in _load_tier_map(db_path=db_path).items():
        if key.startswith(STEP_OVERRIDE_PREFIX) and value:
            out[key.removeprefix(STEP_OVERRIDE_PREFIX)] = value
    return out


# 外部主腕のローカル fallback の設定キー (2026-09-06)。``fallback:<tier名>``。
# 運用意図: 主腕は外部 (Sonnet 等) のまま、障害時の受け皿を BUILTIN (31B) から
# 特化 SFT (v1) へ差し替える —「十分な精度が出たら単独へ」の前段の立ち位置。
# fallback は外部障害時の受け皿なので **ローカルモデル限定** (保存検証で強制)。
LOCAL_FALLBACK_PREFIX = "fallback:"


def resolve_local_fallback_model(tier: Tier, *, db_path: Path | None = None) -> str:
    """外部主腕が落ちたときのローカル fallback モデル。DB 未設定は BUILTIN。"""
    if _db_enabled():
        value = _load_tier_map(db_path=db_path).get(f"{LOCAL_FALLBACK_PREFIX}{tier.value}", "")
        if value:
            return value
    return BUILTIN_MODEL_TIERS[tier.value]


def load_local_fallbacks(*, db_path: Path | None = None) -> dict[str, str]:
    """設定済みの fallback 上書き一覧 (UI GET 用)。空文字 (解除) は含めない。"""
    if not _db_enabled():
        return {}
    out: dict[str, str] = {}
    for key, value in _load_tier_map(db_path=db_path).items():
        if key.startswith(LOCAL_FALLBACK_PREFIX) and value:
            out[key.removeprefix(LOCAL_FALLBACK_PREFIX)] = value
    return out


def merge_preserving_step_overrides(
    new_doc: dict[str, str], current_overrides: dict[str, str]
) -> dict[str, str]:
    """保存 doc に既存の予約キー (step override / fallback) を持ち越す。

    - doc が当該 prefix のキーを 1 つも持たない (= UI のティア編集): 既存値を全て継承。
    - doc が当該 prefix のキーを持つ (= 明示管理する API 呼出): doc をそのまま採用
      (含めなかったものは削除の意思とみなす。混在させない)。族ごとに独立に判定する。
    ``current_overrides`` は prefix 付きの現況 (``step:triage`` / ``fallback:narrative``
    形式)。``load_step_overrides``/``load_local_fallbacks`` の prefix なし形式は呼び出し側で
    prefix を付けて渡す。
    """
    merged = dict(new_doc)
    for prefix in (STEP_OVERRIDE_PREFIX, LOCAL_FALLBACK_PREFIX):
        if any(k.startswith(prefix) for k in new_doc):
            continue
        for key, model in current_overrides.items():
            if key.startswith(prefix) and model:
                merged[key] = model
    return merged


# narrative ティアの拡張思考 (extended thinking) 設定。model_tiers config doc 内の予約 key。
# "auto" = 外部モデル割当時のみ think ON (think A/B 2026-07-24: 分析の階層化で優位) /
# "off" = 常に無効。ローカルモデルは値に関わらず常に OFF (gemma thinking 空応答前歴)。
NARRATIVE_THINK_KEY = "narrative_think"
_NARRATIVE_THINK_VALUES = ("auto", "off")


def resolve_narrative_think(*, db_path: Path | None = None) -> str:
    """narrative ティアの think 設定 ("auto"/"off")。未設定・不正値は "auto"。"""
    if not _db_enabled():
        return "auto"
    value = _load_tier_map(db_path=db_path).get(NARRATIVE_THINK_KEY, "auto")
    return value if value in _NARRATIVE_THINK_VALUES else "auto"


# 外部 → ローカルの自動フォールバック (2026-07-19)。``0`` で無効化 (外部失敗 = step 失敗
# の旧挙動に rollback)。既定 ON — レート制限・bridge 停止・残高不足で夜間チェーンを
# 止めないための可用性装置。
_FALLBACK_FLAG_ENV = "LLM_LOCAL_FALLBACK"


def _local_fallback_enabled() -> bool:
    return os.environ.get(_FALLBACK_FLAG_ENV, "1").strip() not in ("0", "false", "False")


def build_llm_for(step: Step, config: AppConfig, *, db_path: Path | None = None) -> LLMClient:
    """step に対応する ``LLMClient`` を組み立てる。

    step → tier → model (DB→BUILTIN) を解決し、step 固有 timeout と tier の num_ctx を適用する。
    ティア割当が外部 (``anthropic:`` / ``claudecode:``) の場合は、当該ティアのローカル既定
    (``BUILTIN_MODEL_TIERS``) を fallback に持つ ``FallbackLLMClient`` で包む — 外部が
    レート制限等で利用不可でも step を失敗させず継続する (``LLM_LOCAL_FALLBACK=0`` で無効)。
    どの経路も構築時に中華系 denylist を検証する (最終防御、プロバイダ横断)。
    """
    spec = STEP_REGISTRY[step]
    if spec.tier is Tier.EMBEDDING:
        raise ValueError(
            f"{step.value} は埋込 step です。resolve_embedding_model を使ってください",
        )
    model_ref = resolve_step_model(step, db_path=db_path)
    client = _build_client_for_ref(
        model_ref,
        spec,
        config,
        local_fallback=resolve_local_fallback_model(spec.tier, db_path=db_path),
    )
    # narrative ティアの think 方針 (外部モデル割当 + 設定 auto のときだけ ON)。
    # ローカル割当時は wrapper を掛けない = call site の think=False がそのまま効く。
    if (
        spec.tier is Tier.NARRATIVE
        and is_external_model(model_ref)
        and resolve_narrative_think(db_path=db_path) == "auto"
    ):
        from src.tools.llm_fallback import ThinkOnClient  # 遅延 import (循環回避)

        client = ThinkOnClient(client)
    # ⭐ 課題の接頭辞は **ここで一度だけ**付ける (2026-09-22)。call site を 1 つずつ直すと
    #    漏れた経路だけ学習時と形が違ってしまう。詳細は src/tools/task_prefix.py。
    from src.tools.task_prefix import TaskPrefixClient, prefix_for  # 遅延 import (循環回避)

    if prefix_for(step):
        client = TaskPrefixClient(client, step)
    return client


def build_llm_for_ref(
    model_ref: str, step: Step, config: AppConfig, *, timeout_seconds: float | None = None
) -> LLMClient:
    """明示 model ref で step 用 client を組む (画面単位の一時 override 用)。

    分析チャットの「この会話だけ別モデル」のような UI override が使う。tier 解決を
    飛ばす以外は ``build_llm_for`` と同一 — denylist 検証・step timeout・外部 ref の
    ローカル自動フォールバックがすべて同じに掛かる。

    ``timeout_seconds`` は step 既定の上書き。**モデルを変えると所要時間も変わる**
    ため必要になる (2026-08-28: 同じ入力で Opus が step 既定 600s を超え、
    timeout が「到達不能」と判定されて経路ごと cooldown に入っていた)。
    既定の step timeout は本番経路のものなので、ここを恒久的に上げない。
    """
    from src.tools.llm_client import validate_model_name

    spec = STEP_REGISTRY[step]
    if timeout_seconds is not None:
        spec = StepSpec(spec.tier, timeout_seconds)
    if spec.tier is Tier.EMBEDDING:
        raise ValueError(f"{step.value} は埋込 step です")
    bare = model_ref
    for prefix in _EXTERNAL_PREFIXES:
        bare = bare.removeprefix(prefix)
    validate_model_name(bare)
    validate_model_name(model_ref)
    from src.tools.task_prefix import TaskPrefixClient, prefix_for  # 遅延 import (循環回避)

    built = _build_client_for_ref(
        model_ref, spec, config, local_fallback=resolve_local_fallback_model(spec.tier)
    )
    # ⭐ 明示 ref の経路 (評価・教師収穫) も本番と同じ形にする — でないと測った形と
    #    動かす形が食い違う (2026-09-22)。
    return TaskPrefixClient(built, step) if prefix_for(step) else built


def _usage_recorder(provider: str, model: str) -> UsageRecorder:
    """llm_usage への消費記録 callback (UI のモデルタブ表示用)。失敗は client 側が握る。

    cache_read_tokens / cost_usd は自己申告できる provider (claudecode) のみ非 0。
    """

    def _record(
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository  # 遅延 import (循環回避)

        RunHistoryRepository().record_llm_usage(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
        )

    return _record


def _endpoint_ref(model_ref: str) -> tuple[str, str] | None:
    """``<接続先名>:<モデル>`` なら (接続先名, モデル) を返す (未登録 prefix は None)。"""
    if ":" not in model_ref or model_ref.startswith(_EXTERNAL_PREFIXES):
        return None
    prefix, rest = model_ref.split(":", 1)
    from src.tools.llm_endpoints import resolve_endpoint  # 遅延 import

    return (prefix, rest) if rest and resolve_endpoint(prefix) is not None else None


def _build_client_for_ref(
    model_ref: str, spec: StepSpec, config: AppConfig, *, local_fallback: str | None = None
) -> LLMClient:
    """model ref → client 構築の共通本体 (外部 dispatch + ローカル fallback 包装)。

    ``local_fallback``: 外部主腕が落ちたときのローカル受け皿 (``fallback:<tier>`` の
    解決値)。None なら BUILTIN (従来挙動)。
    """
    from src.tools.llm_client import LLMError, OllamaClient  # 遅延 import (循環回避)

    def _local(model: str | None = None) -> OllamaClient:
        return OllamaClient(
            base_url=config.ollama_base_url,
            model=model or local_fallback or BUILTIN_MODEL_TIERS[spec.tier.value],
            timeout_seconds=spec.timeout_seconds,
            num_ctx=TIER_NUM_CTX.get(spec.tier),
        )

    ep_ref = _endpoint_ref(model_ref)
    if not model_ref.startswith(_EXTERNAL_PREFIXES) and ep_ref is None:
        return _local(model_ref)

    try:
        primary: LLMClient
        if ep_ref is not None:
            # ユーザ定義接続先 (OpenAI 互換)。キーは .env、消費は llm_usage に記録。
            from src.tools.llm_endpoints import endpoint_api_key, resolve_endpoint
            from src.tools.openai_compat_client import OpenAICompatClient

            ep_name, ep_model = ep_ref
            endpoint = resolve_endpoint(ep_name)
            assert endpoint is not None  # _endpoint_ref が解決済み
            primary = OpenAICompatClient(
                endpoint_name=ep_name,
                model=ep_model,
                base_url=endpoint.base_url,
                api_key=endpoint_api_key(ep_name),
                timeout_seconds=spec.timeout_seconds,
                usage_recorder=_usage_recorder(ep_name, ep_model),
            )
        elif model_ref.startswith(ANTHROPIC_MODEL_PREFIX):
            from src.tools.anthropic_client import AnthropicClient  # 遅延 import (循環回避)

            bare = model_ref.removeprefix(ANTHROPIC_MODEL_PREFIX)
            primary = AnthropicClient(
                model=bare,
                api_key=config.anthropic_api_key,
                timeout_seconds=spec.timeout_seconds,
                usage_recorder=_usage_recorder("anthropic", bare),
            )
        else:
            from src.tools.claude_code_client import ClaudeCodeClient  # 遅延 import (循環回避)

            cc_model = model_ref.removeprefix(CLAUDECODE_MODEL_PREFIX)
            primary = ClaudeCodeClient(
                model=cc_model,
                bridge_url=config.claude_code_bridge_url,
                timeout_seconds=spec.timeout_seconds,
                # 消費台帳の一本化 (2026-07-26): claudecode も llm_usage に記録する
                usage_recorder=_usage_recorder("claudecode", cc_model),
            )
    except LLMError as e:
        # 構築段階の失敗 (API キー未設定等)。fallback ON ならローカルで継続する
        if _local_fallback_enabled():
            _log.warning(
                "llm_external_build_failed_using_local",
                model_ref=model_ref,
                tier=spec.tier.value,
                error=str(e)[:200],
            )
            return _local()
        raise

    if not _local_fallback_enabled():
        return primary
    from src.tools.llm_fallback import FallbackLLMClient  # 遅延 import (循環回避)

    return FallbackLLMClient(primary=primary, fallback=_local())


def load_model_tiers(*, db_path: Path | None = None) -> dict[str, str]:
    """全ティアの実効モデル割当を返す (UI GET 用)。DB → BUILTIN fallback 済。"""
    return {tier.value: resolve_tier_model(tier, db_path=db_path) for tier in Tier}


def seed_model_tiers_if_absent(*, db_path: Path | None = None) -> bool:
    """DB 未投入なら ``BUILTIN_MODEL_TIERS`` を version 1 として取り込む (起動時)。"""
    from src.storage.config_store import seed_config_if_absent

    return seed_config_if_absent(
        MODEL_TIERS_CONFIG_KEY,
        dict(BUILTIN_MODEL_TIERS),
        note="初期 seed (BUILTIN モデルティア既定)",
        db_path=db_path,
    )


def invalidate_model_tiers_cache() -> None:
    """UI 保存後などにキャッシュを破棄する (次回 resolve で DB を読み直す)。"""
    _CACHE.clear()


def validate_model_tiers(raw: object) -> list[str]:
    """保存前検証。エラー文字列の list を返す (空 = OK)。

    - reasoning / fast は非空・中華系禁止 (``validate_model_name``、プロバイダ横断)。
    - embedding は空可 (埋込無効化)、非空なら中華系禁止・外部プロバイダ不可
      (Anthropic に embedding API は無く、埋込は大量呼出でローカル前提)。
    """
    from src.tools.llm_client import LLMForbiddenModelError, validate_model_name

    if not isinstance(raw, dict):
        return ["model_tiers は dict である必要があります"]
    errs: list[str] = []
    think = raw.get(NARRATIVE_THINK_KEY)
    if think is not None and think not in _NARRATIVE_THINK_VALUES:
        errs.append(f"{NARRATIVE_THINK_KEY}: 'auto' か 'off' を指定してください")
    # step override (``step:<step名>``): 未知 step 名は typo として fail-fast。
    # 空文字 = 解除は許容。denylist はティア割当と同じ 3 層で効く。
    known_steps = {s.value for s in Step}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.startswith(STEP_OVERRIDE_PREFIX):
            continue
        step_name = key.removeprefix(STEP_OVERRIDE_PREFIX)
        if step_name not in known_steps:
            errs.append(f"step override '{step_name}': 未知の step 名です")
            continue
        if STEP_REGISTRY[Step(step_name)].tier is Tier.EMBEDDING:
            errs.append(f"step override '{step_name}': 埋込 step は上書きできません")
            continue
        if not isinstance(value, str):
            errs.append(f"step override '{step_name}': モデル名 (文字列) が必要です")
            continue
        if not value.strip():
            continue  # 空 = 解除
        try:
            validate_model_name(value.strip())
        except LLMForbiddenModelError as e:
            errs.append(f"step override '{step_name}': {e}")
    # ローカル fallback (``fallback:<tier名>``): 外部障害時の受け皿なのでローカル限定。
    known_tiers = {t.value for t in Tier}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.startswith(LOCAL_FALLBACK_PREFIX):
            continue
        tier_name = key.removeprefix(LOCAL_FALLBACK_PREFIX)
        if tier_name not in known_tiers or tier_name == Tier.EMBEDDING.value:
            errs.append(f"fallback '{tier_name}': 対象にできないティア名です")
            continue
        if not isinstance(value, str):
            errs.append(f"fallback '{tier_name}': モデル名 (文字列) が必要です")
            continue
        model = value.strip()
        if not model:
            continue  # 空 = 解除
        if is_external_model(model):
            errs.append(f"fallback '{tier_name}': fallback は外部プロバイダに割当できません")
            continue
        try:
            validate_model_name(model)
        except LLMForbiddenModelError as e:
            errs.append(f"fallback '{tier_name}': {e}")
    for tier in Tier:
        value = raw.get(tier.value)
        if value is None and tier is Tier.NARRATIVE:
            continue  # 旧版 doc (narrative 分離前) の revert 互換 — BUILTIN fallback で解決
        if not isinstance(value, str):
            errs.append(f"ティア '{tier.value}': モデル名 (文字列) が必要です")
            continue
        model = value.strip()
        if not model:
            if tier is Tier.EMBEDDING:
                continue  # 埋込は空 = 無効化を許容
            errs.append(f"ティア '{tier.value}': モデル名が空です")
            continue
        if tier is Tier.EMBEDDING and is_external_model(model):
            errs.append(f"ティア '{tier.value}': 外部プロバイダは埋込ティアに割当できません")
            continue
        try:
            validate_model_name(model)
        except LLMForbiddenModelError as e:
            errs.append(f"ティア '{tier.value}': {e}")
    return errs
