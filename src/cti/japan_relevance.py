"""「日本関連」判定の SSoT (有機的結合監査 H4 の恒久対処)。

4 方言 — routing の厳格判定 (targeted) / ダッシュボード KPI の victim+channel /
threat_operations の channel のみ / salience の言及 (mention) — が画面間の件数矛盾と
「routing が意図的に排除した言及のみ記事に salience が +2.0 の下駄を履かせる」逆転を
生んでいた。行レベル・claim レベルの判定述語をここに集約する。

- routing の厳格判定 (LLM japan_targeted + victim=JP + 事故除外) は BriefingMessage
  文脈専用のため routing_signals.py に残す (このモジュールは post-hoc 判定の SSoT)。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

JAPAN_WATCH_CHANNEL = "japan_watch"

# SQL 文脈用の共通 fragment (articles 単表クエリに埋め込む。列名が曖昧になる JOIN では
# is_japan_targeted_row を Python 側で使う)。overview KPI / threat_operations が同じ
# 定義で数えることで画面間矛盾を排除する。
JAPAN_TARGETED_SQL_FRAGMENT = "(victim_country_iso = 'JP' OR posted_channel = 'japan_watch')"


def is_japan_targeted_row(victim_country_iso: str | None, posted_channel: str | None) -> bool:
    """行レベルの「日本標的」判定 (victim=JP or japan_watch 配信)。

    ダッシュボード KPI / threat_operations / board の共通定義。
    「言及のみ」(mentioned_country) はここに含めない。
    """
    return (victim_country_iso or "").upper() == "JP" or (
        posted_channel or ""
    ) == JAPAN_WATCH_CHANNEL


JapanClaimRelevance = Literal["party", "mention", "none"]


def japan_claim_relevance(claim: str, entity_keys: Iterable[str]) -> JapanClaimRelevance:
    """claim (KeyJudgment) レベルの日本関連度。

    - "party": 日本が当事 — claim 本文が日本を主題にしている (gazetteer) か、
      証拠記事に involved_country:JP (当事国 tag) がある
    - "mention": 証拠記事に mentioned_country:JP (言及のみ) があるだけ —
      routing が意図的に排除する層なので salience でも下駄を履かせない
    - "none": 日本関連なし
    """
    from src.cti.nation_gazetteer import nations_in_text

    keys = set(entity_keys)
    if "involved_country:JP" in keys or "JP" in nations_in_text(claim):
        return "party"
    if "mentioned_country:JP" in keys:
        return "mention"
    return "none"
