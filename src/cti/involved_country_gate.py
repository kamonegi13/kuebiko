"""関与国 (involved_countries) の付けすぎを抑える関門 (2026-10-12)。

背景: i58 の関与国は回収率 94.2% に対し適合率 69.1% (325/470、Opus 正解 150 件、
data/eval/c2d_involved_countries_v1.jsonl)。付けすぎが主因で、
(1) サイバー事案の記事 (定義上 ``[]`` が正) に国が付く、
(2) 並びの後ろの国 (4 番目以降) は本文での言及が薄い列挙で、適合率が 33% 前後まで落ちる。
被害国の関門 (``victim_country_gate``) と同型の、LLM を足さない決定論の後付け関門にする。

規則 (``filter_involved_countries``):
1. カテゴリがサイバー系 (``CYBER_CATEGORIES``) なら全て落とす (定義: サイバー事案では欄は空)。
2. 並びの先頭 ``KEEP_LEADING`` 件は残す (LLM は主要な国を先に挙げる。先頭 3 件の適合率は 88.6%)。
3. 4 件目以降は、本文+要約での言及が ``MIN_MENTIONS`` 回以上ある国だけ残す。
4. 日本 (JP) は 2・3 の対象外 (見逃しの代償が大きいため回収率を優先)。ただし 1 は適用する。

正解集 150 件での値 (全件・調整に使った): 適合率 69.1% → 87.0%、回収率 94.2% → 81.7%。
半分に分けた検証 (選ぶ側/確かめる側を入れ替え 6 通り、確かめ側):
適合率 0.83〜0.91、回収率 0.78〜0.84。

``INVOLVED_COUNTRY_GATE``: ``"1"`` 有効 (既定) / ``"0"`` 無効 (旧挙動) / ``"shadow"`` 落とす国を
ログに出すだけで値は変えない。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from src.cti.nation_gazetteer import country_mention_counts

GATE_ENV = "INVOLVED_COUNTRY_GATE"
MODE_ON = "on"
MODE_OFF = "off"
MODE_SHADOW = "shadow"

#: 関与国の欄が空であるべき記事のカテゴリ (summarizer_rubric の定義: サイバー事案では ``[]``)
CYBER_CATEGORIES: frozenset[str] = frozenset(
    {"breach", "incident", "apt", "malware", "vulnerability", "advisory"}
)
#: 並びの先頭からこの件数までは言及の回数を問わず残す
KEEP_LEADING = 3
#: それ以降の国は、本文+要約でこの回数以上言及されていれば残す
MIN_MENTIONS = 3
#: 日本は回収率優先 (言及の薄さでは落とさない)
_EXEMPT_ISO = "JP"


def gate_mode() -> str:
    """``INVOLVED_COUNTRY_GATE`` を ``on`` / ``off`` / ``shadow`` に解釈する (既定 shadow)。

    既定 shadow の理由 (2026-10-12): 規則を選んだ正解集 (150 件) と測った正解集が同じで、
    半分に分けた確認でも回収率が目標 80% すれすれ (0.785〜0.84)。新しい正解で確かめてから on。
    """
    raw = os.environ.get(GATE_ENV, "shadow").strip().lower()
    if raw in ("0", "off"):
        return MODE_OFF
    if raw in ("1", "on"):
        return MODE_ON
    return MODE_SHADOW


def filter_involved_countries(
    isos: Sequence[str],
    category: str,
    text: str,
    *,
    min_mentions: int = MIN_MENTIONS,
    keep_leading: int = KEEP_LEADING,
) -> list[str]:
    """関与国 (ISO、LLM の並び順) から付けすぎを落とした新しい list を返す (入力は変えない)。

    ``text`` は本文+要約。モード (``gate_mode``) は見ない — 呼び出し側が判断する。
    """
    if category in CYBER_CATEGORIES:
        return []
    counts = country_mention_counts(text)
    return [
        iso
        for index, iso in enumerate(isos)
        if index < keep_leading or iso == _EXEMPT_ISO or counts.get(iso, 0) >= min_mentions
    ]
