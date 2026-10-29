"""台帳の割当判定 ML に gemma4:26b の P(yes) を特徴量として足す (F2b、2026-10-11)。

F2 の効果測定 (data/eval/f2_report.md): Opus 盲検 662 組で ML 単独 AUC 0.917 →
ML+P(yes)(logit) AUC 0.955、本番相当 (規則が候補に出した) 260 組で精度 80%→85%
(回収 79% のまま)。本番の経路の F1 (Opus 盲検 662 組、data/eval/F1_logprob_pin_report.md)
では ``kuebiko-sft:s23i`` 0.956・``gemma4:26b`` 0.970 で、差は閾値の位置にある
(旧記述の「s23i は無情報 AUC 0.537」は撤回済み)。``Step.ASSIGN_EVIDENCE_JUDGE`` の
step 上書き (``step:assign_evidence_judge``) は、割当の照合と閾値の表が前提。

既定 off (``ASSIGN_PROBE_FEATURE``)。probe が ``None`` (外部 LLM 割当・denylist 抵触・
通信失敗) のときは、この module は何もしない — 呼び出し側 (stateful._gate_decider) が
base モデル (特徴量追加なし) にそのまま退避する。
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING

from src.assessment.assignment import STRONG_ANCHOR_TYPES
from src.tools.decision_probe import probe_choice
from src.tools.model_tiers import Step

if TYPE_CHECKING:
    from pathlib import Path

    import ollama

    from src.config_loader import AppConfig

#: この特徴量を使うか (既定 off)。"0" 以外を明示したときだけ有効化する。
ASSIGN_PROBE_FEATURE_ENV = "ASSIGN_PROBE_FEATURE"

#: 問いに載せる固有名詞の種別。data/mlx/assign_probe_export.py の ``STRONG`` と同じ集合
#: (順序は固定 — frozenset 由来の順不定を避けるための表示順であって、学習データの
#: 元の DB 挿入順とは一致しない)。
_ANCHOR_TYPES_IN_ORDER: tuple[str, ...] = (
    "cve",
    "victim_org",
    "malware_family",
    "actor",
    "actor_provisional",
    "campaign",
)
if frozenset(_ANCHOR_TYPES_IN_ORDER) != STRONG_ANCHOR_TYPES:
    raise RuntimeError(
        "assign_probe: 固有名詞の種別が assignment.STRONG_ANCHOR_TYPES からずれています"
    )

#: 問いに載せる固有名詞の上限 (data/mlx/assign_probe_export.py と同じ)
_ANCHOR_LIMIT = 12
#: 記事要約の文字数上限 (同上)
_SUMMARY_CHAR_LIMIT = 600

#: logit 変換時の clip (data/mlx/assign_probe_cv.py と同じ。p=0/1 が多いため ±inf を避ける)
_LOGIT_EPS = 1e-4


def assign_probe_feature_enabled() -> bool:
    """``ASSIGN_PROBE_FEATURE`` (既定 off)。"0" 以外を明示したときだけ有効化する。"""
    value = os.environ.get(ASSIGN_PROBE_FEATURE_ENV, "0").strip()
    return value not in ("", "0")


def probe_logit(p_yes: float) -> float:
    """P(yes) → logit (学習時と同じ clip、data/mlx/assign_probe_cv.py 踏襲)。"""
    clipped = min(max(p_yes, _LOGIT_EPS), 1 - _LOGIT_EPS)
    return math.log(clipped / (1 - clipped))


def sorted_strong_anchors(anchors: frozenset[str]) -> list[str]:
    """情勢の "type:value" キーから、問いに載せる強い固有名詞を抽出する (上限 12)。

    frozenset は順不定 (DB 保存時の挿入順は再現できない) なので文字列昇順で決定的にする —
    学習データの goldset export (DB の JSON 配列順) と完全一致はしないが、内容の集合は同じ。
    """
    strong = sorted(a for a in anchors if a.split(":", 1)[0] in _ANCHOR_TYPES_IN_ORDER)
    return strong[:_ANCHOR_LIMIT]


def assign_probe_question(
    *, situation_title: str, anchors: frozenset[str], article_title: str, article_summary: str
) -> str:
    """data/mlx/assign_probe_run.py の ``question_text`` と同じ文面 (再現性のため変えない)。"""
    anchor_str = ", ".join(sorted_strong_anchors(anchors)) or "(なし)"
    summary = (article_summary or "")[:_SUMMARY_CHAR_LIMIT]
    return (
        "追跡中の「情勢」(見出し+主張) と記事を見て、この記事がこの情勢そのものの証拠かを"
        "判定してください。\n"
        "same (この情勢そのものの続報・詳報・証拠) なら Yes、related (主題は近いが別の事案) または"
        " unrelated (関係しない) なら No と答えてください。\n\n"
        f"## 情勢\n{situation_title}\n固有名詞: {anchor_str}\n\n"
        f"## 記事\n見出し: {article_title}\n要約: {summary}\n\n"
        "この記事は、上の情勢そのものの証拠ですか。"
    )


async def probe_assign_p_yes(
    *,
    situation_title: str,
    anchors: frozenset[str],
    article_title: str,
    article_summary: str,
    config: AppConfig,
    db_path: Path | None = None,
    client: ollama.AsyncClient | None = None,
) -> float | None:
    """(情勢, 記事) の組の P(yes)。``probe_choice`` が ``None`` を返す場合はそのまま返す。"""
    question = assign_probe_question(
        situation_title=situation_title,
        anchors=anchors,
        article_title=article_title,
        article_summary=article_summary,
    )
    probabilities = await probe_choice(
        Step.ASSIGN_EVIDENCE_JUDGE,
        question,
        ("Yes", "No"),
        config=config,
        db_path=db_path,
        client=client,
    )
    return None if probabilities is None else probabilities.get("Yes")
