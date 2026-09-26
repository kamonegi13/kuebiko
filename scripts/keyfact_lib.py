"""要点シート評価の共通部品 (2026-09-26)。

事象ニュースの評価に「欠落」の物差しを入れる。PBP (一対比較) は欠落と誤りを 1 つの勝敗に
混ぜ、長さ・位置・自己選好の偏りも受ける。要点ごとの照合に分ける根拠:

- 松田ら「サイバーセキュリティ関連情報のキュレーションに向けた大規模言語モデルによる
  要約生成の評価方式の提案」(情処論文誌 67(3), 2026): 大項目 [前提条件/攻撃/被害/発覚/
  調査/対策] × 小項目の評価シートで **欠落と誤変換を別々に** 判定。両者は独立に起きる
- FineSurE (Song et al., ACL 2024): 要点 (keyfact) ごとの 2 値判定、忠実性は別系統
- Fox et al. "LLM Judges Verify Presence, Not Absence" (arXiv 2608.31016): 審判は欠落を
  ほぼ偶然水準でしか見抜けず、**事実を列挙して 1 件ずつ照合する形でだけ回復した**
- CheckEval (arXiv 2403.18771): 2 値のチェックリストで審判間の一致が上がる

CTI 向けの拡張: 大項目に「脆弱性」(攻撃の無い公表・修正も多い)、要点に「確度」を持たせ、
主張を事実として書く **確度の格上げ** を誤変換の 1 種として数える (断定の問題そのもの)。
逆に、確認済みの評価・事実を「不明」「未検証」へ書き換える **確度の格下げ** も誤変換 (09-26 の点検で
追加、利用者と合意)。格上げは要約中で最も強い書き方で判定する
(BLUF・要点で言い切れば事実欄の留保は効かない)。
"""

from __future__ import annotations

import json
import re
import unicodedata
from math import comb
from typing import Any, Literal

from pydantic import BaseModel, Field

Category = Literal["前提条件", "攻撃", "脆弱性", "被害", "発覚", "調査", "対策"]
Slot = Literal[
    "誰が",
    "誰の",
    "誰に",
    "何をした",
    "いつ",
    "どこで",
    "なぜ",
    "どうやって",
    "どれくらい",
    "その他",
]
Certainty = Literal["確認済み", "主張・報道", "原文が留保"]
Importance = Literal["核心", "補足"]
Distortion = Literal["なし", "内容の誤り", "数値の誤り", "確度の格上げ", "確度の格下げ"]


class KeyFact(BaseModel):
    """記事群が伝えている 1 つの事実 (原子的な粒度)。"""

    category: Category
    slot: Slot
    fact: str = Field(description="事実を 1 文で。1 要点 = 1 事実")
    certainty: Certainty
    importance: Importance
    source_index: int = Field(description="根拠の記事番号 [N] (1 始まり)")
    quote: str = Field(description="根拠の記事本文から逐語で引用 (言い換えない)")


class KeyFactSheet(BaseModel):
    facts: list[KeyFact] = Field(default_factory=list)


class Judgment(BaseModel):
    """1 要点の照合結果。"""

    id: int
    present: bool = Field(description="要約がこの事実を (一部でも) 伝えているか")
    distortion: Distortion
    quote: str = Field(description="present のとき要約から逐語で引用。無ければ空文字")


class CoverageResult(BaseModel):
    judgments: list[Judgment] = Field(default_factory=list)


# ---------- 純粋関数 ----------


def norm(s: str) -> str:
    """照合用の正規化 — 空白と全角半角の違いで正当な引用を落とさない。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def source_block(prompt: str) -> str:
    """事象ニュースの prompt から記事本文の部分だけを切り出す (無ければ全体)。"""
    start = prompt.find("## 対象記事")
    end = prompt.find("## 識別子カタログ", start + 1)
    if start < 0:
        return prompt
    return prompt[start : end if end > start else len(prompt)]


def verified_facts(facts: list[KeyFact], sources: str) -> tuple[list[KeyFact], int]:
    """引用が記事本文に実在する要点だけを残す。返り値 = (残した要点, 捨てた数)。"""
    hay = norm(sources)
    kept = [f for f in facts if f.quote and norm(f.quote) in hay]
    return kept, len(facts) - len(kept)


def render_draft(raw: str | None) -> str:
    """生成 JSON (EventNewsDraft) を審判に見せる本文へ。読者に見える欄だけを並べる。"""
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    lines = [f"見出し: {d.get('headline', '')}", f"BLUF: {d.get('bluf', '')}"]
    for label, key in (("要点", "key_points"), ("不明点", "unknowns")):
        lines += [f"{label}: {x}" for x in d.get(key) or [] if isinstance(x, str)]
    for label, key in (("事実", "facts"), ("相違", "discrepancies"), ("但し書き", "caveats")):
        lines += [f"{label}: {x.get('text', '')}" for x in d.get(key) or [] if isinstance(x, dict)]
    return "\n".join(lines)


def settle(judgments: list[Judgment], n_facts: int, summary: str) -> list[dict[str, Any]]:
    """審判の出力を要点ごとに確定する。

    - 返ってこなかった要点は「含まない」(judge_missing で数える)
    - present でも引用が要約に実在しなければ「含まない」(unverified で数える)
      — 審判が「ある」と言うだけでは被覆に数えない (引用実在の関門、08-22 と同じ思想)
    """
    hay = norm(summary)
    by_id = {j.id: j for j in judgments if 1 <= j.id <= n_facts}
    out: list[dict[str, Any]] = []
    for i in range(1, n_facts + 1):
        j = by_id.get(i)
        if j is None:
            out.append({"id": i, "present": False, "distortion": "なし", "flag": "judge_missing"})
            continue
        ok_quote = bool(j.quote) and norm(j.quote) in hay
        present = j.present and ok_quote
        flag = "unverified" if j.present and not ok_quote else ""
        out.append(
            {
                "id": i,
                "present": present,
                "distortion": j.distortion if present else "なし",
                "flag": flag,
                "quote": j.quote if present else "",
            }
        )
    return out


def mcnemar_p(a_only: int, b_only: int) -> float:
    """対応ありの 2 値の比較 (片方だけが含む要点の数) の両側正確検定。"""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = max(a_only, b_only)
    return float(min(1.0, 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2**n))
