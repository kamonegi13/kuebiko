"""ニュース由来 alias 収穫 (アクター辞書 Phase2 F3)。

記事本文の「X (aka Y)」「X, also known as Y」「X(別名: Y)」併記から、辞書アクター X の
未収録別名 Y を検出し、**既存の人承認提案インフラ** (actor_update_proposals) へ還流する。

3 層統治の原則 (2026-07-26 確定) どおり identity への昇格は人承認のみ — ここは提案を
作るだけで辞書には書かない。誤収穫の防御は 4 重: ①aka 併記構文のみ (単独出現は拾わない)
②直前 80 字に辞書アクターが必要 ③一般語 SSoT (generic_alias_words) で除外
④既知名 (knows_name) は除外。それでも最後は人承認が門。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.cti.actor_normalizer import (
    ActorAliasRegistry,
    _alias_pattern,
    load_actor_aliases,
)
from src.cti.generic_alias_words import is_generic_alias
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

PROPOSAL_TYPE_NEWS_ALIAS = "news_alias"

# 週次実行 (mitre-actor-sync 相乗り) + 余裕 1 日
_WINDOW_DAYS = 8

# LIKE prefilter 用マーカー (search_recent_articles_by_names に渡す。"aka" 単独は
# osaka 等に誤爆するため括弧/ピリオド付きの形に限定)
AKA_LIKE_MARKERS: tuple[str, ...] = (
    "also known as",
    "also tracked as",
    "formerly known as",
    "a.k.a",
    "(aka ",
    "別名",
)

# 本文中の aka マーカー (厳密照合)。直後に別名リストが続く
_AKA_MARKER_RE = re.compile(
    r"(?:a\.?k\.?a\.?|also\s+known\s+as|also\s+tracked\s+as|tracked\s+as|"
    r"formerly\s+known\s+as|別名)\s*[::]?\s*",
    re.IGNORECASE,
)

# 候補名の形: 英大文字/数字開頭、英数・空白・ハイフン・ドット、3-40 字
_NAME_RE = re.compile(r"[A-Z0-9][\w\-. ]{1,38}[A-Za-z0-9]")

# マーカー前方でアクターを探す窓 / 後方で別名リストを読む窓 (字数)
_BEFORE_WINDOW = 80
_AFTER_WINDOW = 80

# 大文字開頭の語 (未知の主体名の可能性)。アクター名の最後の出現からマーカーまでの間に
# これが挟まる場合は帰属しない — 実データで「MOIS-linked OilRig subgroup Lyceum (aka
# Hexane)」の Lyceum (辞書未収録) に付く別名を遠くの既知アクターへ誤帰属した対策。
_CAPITALIZED_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+")

# 1 マーカーから拾う別名の上限 (長い列挙はノイズ源)
_MAX_NAMES_PER_MARKER = 3


@dataclass(frozen=True)
class AliasCandidate:
    """1 記事から検出した (アクター, 未収録別名) の組。"""

    actor_id: str
    alias: str
    excerpt: str


def _clean_candidate(raw: str) -> str:
    return raw.strip().strip("\"'“”「」『』()").strip()


def extract_alias_candidates(text: str, registry: ActorAliasRegistry) -> list[AliasCandidate]:
    """テキストから aka 併記の未収録別名候補を決定論的に抽出する。"""
    if not text:
        return []
    out: list[AliasCandidate] = []
    seen: set[tuple[str, str]] = set()
    for m in _AKA_MARKER_RE.finditer(text):
        # 前方窓に辞書アクターがいることが条件 (「誰の別名か」の帰属)
        before = text[max(0, m.start() - _BEFORE_WINDOW) : m.start()]
        actor = registry.find(before)
        if actor is None or actor.is_merged:
            continue
        # 帰属妥当性ガード: アクター名の最後の出現からマーカーまでの間に別の大文字語が
        # 挟まるなら、別名は直近の別主体 (辞書未収録) のものの可能性が高い → 帰属しない
        last_end = max(
            (mm.end() for name in actor.all_names for mm in _alias_pattern(name).finditer(before)),
            default=-1,
        )
        if last_end < 0 or _CAPITALIZED_TOKEN_RE.search(before[last_end:]):
            continue
        # 後方窓: 閉じ括弧/文末までを別名リストとして読む
        after = text[m.end() : m.end() + _AFTER_WINDOW]
        after = re.split(r"[)\]。;\n]", after, maxsplit=1)[0]
        names = re.split(r",|、|/| and ", after)[:_MAX_NAMES_PER_MARKER]
        for raw in names:
            name = _clean_candidate(raw)
            if not name or not _NAME_RE.fullmatch(name):
                continue
            if registry.knows_name(name):
                continue  # 既知名 (自他問わず) は収穫しない
            if is_generic_alias(name):
                continue  # 一般語 SSoT — 2026-07-21 型の汚染を取込側で遮断
            key = (actor.id, name.lower())
            if key in seen:
                continue
            seen.add(key)
            excerpt = text[max(0, m.start() - 60) : m.end() + 60]
            out.append(
                AliasCandidate(
                    actor_id=actor.id,
                    alias=name,
                    excerpt=" ".join(excerpt.split())[:160],
                )
            )
    return out


def propose_news_aliases(
    repo: RunHistoryRepository,
    *,
    registry: ActorAliasRegistry | None = None,
    window_days: int = _WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, int]:
    """直近 window の body 現存記事から alias 候補を集計し、人承認提案を作る。

    dedup_key (news_alias:{actor}:{alias}) により同一候補は status 問わず再提案しない
    (却下済みの再出現も抑止 — corpus_emerging_actor と同じ規約)。
    """
    registry = registry or load_actor_aliases()
    since = (now or datetime.now(UTC)) - timedelta(days=window_days)
    rows = repo.search_recent_articles_by_names(list(AKA_LIKE_MARKERS), since)
    # (actor_id, alias_lower) → evidence 集計 (記事は article_id で dedup)
    evidence: dict[tuple[str, str], dict[str, object]] = {}
    seen_articles: set[str] = set()
    for row in rows:
        aid = str(row["article_id"])
        if aid in seen_articles:
            continue
        seen_articles.add(aid)
        text = f"{row['title']}\n{row['body']}"
        for cand in extract_alias_candidates(text, registry):
            key = (cand.actor_id, cand.alias.lower())
            ev = evidence.setdefault(
                key,
                {
                    "alias": cand.alias,
                    "article_ids": [],
                    "titles": [],
                    "excerpt": cand.excerpt,
                },
            )
            ids = ev["article_ids"]
            titles = ev["titles"]
            assert isinstance(ids, list) and isinstance(titles, list)
            if len(ids) < 5:
                ids.append(aid)
                titles.append(str(row["title"])[:120])

    proposed = 0
    skipped_dup = 0
    for (actor_id, alias_lower), ev in sorted(evidence.items()):
        dedup_key = f"news_alias:{actor_id}:{alias_lower}"
        if repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS, dedup_key=dedup_key
        ):
            skipped_dup += 1
            continue
        actor = registry.by_id(actor_id)
        canonical = actor.canonical if actor else actor_id
        ids = ev["article_ids"]
        assert isinstance(ids, list)
        payload = {
            "actor_id": actor_id,
            "actor_canonical": canonical,
            "alias": ev["alias"],
            "_evidence": {
                "article_count": len(ids),
                "sample_article_ids": ids,
                "sample_titles": ev["titles"],
                "excerpt": ev["excerpt"],
            },
        }
        repo.insert_actor_update_proposal(
            run_id=None,
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS,
            mitre_group="",
            dedup_key=dedup_key,
            actor_id=actor_id,
            payload=json.dumps(payload, ensure_ascii=False),
            rationale=(
                f"報道で {canonical} の別名として「{ev['alias']}」が併記されました"
                f" ({len(ids)} 記事)。承認すると alias に追加され、直近 90 日の記事を"
                "再帰属します。"
            ),
        )
        proposed += 1
    stats = {
        "articles_scanned": len(seen_articles),
        "candidates": len(evidence),
        "proposed": proposed,
        "skipped_dup": skipped_dup,
    }
    _log.info("news_alias_harvest_done", **stats)
    return stats
