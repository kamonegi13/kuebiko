"""主題アクターの **focused 単機能分類器** (2026-07-26)。

背景: summarizer (過負荷 26B、多数フィールド同時出力) の routing_flags.primary_actor_id は
末尾フィールド枯死で **実測 0/25 産出** (本文主題型の cyber 記事)。intent/editorial_stance/
diamond/event_date と同じ病理 — summarizer から切り出し、取得時に専用 LLM 呼び出しで
確実に付与する (CLAUDE.md §3「過負荷モデルに分類を足さない」原則の 5 度目の適用)。

設計 (誤帰属を構造的に防ぐ):
- **候補を本文言及集合に限定**: LLM には「記事に登場する既知アクター」のみを提示し、
  その中から主題を 1 つ選ばせる。辞書ゲート (確定帰属は辞書のみ) + 言及所属 (記事に
  出ていないアクターを主題にしない) の二重ゲートを **プロンプト構造で内蔵**する —
  LLM は候補外を返せない (最悪の失敗が「候補内の選び間違い」に限定される)。
- **迷ったら none**: 主題が曖昧なら選ばない (mention のまま)。過剰帰属を防ぐ。
- title 層で決まる記事には呼ばない (呼び出し側が gate) — LLM 呼び出しは曖昧クラス
  (~16 件/日、summarizer の 3%) に限定。ローカル LLM のみ。
"""

from __future__ import annotations

from pydantic import BaseModel

from src.cti.actor_normalizer import ActorAlias
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_BODY_CHARS = 2000


class _SubjectOut(BaseModel):
    subject_actor_id: str = ""  # 候補 id のいずれか、または "" (主題なし)
    confidence: str = "low"  # high / medium / low


_PROMPT = """次の記事が **主題として扱っているアクター** を 1 つ選べ。

主題 = 記事の主語 (その集団の活動・作戦・攻撃を報じている)。
単なる言及 (比較・背景・「〜に類似」「過去に〜も」) は主題ではない。

**必ず下の候補リストの id から選ぶこと。候補外は返さない。**
主題が候補にない / 曖昧なら subject_actor_id="" (空) を返す。

候補 (記事に登場した既知アクター):
{candidates}

タイトル: {title}
本文: {body}

JSON {{"subject_actor_id": "<候補id or 空>", "confidence": "high|medium|low"}} のみ返答。"""


async def classify_subject_actor(
    llm: LLMClient,
    *,
    title: str,
    body: str,
    candidates: list[ActorAlias],
) -> tuple[str, str]:
    """本文主題アクターを focused に判定する。返り値 (actor_id, confidence)。

    candidates = 本文言及集合 (R-A 済)。空なら呼ばない前提だが防御的に ('', 'low')。
    障害時・候補外返答は ('', 'low') に倒す (安全側 = 主題なし)。
    """
    text = (body or "").strip()
    valid_ids = {c.id for c in candidates}
    if not text or not valid_ids:
        return "", "low"
    cand_lines = "\n".join(f"- {c.id}: {c.canonical}" for c in candidates)
    prompt = _PROMPT.format(
        candidates=cand_lines, title=(title or "").strip(), body=text[:_BODY_CHARS]
    )
    try:
        res = await llm.generate_structured(prompt, schema=_SubjectOut, think=False)
    except Exception as e:  # noqa: BLE001 — 分類失敗で記事処理を止めない
        _log.warning("subject_actor_classify_failed", error=str(e)[:80])
        return "", "low"
    pid = (res.subject_actor_id or "").strip()
    # 候補外・幻覚は棄却 (構造ゲート: LLM は候補内しか主題にできない)
    if pid not in valid_ids:
        return "", "low"
    conf = res.confidence if res.confidence in ("high", "medium", "low") else "low"
    return pid, conf
