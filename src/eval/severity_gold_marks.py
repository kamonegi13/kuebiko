# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さの正解集 (severity_gold_v4) の評価側の除外の印 (2026-10-22)。

まとめ記事 (週刊まとめ・KEV の週報など、定期の複数事象の要約) には深刻さを付けない
(docs/importance_relevance_redesign.md §4.1。中の個々の事象のほうで評価する)。正解集は版 .6 の
Opus 裁定でこれを S3 としているが、**正解集のファイルは書き換えず**、評価・学習の側でここの印で外す。
"""

from __future__ import annotations

#: 正解集でまとめ記事が S3 になっている 4 件 (分類が roundup の 3 件と、KEV の前週分の週報)。
#: 分類 (``article_kinds``) が付いていなくても外せるよう id で持つ。
ROUNDUP_GOLD_S3_IDS: frozenset[str] = frozenset(
    {
        "rss:https://thecyberexpress.com/?p=113953",
        "rss:https://thehackernews.com/2026/09/weekly-recap-cisco-0-day-ai-agent-rce.html",
        "rss:https://www.security-next.com/?p=189606",
        "rss:https://www.helpnetsecurity.com/?p=382373",
    }
)
ROUNDUP_KIND = "roundup"


def gold_exclusion(article_id: str, event_kind: str) -> str | None:
    """評価・学習から外す理由 (外さなければ None)。

    分類が roundup の記事は S3 以外 (対象外 = 正解も「付けない」) でも、深刻さを付けない型として
    外す。KEV の週報のように分類が roundup でない 1 件は id で外す。
    """
    if event_kind == ROUNDUP_KIND or article_id in ROUNDUP_GOLD_S3_IDS:
        return "roundup"
    return None
