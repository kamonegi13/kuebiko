"""cluster 帯の dedup を確定する前の一問 — 「本当に同内容か」を LLM に確かめる。

⭐ 実測 (2026-09-03・スキップ 88 件の盲検): cluster 帯 (cos>=0.78) の **10.2% が
別内容の誤 dedup** だった。型は「同一ベンダの別製品」(Oracle WebLogic vs HTTP
Server / Ebyte の別型番 / Ubuntu vs SUSE の同名勧告) — 書式とベンダ名で cos が
上がる、群化と同じ「埋込は書式を測る」限界。

一方向の救済網: duplicate と確答したときだけ skip し、**different / unclear /
判定失敗はすべて取込へ倒す** (Recall 優先の任務原則)。取り逃した重複は下流の
事象群化が続報・裏取りとして吸収する (2026-09 週の群化改善で費用が構造的に低下)。
hard 帯 (cos>=0.92) と URL 一致は従来どおり無条件 skip。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

SYSTEM = "\n".join(
    (
        "あなたは日本の CTI アナリストです。重複排除の判定を検査します。",
        "記事 A (題のみ) が記事 B と**同じ内容の再配信・転載・同一報道**なら duplicate、",
        "別の内容 (別の事件・別の対象製品・独自情報を含みうる続報) なら different、",
        "題だけでは判断できなければ unclear と答えてください。",
        "- 同じベンダでも別製品・別型番の脆弱性は different です。",
        "- 同じ事件でも、別の被害者・明確に別の段階の続報は different です。",
    )
)


class DuplicateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str  # duplicate | different | unclear
    reason: str


async def is_duplicate(
    llm: LLMClient,
    *,
    skipped_title: str,
    skipped_feed: str,
    matched_title: str,
) -> bool | None:
    """True = 重複と確答 (skip してよい)。False/None = 取込へ倒す。"""
    prompt = (
        f"【A (スキップ候補)】媒体: {skipped_feed}\n題: {skipped_title}\n\n"
        f"【B (一致先)】題: {matched_title}\n\nA は B の duplicate ですか。"
    )
    try:
        v = await llm.generate_structured(
            prompt, DuplicateVerdict, system=SYSTEM, max_attempts=2, think=False
        )
    except Exception as e:  # noqa: BLE001 — 判定できないときは取り込む (Recall 優先)
        _log.warning("dedup_judge_failed", error=str(e)[:120])
        return None
    if v.verdict == "duplicate":
        return True
    if v.verdict == "different":
        return False
    return None
