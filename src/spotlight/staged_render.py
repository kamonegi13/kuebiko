"""多段 Spotlight の決定論の描画 (n20 T5、旗 ``SPOTLIGHT_STAGED``、利用者判断 U-A)。

- (c) 推奨は LLM に書かせず、主要事象の候補記事から**対応表** (``RECOMMENDATION_TABLE``) で描く。
  材料の外の推奨 (「JPCERT・NISC 等との連携」) を書けと命じて捏造に数えていた矛盾 (設計 D §1.1) を、
  形で消す。表に当たらなければ (c) は出さない。
- (d) 上位の文脈は外す (描かない)。
- 情報源の階層 (SNS・国営メディア・不明) の留保は、候補の ``reliability`` から定型文で描く。
純粋関数のみ。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

#: (規則 id, 照合 (候補の title + summary), 描く文)。上から順に、当たったものを全て描く。
RECOMMENDATION_TABLE: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "kev",
        re.compile(r"\bKEV\b|Known Exploited Vulnerabilit", re.IGNORECASE),
        "CISA KEV に登載された脆弱性が含まれる。該当製品の修正の適用状況を確認する。",
    ),
    (
        "exploited",
        re.compile(r"exploited in the wild|actively exploited|悪用が確認|実際の悪用|悪用されて"),
        "実際の悪用が報じられた脆弱性がある。修正の適用と侵害の痕跡の確認を優先する。",
    ),
    (
        "cve",
        re.compile(r"CVE-\d{4}-\d{4,}"),
        "CVE が付いた脆弱性が含まれる。影響を受ける製品が資産台帳にあるかを確認する。",
    ),
    (
        "japan",
        re.compile(r"日本|国内|Japan", re.IGNORECASE),
        "日本の組織・利用者に関わる事案がある。関係組織への通知・共有の要否を判断する。",
    ),
    (
        "ransomware",
        re.compile(r"ランサム|ransomware", re.IGNORECASE),
        "ランサムウェアの事案がある。バックアップの隔離と復旧手順を点検する。",
    ),
)
#: ATT&CK 手法が抽出済みの主要事象があるときの定型文 ({ids} は最大 _TTP_SHOWN 件)
TTP_RECOMMENDATION = "抽出された ATT&CK 手法 ({ids}) に対応する検知の有無を確認する。"
_TTP_SHOWN = 5

#: 情報源の階層 → 留保の定型文 (official / research / news は付けない)
TIER_CAVEAT: Mapping[str, str] = {
    "social": "SNS 等の投稿",
    "state_media": "国営メディア",
    "unknown": "出所の階層が不明な情報源",
}
_TITLE_SHOWN = 40

ASPECT_HEADINGS: Mapping[str, str] = {
    "a": "(a) 今期のパターン",
    "b": "(b) 来週の指標",
    "c": "(c) 推奨 (定型の対応表)",
}


def _text(c: Mapping[str, Any]) -> str:
    return f"{c.get('title', '')}\n{c.get('summary', '')}"


def recommendations(chosen: Sequence[Mapping[str, Any]]) -> list[str]:
    """(c) 推奨: 主要事象の候補記事に当たった規則の文 (表の順、重複なし)。"""
    out = [
        line for _, pat, line in RECOMMENDATION_TABLE if any(pat.search(_text(c)) for c in chosen)
    ]
    ttps = list(dict.fromkeys(t for c in chosen for t in c.get("ttps") or []))
    if ttps:
        out.append(TTP_RECOMMENDATION.format(ids=", ".join(ttps[:_TTP_SHOWN])))
    return out


def source_caveats(chosen: Sequence[tuple[int, Mapping[str, Any]]]) -> list[str]:
    """情報源の階層の留保 (候補の ``reliability`` から)。``chosen`` は (index, 候補)。"""
    out: list[str] = []
    for index, c in chosen:
        label = TIER_CAVEAT.get(str(c.get("reliability", "")))
        if label:
            title = str(c.get("title", ""))[:_TITLE_SHOWN]
            out.append(f"[{index}]「{title}」は{label}に基づく (裏付けは未確認)。")
    return out


def render_outlook(points: Sequence[tuple[str, str]], recs: Sequence[str]) -> str:
    """見通しの本文: 観点 a・b は LLM の要点、(c) は対応表。(d) は描かない。"""
    blocks: list[str] = []
    for aspect in ("a", "b"):
        texts = [t.strip() for a, t in points if a == aspect and t.strip()]
        if texts:
            blocks.append(f"{ASPECT_HEADINGS[aspect]}: " + " ".join(texts))
    if recs:
        blocks.append(f"{ASPECT_HEADINGS['c']}: " + " ".join(recs))
    return "\n\n".join(blocks)
