"""群化のシャドー観測 — ML 判定の材料を実トラフィックで貯める。

**本番の挙動は一切変えない。** 現行の規則で群化した後に、同じペアを ML の材料と
して観測し直して記録するだけ。2026-08-31 の実測では評価セット 365 組で
現行 76% / 提案 90% だったが、⭐ **ラベルは全て 1 人が付けたもの**なので、実際の
取り込みで両者がどう食い違うかを見てから切り替える。

費用の目安 (実測 7 日): 判定ペアは中央値 10 組 / p90 41 組 / 最大 115 組。
26B (think=False) が 2.7 秒/組なので、最大の時間でも 5 分。
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import numpy as np

from src.eventnews.grouping import edge_is_allowed
from src.eventnews.models import WINDOW_HOURS, MemberArticle
from src.eventnews.pair_features import (
    FEATURE_NAMES,
    PairSide,
    concrete_shared_names,
    pair_features,
)
from src.eventnews.pair_judge import build_prompt, judge_pair
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

#: 1 回の観測で見るペアの上限。実測 p90 が 41 組なので、暴発だけを抑える値。
_MAX_PAIRS = 150
#: 有効化フラグ。⭐ 既定は off — LLM 呼出を伴うので明示的に開ける。
_ENV_FLAG = "EVENTNEWS_PAIR_SHADOW"


def is_enabled() -> bool:
    return os.environ.get(_ENV_FLAG, "0") == "1"


def _side(member: MemberArticle, vec: np.ndarray, summary_vec: np.ndarray | None) -> PairSide:
    return PairSide(
        article_id=member.article_id,
        title=member.title,
        category=member.category,
        feed_title=member.feed_title,
        published_at=member.anchor_ts,
        entities=member.entities,
        vector=vec,
        summary_vector=summary_vec,
    )


def select_pairs(
    candidates: Sequence[MemberArticle],
    members: Sequence[MemberArticle],
    vectors: dict[str, np.ndarray],
) -> list[tuple[MemberArticle, MemberArticle]]:
    """観測するペアを決める (決定論の前段の関門と同じ条件で絞る)。

    ⚠ 絞りが本番と違うと、観測した比較が本番の比較にならない
    (2026-08-24 に評価と本番で取得が分かれていて挙動が一致しなかった)。
    """
    out: list[tuple[MemberArticle, MemberArticle]] = []
    for cand in candidates:
        if cand.article_id not in vectors:
            continue
        for member in members:
            if member.article_id == cand.article_id or member.article_id not in vectors:
                continue
            gap = abs((cand.anchor_ts - member.anchor_ts).total_seconds())
            if gap > WINDOW_HOURS * 3600:
                continue
            left = _side(cand, vectors[cand.article_id], None)
            right = _side(member, vectors[member.article_id], None)
            if not concrete_shared_names(left, right):
                continue
            out.append((cand, member))
            if len(out) >= _MAX_PAIRS:
                return out
    return out


async def observe(
    repo: object,
    candidates: Sequence[MemberArticle],
    members: Sequence[MemberArticle],
    vectors: dict[str, np.ndarray],
    *,
    llm: LLMClient,
    embed_summary: Callable[[Sequence[MemberArticle]], Awaitable[dict[str, np.ndarray]]],
    now: datetime | None = None,
) -> int:
    """ペアを観測して記録する。**戻り値は記録した件数**。例外は呼び手で握り潰す。"""
    pairs = select_pairs(candidates, members, vectors)
    if not pairs:
        return 0
    involved = {m.article_id: m for pair in pairs for m in pair}
    svecs = await embed_summary(list(involved.values()))
    stamp = now or datetime.now(UTC)
    recorded = 0
    for cand, member in pairs:
        left = _side(cand, vectors[cand.article_id], svecs.get(cand.article_id))
        right = _side(member, vectors[member.article_id], svecs.get(member.article_id))
        feats = pair_features(left, right)
        shared = tuple(sorted(left.entities & right.entities))
        cos = feats[0]
        rule = edge_is_allowed(left.entities, right.entities, shared, cos)
        same = await judge_pair(
            llm,
            build_prompt(
                left.title,
                cand.summary,
                left.feed_title,
                right.title,
                member.summary,
                right.feed_title,
            ),
        )
        repo.record_pair_shadow(  # type: ignore[attr-defined]
            observed_at=stamp,
            left_id=left.article_id,
            right_id=right.article_id,
            features_json=json.dumps(dict(zip(FEATURE_NAMES, feats, strict=True))),
            llm_same=same,
            rule_joined=rule,
            cos=cos,
        )
        recorded += 1
    _log.info("eventnews_pair_shadow", pairs=recorded)
    return recorded
