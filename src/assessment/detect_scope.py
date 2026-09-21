"""detect へ渡す「既に追跡中の情勢」の絞り込み (2026-09-21)。

一覧 (`active_titles`) の役目は **既に追跡中のものを二重に開かせない**こと。ただし
記事から台帳への割当は決定論の照合 (`assignment.match_situation`) が detect の**前**に
済ませており、detect に届くのは割当に漏れた残余である。つまりこの一覧は照合が
取りこぼした分を拾う **二次的な安全網** で、候補記事と何の重なりも無い情勢を載せても
判断の材料にならない。

⚠ 絞りは **割当より緩く**する。割当と同じ厳しさ (強アンカー 1 個以上) にすると、
残余は定義上それを満たさないので一覧が空になる。

実測 (2026-09-21、追跡中 147 件 = 9,668 tok = detect プロンプトの 63%):

| 基準 | 08-15 | 09-15 | 09-20 |
|---|---|---|---|
| 強アンカーのみ | 16 件 | 11 件 | 6 件 |
| **強 or (国 + 語 1)** | **36 件 (3,233 tok)** | **35 件 (3,435)** | **33 件 (3,221)** |
| 強 or 国+語 or 語 2 | 77 件 | 74 件 | 71 件 |

⚠ 国だけでは残さない — RU-UA のような高頻度ペアで一覧が縮まない (割当側が
``_MIN_NATION_PAIR`` を置いているのと同じ理由)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np

from src.assessment.assignment import _MIN_TOKEN_ONLY_CLAIM


class _HasKeys(Protocol):
    """割当キーを持つもの (ArticleKeys / SituationKeys)。

    ⚠ **読み取り専用 (property) で宣言する** — 可変属性として宣言すると frozen な
    dataclass が構造的に適合しない (mypy が invariant として扱う)。
    """

    @property
    def strong(self) -> frozenset[str]: ...

    @property
    def nations(self) -> frozenset[str]: ...

    @property
    def tokens(self) -> frozenset[str]: ...


class _SituationLike(_HasKeys, Protocol):
    @property
    def row(self) -> object: ...


#: 国も強アンカーも無いときに残す語の重なり。``assignment._MIN_TOKEN_ONLY_CLAIM`` と同値。
#: ⚠ ここを緩めると一覧が膨らみ、厳しくすると下の**上位集合の保証**が崩れる。
TOKEN_ONLY_MIN = _MIN_TOKEN_ONLY_CLAIM


def _is_relevant(sit: _HasKeys, art: _HasKeys) -> bool:
    """割当・claim 照合の **どの規則よりも緩い** 重なり判定。

    ⭐ これが本関数の要点: 決定論の照合 (``match_situation`` / ``match_claim``) が
    成立しうる組は **必ず残る**。照合の規則は

    1. 強アンカー >= 1
    2. 国 >= 2 かつ 語 >= 1
    3. 国 >= 1 かつ 語 >= 2
    4. (claim のみ) 語 >= 3

    で、下の条件はそれぞれ 1 / (国>=1 かつ 語>=1) / 同 / (語>=3) に緩めた上位集合。
    よって **落とした情勢は決定論では一致し得ない** — 一覧を絞っても、照合が拾えた
    はずの二重開設を招かない。
    """
    if sit.strong & art.strong:
        return True
    if sit.nations & art.nations and sit.tokens & art.tokens:
        return True
    return len(sit.tokens & art.tokens) >= TOKEN_ONLY_MIN


def relevant_situation_titles(
    situations: Sequence[_SituationLike], articles: Sequence[_HasKeys]
) -> list[str]:
    """候補記事と重なる情勢の題名だけを、元の順で返す (重複題名は 1 度だけ)。"""
    out: list[str] = []
    seen: set[str] = set()
    for sit in situations:
        title = str(getattr(sit.row, "title", "")).strip()
        if not title or title in seen:
            continue
        if any(_is_relevant(sit, art) for art in articles):
            out.append(title)
            seen.add(title)
    return out


# ---------- 埋込による絞り込み (2026-09-22 に決定論から置き換え) ----------
#
# 全件判定 (422 組・同一事象 39 件) の実測:
#
# | 方式 | AUC | 回収 | 別事象を残す |
# |---|---|---|---|
# | **埋込 題名 × 要約** | **0.960** | 31/33 (94%) | 30/287 (10%) |
# | 埋込 題名 × 見出し | 0.926 | 36/39 (92%) | 132/383 (34%) |
# | 埋込 題名 × 本文 | 0.898 | 36/39 | 151/383 (39%) |
# | 群化 ML (構成記事で代表) | 0.804 | 36/39 | 234/383 (61%) |
# | 決定論 (キーの重なり) | 0.763 | 25/33 (75%) | 69/287 (24%) |
#
# ⭐ **決定論の併用は無意味** — 埋込が落とす同一 2 件は決定論も落とす (決定論だけが拾える
#    同一は 0 件)。和集合は別事象を 10% → 28% に増やすだけ。
# ⭐ **本文は使わない** — 定型文・背景説明・関連記事への言及が共通し、別事象どうしが近づく
#    (別事象の最大余弦が同一の中央を超える)。要約は書式が揃い事象の核だけが残る。
# ⭐ **含意 (implication) は足さない** — 「サプライチェーンが標的」のようにどの事象にも
#    当てはまる文言で、足すと別事象が近づく (AUC 0.960 → 0.935)。

#: 要約埋込との余弦の下限。回収 94% / 別事象 10% の動作点 (実測)。
#: 0.50 は回収が同じで別事象 25%、0.60 は回収 76% まで落ちる。
SUMMARY_THRESHOLD = 0.55
#: 要約埋込が無い記事 (実測 25%) は見出しで代替する。書式が違うので閾値も別。
TITLE_THRESHOLD = 0.45


def relevant_titles_by_embedding(
    *,
    situations: Sequence[tuple[str, str, np.ndarray]],
    candidates: Sequence[tuple[str, np.ndarray, bool]],
) -> list[str]:
    """候補記事のいずれかと意味的に近い情勢の題名を、元の順で返す (純粋関数)。

    Args:
        situations: (situation_id, 題名, 題名の正規化済み埋込)
        candidates: (article_id, 正規化済み埋込, その埋込が要約由来か)
    """
    out: list[str] = []
    seen: set[str] = set()
    for _sid, title, svec in situations:
        name = title.strip()
        if not name or name in seen:
            continue
        for _aid, avec, is_summary in candidates:
            threshold = SUMMARY_THRESHOLD if is_summary else TITLE_THRESHOLD
            if float(np.dot(svec, avec)) >= threshold:
                out.append(name)
                seen.add(name)
                break
    return out
