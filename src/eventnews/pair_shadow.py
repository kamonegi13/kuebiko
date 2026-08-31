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
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from src.eventnews import pair_model
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


@dataclass(frozen=True)
class PairVerdict:
    """1 ペアぶんの評価結果 — 規則 / LLM / ML の 3 者を **同じ材料から 1 回で** 作る。

    ⭐ 2026-09-01: 切替前は ``decide`` と ``observe`` が同じペア集合に同じ 26B 判定を
    それぞれ掛けていた (実測 毎時 4-42 組・上限 150 組ぶん丸ごと二重)。判定を 1 か所に
    まとめ、群化に渡す値と記録する値を同じ評価から取る。
    """

    left: PairSide
    right: PairSide
    features: list[float]
    rule_joined: bool
    llm_same: bool | None
    ml_proba: float | None
    ml_joined: bool | None

    @property
    def key(self) -> frozenset[str]:
        return frozenset((self.left.article_id, self.right.article_id))


async def evaluate(
    candidates: Sequence[MemberArticle],
    members: Sequence[MemberArticle],
    vectors: dict[str, np.ndarray],
    *,
    llm: LLMClient,
    embed_summary: Callable[[Sequence[MemberArticle]], Awaitable[dict[str, np.ndarray]]],
) -> list[PairVerdict]:
    """観測対象のペアを 1 回だけ評価する。**群化にも記録にもこの結果を使う。**"""
    return await judge_pairs(
        select_pairs(candidates, members, vectors),
        vectors,
        llm=llm,
        embed_summary=embed_summary,
    )


async def judge_pairs(
    pairs: Sequence[tuple[MemberArticle, MemberArticle]],
    vectors: dict[str, np.ndarray],
    *,
    llm: LLMClient,
    embed_summary: Callable[[Sequence[MemberArticle]], Awaitable[dict[str, np.ndarray]]],
) -> list[PairVerdict]:
    """**明示したペア**を評価する。ペアの選び方は呼び手が決める。

    ⚠ 判定の実装はここ 1 か所だけ。毎時の群化と遡及統合が別々に持つと必ずずれる
    (2026-08-31 に遡及側が自前の条件を持っていたため本番のガードが効かなかった。
    2026-09-01 には本番だけ ML になり、遡及が決定論のままでまとめ記事を潰していた)。
    """
    if not pairs:
        return []
    involved = {m.article_id: m for pair in pairs for m in pair}
    svecs = await embed_summary(list(involved.values()))
    model = pair_model.load_model()  # ⭐ ループの外で 1 回だけ (以前はペアごとに読み直していた)
    out: list[PairVerdict] = []
    for cand, member in pairs:
        left = _side(cand, vectors[cand.article_id], svecs.get(cand.article_id))
        right = _side(member, vectors[member.article_id], svecs.get(member.article_id))
        feats = pair_features(left, right)
        shared = tuple(sorted(left.entities & right.entities))
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
        proba = joined = None
        if model is not None:
            full = feats + [1.0 if same else 0.0, 0.0 if same is None else 1.0]
            proba = model.probability(full)
            joined = model.joins(full, llm_available=same is not None)
        out.append(
            PairVerdict(
                left=left,
                right=right,
                features=feats,
                rule_joined=edge_is_allowed(left.entities, right.entities, shared, feats[0]),
                llm_same=same,
                ml_proba=proba,
                ml_joined=joined,
            )
        )
    return out


def decisions_of(verdicts: Sequence[PairVerdict]) -> dict[frozenset[str], bool]:
    """群化へ渡す判定表。**ML が採点できていなければ空** → 呼び手は決定論のまま。"""
    return {v.key: bool(v.ml_joined) for v in verdicts if v.ml_joined is not None}


def record(repo: object, verdicts: Sequence[PairVerdict], now: datetime | None = None) -> int:
    """評価結果をシャドー観測として残す。**戻り値は記録した件数**。"""
    stamp = now or datetime.now(UTC)
    for v in verdicts:
        repo.record_pair_shadow(  # type: ignore[attr-defined]
            observed_at=stamp,
            left_id=v.left.article_id,
            right_id=v.right.article_id,
            features_json=json.dumps(dict(zip(FEATURE_NAMES, v.features, strict=True))),
            llm_same=v.llm_same,
            rule_joined=v.rule_joined,
            cos=v.features[0],
            ml_proba=v.ml_proba,
            ml_joined=v.ml_joined,
        )
    _log.info("eventnews_pair_shadow", pairs=len(verdicts))
    return len(verdicts)


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
    verdicts = await evaluate(candidates, members, vectors, llm=llm, embed_summary=embed_summary)
    if not verdicts:
        return 0
    return record(repo, verdicts, now=now)


#: ML 判定を **本番の群化に使う** フラグ。既定は off (シャドーだけ回す)。
_ENV_LIVE = "EVENTNEWS_PAIR_ML"


def is_live() -> bool:
    """ML の判定を群化に反映するか。⭐ 有効化には観測 (shadow) も必要ない —
    こちらだけ立てても動くが、切替後も観測を続けると比較ができる。"""
    return os.environ.get(_ENV_LIVE, "0") == "1"


def is_ml_ready() -> bool:
    """ML を群化に使える状態か。**フラグが立っていてもモデルが読めなければ False**。

    ⭐ 群単位の実測 (2026-08-31・365 組): 決定論 67% → これ 83%、誤結合 33 → 14。
    ペア単位では 70% → 88% で、差は連鎖と割当の取り合いによるもの。
    """
    return is_live() and pair_model.load_model() is not None
