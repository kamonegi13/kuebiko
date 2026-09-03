"""記事ペアの特徴量 — 「同じ出来事の報道か」を機械学習で判定するための材料。

2026-08-31 の実測 (365 組のラベル付き評価セット、data/eval/) で、決定論の規則だけ
では 76% が天井と分かった。⭐ 天井の正体は **埋込コサインが出来事ではなく書式を
測っている**こと — 同じ出来事の違う書式 (CISA の告知 ⇔ ベンダー注意喚起) は離れ、
違う出来事の同じ書式 (流出サイトの別被害者) は近づく。

⭐⭐ 越える方法は「良い判定器を 1 つ選ぶ」ことではなく、**複数の弱い手掛かりを
特徴として渡す**ことだった。26B の二値判定は単独で 83%、要約の埋込は単独で +2pt
だが、両方を特徴として木モデルへ渡すと 90% になる (片方だけでは 85-86%、
両方外すと 76% = 現行と同じ)。

この module は **純粋ロジック** (DB / LLM 非依存)。呼び手が材料を集めて渡す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from src.eventnews.event_kind import kind_pair_features
from src.eventnews.grouping import blocked_by_different_victims, names_of_type

#: まとめ記事の見出しに出る語。個別の出来事とは別に扱うための手掛かり。
_ROUNDUP = re.compile(
    r"まとめ|ダイジェスト|週刊|今週|日次|ブリーフィング|Recap|Weekly|一覧|"
    r"新たに\s*\d+|複数の被害|等の|Issue #"
)
#: 「3 件」「5 社」等の件数表記。まとめ側の目印になる。
_COUNT = re.compile(r"(\d+)\s*(件|社|組織|つの)")
#: 時間差の上限 (これ以上離れていても情報量は増えない)
_MAX_HOURS = 336.0

#: 特徴の名前。**順序が意味を持つ** — 学習済みモデルの列順と一致させること。
FEATURE_NAMES: tuple[str, ...] = (
    "cos",
    "cos_summary",
    "cos_delta",
    "shared_names",
    "shared_cves",
    "shared_victim",
    "shared_malware",
    "shared_tool",
    "shared_actor",
    "victim_conflict",
    "same_category",
    "roundup_both",
    "roundup_one",
    "same_feed",
    "title_bigram_jaccard",
    "hours_apart",
    "title_len_diff",
    "count_both",
    "count_one",
    # 種別対 (event_kind、2026-09-03)。定義の SSoT は event_kind.KIND_FEATURE_NAMES
    "kind_same",
    "kind_advisory_vs_incident",
    "kind_roundup_one",
    "kind_stats_one",
)


@dataclass(frozen=True)
class PairSide:
    """特徴量を作るのに要る 1 記事分の材料。"""

    article_id: str
    title: str
    category: str
    feed_title: str
    published_at: datetime
    entities: frozenset[tuple[str, str]]
    #: 本文の埋込 (既存の article_embeddings)
    vector: np.ndarray
    #: 見出し + 要約の埋込。⭐ 書式が揃うので同じ出来事どうしが近づく
    summary_vector: np.ndarray | None = None
    #: 記事の種別 (event_kind.KINDS)。未分類は "other" — 学習時の退避先と揃える
    kind: str = "other"


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _cos(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if not na or not nb:
        return None
    return float(np.dot(a / na, b / nb))


def pair_features(left: PairSide, right: PairSide) -> list[float]:
    """1 ペア分の特徴量。``FEATURE_NAMES`` と同じ順で返す。

    ⚠ 順序を変えるときは学習済みモデルも作り直すこと (列順で対応している)。
    """
    shared = left.entities & right.entities
    types = [t for t, _ in shared]
    cos = _cos(left.vector, right.vector) or 0.0
    cos_summary = _cos(left.summary_vector, right.summary_vector)
    if cos_summary is None:  # 要約が無い記事は本文の値で代替する
        cos_summary = cos
    ra, rb = bool(_ROUNDUP.search(left.title)), bool(_ROUNDUP.search(right.title))
    ca, cb = bool(_COUNT.search(left.title)), bool(_COUNT.search(right.title))
    ga, gb = _bigrams(left.title), _bigrams(right.title)
    hours = abs((left.published_at - right.published_at).total_seconds()) / 3600.0
    return [
        cos,
        cos_summary,
        cos_summary - cos,
        float(len({v.casefold() for _, v in shared})),
        float(types.count("cve")),
        float("victim_org" in types),
        float("malware_family" in types),
        float("tool" in types),
        float("actor" in types),
        float(blocked_by_different_victims(left.entities, right.entities)),
        float(bool(left.category) and left.category == right.category),
        float(ra and rb),
        float(ra != rb),
        float(left.feed_title == right.feed_title),
        len(ga & gb) / max(1, len(ga | gb)),
        min(hours, _MAX_HOURS),
        abs(len(left.title) - len(right.title)) / 50.0,
        float(ca and cb),
        float(ca != cb),
        *kind_pair_features(left.kind, right.kind),
    ]


def concrete_shared_names(left: PairSide, right: PairSide) -> set[str]:
    """「何が起きたか」を指す共有名。アクター名としても出る名前は除く。

    LLM 判定へ回す前の絞り込みに使う (アクター名だけの共有では判定させない)。
    """
    shared = left.entities & right.entities
    actors = names_of_type(left.entities, "actor") | names_of_type(right.entities, "actor")
    what = {"cve", "victim_org", "malware_family", "tool"}
    return {v.casefold() for t, v in shared if t in what} - {n.casefold() for n in actors}
