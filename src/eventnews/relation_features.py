"""事象ごとの指標 (EventFeatures) を DB から組み立てる (2026-09-27、relations.py の入力)。

事象 = 構成記事の和集合。指標の役割はここで絞る (§11 の「線の属性」):
- 主題アクター: 信頼できる経路 (フィード・見出しの別名・LLM high) で、辞書の group のものだけ
- CVE: 焦点の CVE だけ — その記事の CVE が 3 件以下のとき (多 CVE の記事は列挙で、焦点が無い)
- 被害組織: 表記揺れを畳む (事象の群化と同じ normalize_for_match)
- まとめ: 構成記事の見出しがまとめ系
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from src.eventnews.relations import EventFeatures

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

#: 焦点の CVE とみなす記事あたりの上限
FOCUS_CVE_MAX = 3
_TRUSTED_SOURCES = frozenset({"feed", "feed_match", "title"})
_ENTITY_TYPES = ("malware_family", "tool", "cve", "victim_org")
_CHUNK = 800


def _ts(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def headline_countries(text: str, lookup: dict[str, str]) -> set[str]:
    """見出しに出ている国 (ISO)。英字 2 文字の略号は使わず、英字は語頭一致。"""
    low = text.lower()
    found: set[str] = set()
    for alias, iso in lookup.items():
        if iso in found or (alias.isascii() and len(alias) < 3):
            continue
        if alias.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}", low):
                found.add(iso)
        elif alias in text:
            found.add(iso)
    return found


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def _load(repo: RunHistoryRepository, since: datetime) -> tuple[list[dict[str, Any]], ...]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の集約
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT m.item_id, m.article_id, i.first_reported_at, i.last_reported_at"
                " FROM event_item_members m JOIN event_items i ON i.id = m.item_id"
                " WHERE (i.merged_into IS NULL OR i.merged_into = '') AND i.last_reported_at >= ?",
                (since.isoformat(),),
            ).fetchall()
        ]
        aids = sorted({str(m["article_id"]) for m in members})
        ents: list[dict[str, Any]] = []
        arts: list[dict[str, Any]] = []
        types_ph = ",".join("?" for _ in _ENTITY_TYPES)
        for chunk in _chunks(aids):
            ph = ",".join("?" for _ in chunk)
            ents += [
                dict(r)
                for r in conn.execute(
                    "SELECT article_id, entity_type, value FROM article_entities"  # noqa: S608
                    f" WHERE article_id IN ({ph}) AND entity_type IN ({types_ph})",
                    [*chunk, *_ENTITY_TYPES],
                ).fetchall()
            ]
            arts += [
                dict(r)
                for r in conn.execute(
                    "SELECT article_id, title, subject_actor_ids, subject_actor_source,"  # noqa: S608
                    " subject_actor_confidence, victim_sector_canonical, victim_country_iso"
                    f" FROM articles WHERE article_id IN ({ph})",
                    chunk,
                ).fetchall()
            ]
    return members, ents, arts


def _trusted_subjects(row: dict[str, Any], is_group: Any) -> set[str]:
    source = str(row.get("subject_actor_source") or "")
    conf = str(row.get("subject_actor_confidence") or "")
    if not (source in _TRUSTED_SOURCES or (source == "llm" and conf == "high")):
        return set()
    ids = {s.strip() for s in str(row.get("subject_actor_ids") or "").split(",") if s.strip()}
    return {a for a in ids if is_group(a)}


def load_event_features(
    repo: RunHistoryRepository, *, days: int = 60, now: datetime | None = None
) -> list[EventFeatures]:
    """直近 ``days`` 日に報じられた事象の指標 (統合済みの事象は除く)。"""
    from src.assessment.evidence_verify import normalize_for_match
    from src.assessment.situation_track import registry_is_group
    from src.eventnews.pair_features import _ROUNDUP
    from src.tools.rollup_title import is_rollup_title

    since = (now or datetime.now(UTC)) - timedelta(days=days)
    members, ents, arts = _load(repo, since)
    is_group = registry_is_group()
    actor_names = _actor_names()
    kinds = repo.get_article_kinds(sorted({str(m["article_id"]) for m in members}))

    per_article: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for e in ents:
        per_article[str(e["article_id"])][str(e["entity_type"])].add(str(e["value"]).strip())
    art_by_id: dict[str, dict[str, Any]] = {}
    for a in arts:
        art_by_id.setdefault(str(a["article_id"]), a)

    acc: dict[str, dict[str, Any]] = {}
    for m in members:
        iid, aid = str(m["item_id"]), str(m["article_id"])
        e = acc.setdefault(
            iid,
            {
                "first": _ts(m["first_reported_at"]),
                "last": _ts(m["last_reported_at"]),
                "subjects": set(),
                "malware": set(),
                "tools": set(),
                "cves": set(),
                "victims": set(),
                "sectors": set(),
                "countries": set(),
                "kinds": set(),
                "roundup": False,
                "titles": [],
                "members": [],
            },
        )
        e["members"].append(aid)
        ent = per_article.get(aid, {})
        # アクターの名前・別名は能力に数えない (「Kimsuky」「LockBit 5.0」がマルウェアとしても入り、
        # 同じ攻撃者というだけの事象を「珍しい道具の共有」で結んでいた — 盲検 2026-09-27)
        e["malware"] |= {
            v.lower()
            for v in ent.get("malware_family", set())
            if not _is_actor_name(v, actor_names)
        }
        e["tools"] |= {
            v.lower() for v in ent.get("tool", set()) if not _is_actor_name(v, actor_names)
        }
        cves = {v.upper() for v in ent.get("cve", set())}
        if len(cves) <= FOCUS_CVE_MAX:
            e["cves"] |= cves
        e["victims"] |= {normalize_for_match(v) for v in ent.get("victim_org", set()) if v}
        art = art_by_id.get(aid)
        if art is not None:
            e["subjects"] |= _trusted_subjects(art, is_group)
            if art.get("victim_sector_canonical") not in (None, "", "uncategorized", "other"):
                e["sectors"].add(str(art["victim_sector_canonical"]))
            if art.get("victim_country_iso"):
                e["countries"].add(str(art["victim_country_iso"]).upper())
            title = str(art.get("title") or "")
            e["titles"].append(title)
            if _ROUNDUP.search(title) or is_rollup_title(title):
                e["roundup"] = True
        if kinds.get(aid) and kinds[aid] != "other":
            e["kinds"].add(kinds[aid])
    # まとめの判定に事象の見出しも使う (構成記事の見出しだけだと週刊まとめを取りこぼした)
    headlines = repo.latest_event_versions(list(acc))
    from src.cti.taxonomy_normalizer import load_normalizer

    lookup = dict(load_normalizer().country_lookup)
    for iid, v in headlines.items():
        if _ROUNDUP.search(v.headline) or is_rollup_title(v.headline):
            acc[iid]["roundup"] = True
    # 役割の近似: 被害組織と CVE は **事象の見出しに出ているもの** だけを線にする (盲検 2026-09-27。
    # 抽出された被害組織は言及を含み、eBay / Telegram 等の言及だけで「続報」を作っていた)。
    # 見出しの無い事象 (本文未生成) は構成記事の見出しで代える
    for iid, e in acc.items():
        ver = headlines.get(iid)
        text = normalize_for_match(ver.headline if ver is not None else " ".join(e["titles"]))
        e["victims"] = {x for x in e["victims"] if x and x in text}
        e["cves"] = {c for c in e["cves"] if c.lower() in text.lower()}
        head = (ver.headline if ver is not None else " ".join(e["titles"])).lower()
        e["malware_head"] = {x for x in e["malware"] if x in head}
        e["tools_head"] = {x for x in e["tools"] if x in head}
        e["countries_head"] = headline_countries(head, lookup) & e["countries"]
    return [
        EventFeatures(
            item_id=iid,
            first=v["first"],
            last=v["last"],
            **{
                k: frozenset(v[k])
                for k in (
                    "subjects",
                    "malware",
                    "tools",
                    "cves",
                    "victims",
                    "sectors",
                    "countries",
                    "malware_head",
                    "tools_head",
                    "countries_head",
                    "kinds",
                )
            },
            roundup=bool(v["roundup"]),
            member_ids=tuple(dict.fromkeys(v["members"])),
        )
        for iid, v in acc.items()
    ]


def _actor_names() -> frozenset[str]:
    """アクター辞書の正規名・別名 (小文字)。ランサムのブランド名 (版つき含む) も入る。"""
    from src.cti.actor_normalizer import load_actor_aliases

    names: set[str] = set()
    for a in load_actor_aliases().actors:
        names.add(a.canonical.lower())
        names.update(x.lower() for x in a.aliases)
    return frozenset(n for n in names if n)


def _is_actor_name(value: str, names: frozenset[str]) -> bool:
    """値がアクター名か (「LockBit 5.0」のような版つきのブランド名も、先頭の語で判定)。"""
    v = value.strip().lower()
    if v in names:
        return True
    head = v.rsplit(" ", 1)[0] if any(ch.isdigit() for ch in v.rsplit(" ", 1)[-1]) else v
    return head in names


def actor_helpers() -> tuple[Any, Any]:
    """(アクター → 帰属国, 2 アクターが所属関係にあるか) — 共通の供給元の判定用。"""
    from src.cti.actor_normalizer import load_actor_aliases

    reg = load_actor_aliases()

    def nation_of(actor_id: str) -> str | None:
        a = reg.by_id(actor_id)
        return (a.nation or "").lower() or None if a else None

    def related(x: str, y: str) -> bool:
        ax, ay = reg.by_id(x), reg.by_id(y)
        if ax is None or ay is None:
            return False
        return (
            ax.sponsor_org == y
            or ay.sponsor_org == x
            or (bool(ax.sponsor_org) and ax.sponsor_org == ay.sponsor_org)
        )

    return nation_of, related
