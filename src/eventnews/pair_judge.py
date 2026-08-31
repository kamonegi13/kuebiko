"""記事ペアが同じ出来事かを LLM に判定させる (群化の手掛かりの 1 つ)。

⭐ 単独の判定器ではなく **特徴の 1 つ**として使う。2026-08-31 の実測 (365 組):
26B 単独 83% / 特徴量のみ 86% / **両方を木モデルへ渡すと 90%**。

⚠ 判定の原則はプロンプトに置く。利用者裁定 (2026-08-31):
**同じ識別子が出ることと同じ出来事であることは別。**
「Microsoft が直した」と「Lazarus が悪用した」は同じ CVE でも別の出来事で、
「Qilin が 5 件掲載」と「Qilin: BLISS 1041」は同じ行為の別の報じ方。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

#: 要約をこの長さで切る (判定に要るのは冒頭の事実で、長くしても精度は上がらない)
_SUMMARY_CHARS = 420

SYSTEM = "\n".join(
    (
        "あなたは日本の CTI アナリストです。",
        "2 本の記事が **同じ出来事** を報じているかを判定します。",
        "判定の原則:",
        "- 同じ識別子 (CVE・アクター名) が出ることと、同じ出来事であることは別です。",
        "  例: 「Microsoft が脆弱性を修正した」と「Lazarus がその脆弱性を悪用した」は、",
        "  同じ CVE でも別の出来事。",
        "- 同じ行為者による同じ行為を報じているなら同じ出来事です。",
        "  例: 「Qilin がリークサイトに 5 件掲載」と「Qilin: BLISS 1041」は同じ行為の別の報じ方。",
        "- 週刊/日次のまとめ記事は、個別の出来事とは別です。",
        "- 同じ攻撃者の別々の作戦は別の出来事です。",
    )
)


class PairVerdict(BaseModel):
    """LLM の判定。``reason`` は監査用で、特徴量には使わない。"""

    model_config = ConfigDict(extra="forbid")

    same_event: bool
    reason: str


def build_prompt(
    left_title: str,
    left_summary: str,
    left_feed: str,
    right_title: str,
    right_summary: str,
    right_feed: str,
) -> str:
    """判定用プロンプト。**本文ではなく要約を渡す** — 書式が揃い、入力も短くなる。"""

    def block(tag: str, feed: str, title: str, summary: str) -> str:
        body = summary[:_SUMMARY_CHARS] or "(要約なし)"
        return f"【記事{tag}】媒体: {feed}\n見出し: {title}\n要約: {body}"

    return (
        block("A", left_feed, left_title, left_summary)
        + "\n\n"
        + block("B", right_feed, right_title, right_summary)
        + "\n\nこの 2 本は同じ出来事を報じていますか。"
    )


async def judge_pair(llm: LLMClient, prompt: str) -> bool | None:
    """同じ出来事なら True。**失敗は None** (呼び手が「判定なし」として扱う)。

    ⚠ 失敗を False に倒さない — 「別の出来事だと判定した」と「判定できなかった」は
    別の情報で、特徴量としても区別する必要がある。
    """
    try:
        verdict = await llm.generate_structured(
            prompt, PairVerdict, system=SYSTEM, max_attempts=2, think=False
        )
    except Exception as e:  # noqa: BLE001 — 判定不能で群化を止めない
        _log.warning("pair_judge_failed", error=str(e)[:200])
        return None
    return verdict.same_event
