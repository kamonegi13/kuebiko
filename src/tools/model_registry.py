"""学習済みモデルの許可リスト — 1 か所の SSoT (P0-27、2026-10-29)。

いままで ``task_prefix`` (接頭辞・未学習 step)・``summary`` (引用の欄)・``focus_v6``
(v6 の要点の形) が各自でモデル名の集合を持っていた。新しいモデルを割り当てるときに
1 か所でも足し忘れると、課題の印・引用の欄・v6 の形が**黙って外れる**。

ここに 1 モデル (または系統の前方一致) 1 行で能力を宣言し、上の集合はすべてここから導く。
新しい学習済みモデル (n20 / s26 等) は ``_CAPABILITIES`` に 1 行足すだけ。

⚠ 値は移す前と同じ (挙動を変えない)。同値性は ``tests/unit/test_model_registry_parity.py``
が固定する。このモジュールは ``Step`` を import しない (``model_tiers`` が既定値をここから
作るので循環する) — step は ``Step`` の値 (文字列) で持つ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

MatchKind = Literal["exact", "prefix"]
Slot = Literal["S", "N"]


@dataclass(frozen=True)
class ModelCapability:
    """モデル名 (または系統の前方一致) 1 つの能力。既定はすべて「何も学習していない」。"""

    name: str
    match: MatchKind = "exact"
    #: 常駐の枠 (S = 判定・分類・要約系 / N = 長い散文系)。枠の「いまの中身」は RESIDENT_SLOTS
    slot: Slot | None = None
    #: 課題の接頭辞つきで学習した (印を付ける)
    task_prefix: bool = False
    #: 技術ごとの原文の引用 (mitre_evidence 欄) を学習した
    evidence_fields: bool = False
    #: v6.2 の SIR 別の要点の形を学習した
    focus_v6: bool = False
    #: 判断の分類器・IOC 検証・ACH 補助・重複判定を専用の印で学習した (s24b 以降)
    new_task_markers: bool = False
    #: 印を付けても学習していない step (``Step`` の値)。印を付けない
    untrained_steps: frozenset[str] = frozenset()
    #: 常駐の例外 (終了日つき)。None = 例外ではない
    exception_until: date | None = None

    def matches(self, model: str) -> bool:
        if self.match == "exact":
            return model == self.name
        return model.startswith(self.name)


_PIR_FOCUS_AXES = frozenset({"pir_daily_focus", "severity_axes"})
_DEEP_DIVE = frozenset({"digest_deep_dive", "digest_deep_dive_select"})
#: s24 系で学習していない step (detect 系・ach/summary の借用・narrative 系・深掘り)
_S24_UNTRAINED = frozenset(
    {
        "synthesis_detect",
        "detect_claim_write",
        "ledger_ach_aux",
        "ioc_llm_verify",
        "event_news",
        "pir_spotlight",
        "synthesis_narrative",
        "digest_deep_dive",
        "digest_deep_dive_select",
    }
)
#: 常駐の例外の終了日 (利用者承認 10-29)。s23i と、それと一緒に退役する gemma4:26b の
#: step 上書き (assign / org) は 2 枠の状態になる 12-02 まで
_TWO_SLOT_DEADLINE: Final = date(2026, 12, 2)
#: 翻訳・MITRE 同期の和訳・triage_geo_rescue が使う kuebiko-sft:26b は 12-20 まで
_TRANSLATION_DEADLINE: Final = date(2026, 12, 20)

#: 登録簿。**新しいモデルはここに 1 行足すだけ** (n20 / s26 は学習が済んでから)。
_CAPABILITIES: Final[tuple[ModelCapability, ...]] = (
    ModelCapability("kuebiko-sft:s19", task_prefix=True, untrained_steps=_PIR_FOCUS_AXES),
    ModelCapability("kuebiko-sft:s21", task_prefix=True, untrained_steps=_PIR_FOCUS_AXES),
    ModelCapability(
        "kuebiko-sft:n19",
        slot="N",
        task_prefix=True,
        untrained_steps=_DEEP_DIVE | _PIR_FOCUS_AXES,
    ),
    ModelCapability("kuebiko-sft:i58", task_prefix=True, evidence_fields=True),
    ModelCapability(
        "kuebiko-sft:s23i",
        task_prefix=True,
        evidence_fields=True,
        exception_until=_TWO_SLOT_DEADLINE,
    ),
    # s24 系 (補間タグ ``s24i25`` / ``s24ci65`` 等を含む前方一致)。S 枠の中身 (s24ci65) もここ
    ModelCapability(
        "kuebiko-sft:s24",
        match="prefix",
        slot="S",
        task_prefix=True,
        evidence_fields=True,
        untrained_steps=_S24_UNTRAINED,
    ),
    ModelCapability("kuebiko-sft:s24b", match="prefix", new_task_markers=True),
    ModelCapability("kuebiko-sft:s24c", match="prefix", new_task_markers=True, focus_v6=True),
    # s24c と同じ課題・印で学習 (build_dataset_s25.py、補間 s25i65 等も前方一致)
    ModelCapability(
        "kuebiko-sft:s25",
        match="prefix",
        task_prefix=True,
        evidence_fields=True,
        focus_v6=True,
        new_task_markers=True,
    ),
    # --- 学習した課題の印を持たないモデル (割当は登録済みでなければならない) ---
    ModelCapability("kuebiko-sft:26b", exception_until=_TRANSLATION_DEADLINE),
    ModelCapability("gemma4:26b"),
    ModelCapability("gemma4:31b"),
)

#: 常駐の 2 枠のいまの中身 (s26 / n20 に替わるときはここの 1 行を変える)
RESIDENT_SLOTS: Final[dict[Slot, str]] = {"S": "kuebiko-sft:s24ci65", "N": "kuebiko-sft:n19"}

#: 既定値 (``BUILTIN_MODEL_TIERS``) の元。fast / reasoning / dialog = S、narrative = N
_TIER_SLOTS: Final[dict[str, Slot]] = {
    "fast": "S",
    "reasoning": "S",
    "narrative": "N",
}


#: dialog の既定・受け皿 (緊急用の gemma4:26b のまま)。s24ci65 は dialog の 4 step
#: (分析チャット・PIR compile・precise search・selector 提案) を学習していない。
#: TODO: S を dialog の受け皿にするのは、4 step で出力の形を確かめてからの別作業。
DIALOG_LOCAL_DEFAULT: Final = "gemma4:26b"


def builtin_local_tiers() -> dict[str, str]:
    """ティア名 → 既定のローカルモデル (埋込は含まない)。dialog だけ緊急用のまま。"""
    tiers = {tier: RESIDENT_SLOTS[slot] for tier, slot in _TIER_SLOTS.items()}
    return {**tiers, "dialog": DIALOG_LOCAL_DEFAULT}


#: 緊急の受け皿 (10-29 判断 J4)。使えるのは受け皿の経路だけ。通常の割当には入れない
EMERGENCY_FALLBACK_MODELS: Final[frozenset[str]] = frozenset({"gemma4:26b"})

#: 常駐の例外 (名前 → 終了日)。終了日を過ぎると CI が赤くなる
RESIDENT_EXCEPTIONS: Final[dict[str, date]] = {
    c.name: c.exception_until for c in _CAPABILITIES if c.exception_until is not None
}


#: step 単位の例外 ((step 値, モデル) → 終了日)。assign / org の判定は素の 26B の
#: logprob で較正してあり、s23i と一緒に退役する (2 枠の状態 = 12-02、利用者承認)
RESIDENT_STEP_EXCEPTIONS: Final[dict[tuple[str, str], date]] = {
    ("assign_evidence_judge", "gemma4:26b"): _TWO_SLOT_DEADLINE,
    ("org_identity_judge", "gemma4:26b"): _TWO_SLOT_DEADLINE,
}


def _names(*, match: MatchKind, flag: str) -> tuple[str, ...]:
    return tuple(c.name for c in _CAPABILITIES if c.match == match and getattr(c, flag))


#: 接頭辞つきで学習したモデル (完全一致 / 前方一致)
PREFIX_TRAINED_EXACT: Final[frozenset[str]] = frozenset(_names(match="exact", flag="task_prefix"))
PREFIX_TRAINED_PREFIXES: Final[tuple[str, ...]] = _names(match="prefix", flag="task_prefix")
#: 引用の欄を学習したモデル
EVIDENCE_EXACT: Final[frozenset[str]] = frozenset(_names(match="exact", flag="evidence_fields"))
EVIDENCE_PREFIXES: Final[tuple[str, ...]] = _names(match="prefix", flag="evidence_fields")
#: v6 の要点の形を学習した系統 (前方一致)
FOCUS_V6_PREFIXES: Final[tuple[str, ...]] = _names(match="prefix", flag="focus_v6")
#: 専用の印で新しい課題を学習した系統 (前方一致)
NEW_TASK_PREFIXES: Final[tuple[str, ...]] = _names(match="prefix", flag="new_task_markers")
#: 名前 → 学習していない step の値 (完全一致 / 前方一致の登録名をそのまま鍵にする)
UNTRAINED_STEP_VALUES: Final[dict[str, frozenset[str]]] = {
    c.name: c.untrained_steps for c in _CAPABILITIES if c.untrained_steps
}


def find_capability(model: str) -> ModelCapability | None:
    """登録済みの能力 (完全一致を優先、なければ最も長い前方一致)。未登録は None。"""
    name = model.strip()
    exact = next((c for c in _CAPABILITIES if c.match == "exact" and c.name == name), None)
    if exact is not None:
        return exact
    candidates = [c for c in _CAPABILITIES if c.match == "prefix" and c.matches(name)]
    return max(candidates, key=lambda c: len(c.name), default=None)


def is_registered(model: str) -> bool:
    """このモデル名は登録簿にあるか (外部・埋込の判定は呼び手の責務)。"""
    return find_capability(model) is not None


def registered_names() -> tuple[str, ...]:
    """登録された名前 (前方一致の系統を含む)。"""
    return tuple(c.name for c in _CAPABILITIES)


#: 評価中の一時タグ (task_prefix と同じ環境変数、カンマ区切り)
EVAL_TAGS_ENV: Final = "SFT_TASK_PREFIX_MODELS"


def eval_tag_models() -> frozenset[str]:
    return frozenset(m.strip() for m in os.environ.get(EVAL_TAGS_ENV, "").split(",") if m.strip())


#: 評価で未登録のモデルをその場だけ許可する (カンマ区切り)。接頭辞は付けない
#: (``SFT_TASK_PREFIX_MODELS`` は印も付けるので別にした)。本番の env には置かない
EVAL_ALLOW_ENV: Final = "MODEL_EVAL_ALLOW"


def eval_allowed_models() -> frozenset[str]:
    return frozenset(m.strip() for m in os.environ.get(EVAL_ALLOW_ENV, "").split(",") if m.strip())


def is_override_allowed(model: str) -> bool:
    """明示の上書き (比較・チャットの会話単位) に使ってよいローカルモデルか。

    登録済み ∪ 評価中のタグ ∪ ``MODEL_EVAL_ALLOW``。外部は呼び手が先に判定する。
    """
    name = model.strip()
    return is_registered(name) or name in eval_tag_models() or name in eval_allowed_models()
