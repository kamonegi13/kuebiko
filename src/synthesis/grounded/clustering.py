"""nominate 後に各 claim の裏取りソースをプールから拡張する (決定論・LLM 不使用)。

nominate は 150 タイトルを 1 回で読むため各 claim に 1-4 記事しか紐づけられない
(過少裏取り → ACH が退化)。ここでプールから**同一事案/同一トピック**の記事を entity/keyword の
重なりで決定論的にクラスタし、接地に回す。**判定数 (トピック) は増やさず各判定の裏取りを厚くする**。
設計: docs/synthesis_reliability_redesign.md。
"""

from __future__ import annotations

import re

# 同一「事案」を強く示す entity (共有 = 同一インシデントの可能性大)。
# involved_country / sector は広すぎ (over-cluster) なので anchor に含めない。
_ANCHOR_TYPES: frozenset[str] = frozenset({"cve", "victim_org", "malware_family", "actor"})
# 日本語は分かち書きされないので、ひらがな/カタカナ/漢字を **別クラス**で run 分割する
# (1 クラスに混ぜると助詞まで繋がり文全体が 1 トークンになる)。漢字複合語・カタカナ語が
# 一致すれば同一トピックの強い手掛かり。ひらがなは助詞ノイズが多いので {3,} に絞る。
_TOKEN_RE = re.compile(r"[0-9a-z]{3,}|[ァ-ヶー]{2,}|[一-龠]{2,}|[ぁ-ん]{3,}")
# タイトル重なりで over-cluster させない汎用語 (CTI で頻出だが弁別力が低い)。
_STOP: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "new",
        "attack",
        "cyber",
        "security",
        "malware",
        "ransomware",
        "vulnerability",
        "report",
        "サイバー",
        "攻撃",
        "セキュリティ",
        "脆弱性",
        "による",
        "および",
        "して",
        "した",
        "する",
        "から",
        "発生",
        "確認",
        "対象",
    }
)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


def anchor_entities(entities_by_id: dict[str, set[str]], ids: tuple[str, ...]) -> set[str]:
    """指定記事群の **弁別的 entity** (cve/victim_org/malware/actor) を集める。

    同一事案クラスタリング (プール内) と、過去文脈 retrieval (プール外・同一 entity の
    過去記事を ACH 証拠に引く) の両方の anchor に使う。
    """
    anchors: set[str] = set()
    for sid in ids:
        for e in entities_by_id.get(sid, set()):
            if e.split(":", 1)[0] in _ANCHOR_TYPES:
                anchors.add(e)
    return anchors


def expand_claim_sources(
    *,
    claim_text: str,
    seed_ids: tuple[str, ...],
    pool: list[dict[str, object]],
    entities_by_id: dict[str, set[str]],
    max_sources: int,
) -> list[str]:
    """claim の seed 記事に、プールの関連記事 (entity/keyword 一致) を足して返す (seed 優先・上限)。

    - 強 entity (cve/victim_org/malware/actor) を 1 つでも共有 → 採用 (同一事案)。
    - entity が乏しい claim (地政学等) は claim/seed タイトルとのトークン重なり >= 2 で採用。
    - ランクは entity 一致を優先し、上限 max_sources で打ち切る (over-cluster を cap で抑える)。
    """
    seed_order = list(seed_ids)
    seed_set = set(seed_ids)
    anchors = anchor_entities(entities_by_id, seed_ids)
    title_by_id = {str(a.get("article_id", "")): str(a.get("title", "")) for a in pool}
    claim_tok = _tokens(claim_text)
    for sid in seed_ids:
        claim_tok |= _tokens(title_by_id.get(sid, ""))

    scored: list[tuple[int, str]] = []
    for a in pool:
        aid = str(a.get("article_id", ""))
        if not aid or aid in seed_set:
            continue
        ent_share = sum(1 for e in entities_by_id.get(aid, set()) if e in anchors)
        tok_share = len(_tokens(str(a.get("title", ""))) & claim_tok)
        if ent_share >= 1 or tok_share >= 2:
            scored.append((ent_share * 3 + tok_share, aid))
    # score 降順 → 同点は id 昇順で決定論。
    scored.sort(key=lambda x: (-x[0], x[1]))
    extra = [aid for _, aid in scored]
    return (seed_order + extra)[:max_sources]
