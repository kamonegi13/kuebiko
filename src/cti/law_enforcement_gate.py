"""alert channel: 当局の法執行発表を除外する関門 (R1b、2026-10-11)。

利用者決定 (10-11): 当局による摘発・無力化・起訴・押収・逮捕・制裁指定の発表
(例: FBI が中国系 APT の基盤を無力化) は alert にしない (watch/日次で読む)。
根拠: R1 の 30 日再現 (data/eval/r1_report.md) で alert 対象外の多くがこの型だった。

判定は 2 経路 (比較用、どちらも呼べる):
    1. **決定的な語の条件** (``has_law_enforcement_keyword``) — 高速・無料。
       takedown/indict/arrest/seized/disrupt/摘発/起訴/逮捕/無力化 に加え、利用者の
       元の決定文 (1) が明示する「押収」も英語 "seized" の対訳として加えた
       (差し押さえも同義語として追加)。
    2. **logprob 判定** (``probe_law_enforcement_announcement``、decision_probe 経由、
       ``Step.ALERT_LAW_ENFORCEMENT_JUDGE``) — 「記事の主題が当局の法執行発表か」を
       1 回の前向き計算で読む。学習していない新しい判断のため、既存の SFT モデルより
       素の fast ティアモデルの判定が妥当か、gold set で確認が必要
       (data/eval/r1b_report.md に設計を記載、Opus 裁定は承認後)。

本番適用は alert 候補にのみ (watch へ降格)。他チャンネルの判定は変えない。

rollback: 環境変数 ``ALERT_EXCLUDE_LAW_ENFORCEMENT`` を既定 ("0") のままにすると無効化。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from src.tools.decision_probe import probe_choice
from src.tools.model_tiers import Step

if TYPE_CHECKING:
    import ollama

    from src.config_loader import AppConfig

GATE_ENV = "ALERT_EXCLUDE_LAW_ENFORCEMENT"

#: 英語の決定的な語 (単語境界つき、大小文字不問)。
_KEYWORD_PATTERN_EN: re.Pattern[str] = re.compile(
    r"\b(takedown|take[- ]down|indict\w*|arrest\w*|seiz\w*|disrupt\w*|sanction\w*\s+design\w*)\b",
    re.IGNORECASE,
)

#: 日本語の決定的な語 (部分一致)。利用者の決定文 (1) の 5 語
#: (摘発・無力化・起訴・押収・逮捕) + 「差し押さえ」(押収の同義語) + 「制裁指定」。
_KEYWORD_WORDS_JA: tuple[str, ...] = (
    "摘発",
    "無力化",
    "起訴",
    "押収",
    "差し押さえ",
    "逮捕",
    "制裁指定",
)


def is_law_enforcement_gate_enabled() -> bool:
    """``ALERT_EXCLUDE_LAW_ENFORCEMENT`` (既定 off)。"0" 以外を明示したときだけ有効化する。"""
    value = os.environ.get(GATE_ENV, "0").strip()
    return value not in ("", "0")


def has_law_enforcement_keyword(title: str, summary: str = "") -> bool:
    """決定的な語の条件 (比較用・fallback)。タイトル+要約に 1 語でもあれば True。"""
    text = f"{title} {summary}"
    if _KEYWORD_PATTERN_EN.search(text):
        return True
    return any(word in text for word in _KEYWORD_WORDS_JA)


def law_enforcement_probe_question(title: str, summary: str) -> str:
    """probe_choice に渡す問い (再現性のため文面を固定する)。"""
    return (
        "以下はニュース記事のタイトルと要約です。\n\n"
        f"タイトル: {title}\n\n"
        f"要約:\n{summary}\n\n"
        "質問: この記事の主題は、当局 (警察・検察・FBI・司法省等) による"
        "摘発・無力化・起訴・押収・逮捕・制裁指定の**発表**か。"
        "技術的な脅威の詳細や新たな被害の説明が主題なら No と答える。"
    )


async def probe_law_enforcement_announcement(
    *,
    title: str,
    summary: str,
    config: AppConfig,
    db_path: Path | None = None,
    client: ollama.AsyncClient | None = None,
) -> float | None:
    """P(当局の法執行発表) を logprob で読む。``probe_choice`` が ``None`` を返す場合
    (外部 LLM 割当・denylist 抵触・通信失敗) もそのまま ``None`` を返す
    (呼び出し側は ``has_law_enforcement_keyword`` へ退避すること)。
    """
    probabilities = await probe_choice(
        Step.ALERT_LAW_ENFORCEMENT_JUDGE,
        law_enforcement_probe_question(title, summary),
        ("はい", "いいえ"),
        config=config,
        db_path=db_path,
        client=client,
    )
    if probabilities is None:
        return None
    return probabilities.get("はい")


__all__ = [
    "GATE_ENV",
    "has_law_enforcement_keyword",
    "is_law_enforcement_gate_enabled",
    "law_enforcement_probe_question",
    "probe_law_enforcement_announcement",
]
