"""言及タグの決定論導出 (mentioned_country / campaign) — ingest 時のタギング補完。

タグ体系の多角的棚卸し (STIX 2.1 / MISP / OpenCTI 突合、2026-07-03) で確定した 2 gap を埋める:

1. **mentioned_country** (STIX location の「言及」次元): involved_country (LLM 抽出の当事国)
   はプール記事の 4 割超で未記入。国名はタイトル/要約に明示されるため gazetteer で決定論回収。
   **当事国 (involved_country) と type を分離**し意味論を混ぜない (mention ≠ 当事、
   actor/actor_provisional と同じ provenance 分離原則)。既に当事国として付与済みの ISO は
   重複付与しない (involvement ⊃ mention)。
2. **campaign** (STIX Campaign SDO 相当): 命名された作戦 (Operation Endgame 等) は
   Situation の強い identity anchor だが未抽出だった。"Operation <Name>" 型のみ regex で
   高精度に拾う (「〜作戦」型は 陽動作戦/特別軍事作戦 等の一般語と弁別できず不採用 = 精度優先)。

LLM プロンプトには手を入れない (26B の field 脱落前科 — 過負荷を避け決定論で足す)。
消費者: Situation Ledger 割当 (campaign=強anchor / mentioned_country=国)、検索 facet。
国家情勢ボード/国家相関への言及タグ導入は面ごとの別判断 (正直ラベル必須)。
"""

from __future__ import annotations

import re

from src.cti.nation_gazetteer import nations_in_text

# "Operation <Name>" (英 1-2 語) と「オペレーション・<名>」。後続の一般語は作戦名でない。
_OP_EN_RE = re.compile(r"\bOperation\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)?)")
_OP_JP_RE = re.compile(r"オペレーション[・:：]?\s*([A-Za-z][A-Za-z0-9]+|[ァ-ヶー]{3,})")
# "Security Operation Center" / "Operation Technology" 等の一般名詞連結を弾く blocklist。
_OP_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "center",
        "centers",
        "centre",
        "centres",
        "technology",
        "technologies",
        "manager",
        "managers",
        "management",
        "system",
        "systems",
        "support",
        "team",
        "teams",
        "room",
        "officer",
        "mode",
        "guide",
    }
)


def campaigns_in_text(text: str) -> frozenset[str]:
    """命名された作戦 (campaign) を "Operation <Name>" 正規形の集合で返す (決定論)。"""
    if not text:
        return frozenset()
    found: set[str] = set()
    for m in _OP_EN_RE.finditer(text):
        name = m.group(1).strip()
        first_word = name.split()[0].lower()
        if first_word in _OP_GENERIC_WORDS:
            continue
        # 2 語目が一般語なら 1 語目のみ採る (例: "Operation Endgame Team" → Endgame)
        words = name.split()
        if len(words) == 2 and words[1].lower() in _OP_GENERIC_WORDS:
            name = words[0]
        found.add(f"Operation {name}")
    for m in _OP_JP_RE.finditer(text):
        name = m.group(1).strip()
        if name.lower() not in _OP_GENERIC_WORDS:
            found.add(f"Operation {name}")
    return frozenset(found)


def derive_mention_entities(
    *, title: str, summary: str, involved_isos: set[str]
) -> list[tuple[str, str]]:
    """(entity_type, value) の言及タグ一覧を導出する (ingest / backfill 共用)。

    involved_isos: 当該記事に involved_country として付与済みの ISO (小文字)。
    当事国は言及を包含するため mentioned_country を重複付与しない。
    """
    text = f"{title or ''}\n{summary or ''}"
    out: list[tuple[str, str]] = []
    for iso in sorted(nations_in_text(text)):
        if iso.lower() not in involved_isos:
            out.append(("mentioned_country", iso))
    for name in sorted(campaigns_in_text(text)):
        out.append(("campaign", name))
    return out
