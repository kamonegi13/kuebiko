"""S26-ML-GOLD の母集団 (prereg §2)。単位 = 記事 (detect・sevc・deep_dive) か事象の対 (relation)。

本番 DB は読むだけ。LLM・埋込は呼ばない。
"""

# ruff: noqa: E501
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scripts.s26_ml_gold import core, db

#: relation のラベルに渡す構成記事の数 (事象ごと、事象時刻の早い順)
REL_MEMBERS = 3


@dataclass(frozen=True)
class Unit:
    """標本の単位。``articles`` は汚染・教師の照合と本文の sha に使う記事。"""

    key: str
    stratum: str
    articles: tuple[str, ...]
    extra: dict[str, Any] = field(default_factory=dict)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def detect_units() -> list[Unit]:
    start, end = _iso(core.WINDOW_START), _iso(core.WINDOW_END)
    ids = db.window_article_ids(
        start, end, "AND a.status = 'posted' AND a.importance IN ('high','medium')"
    )
    arts = db.fetch_articles(ids)
    shadow = db.detect_shadow_ids(start, end)
    out = []
    for a in sorted(ids):
        s = core.detect_stratum(in_shadow=a in shadow, importance=str(arts[a]["importance"]))
        if s is not None:
            out.append(Unit(a, s, (a,)))
    return out


def sevc_units() -> list[Unit]:
    start, end = _iso(core.WINDOW_START), _iso(core.WINDOW_END)
    ids = db.window_article_ids(start, end, "AND a.status = 'posted'")
    p = db.sevc_latest_p(ids)
    return [Unit(a, core.sevc_stratum(p[a]), (a,), {"p_s3": p[a]}) for a in sorted(p)]


def relation_units(repo: Any) -> tuple[list[Unit], dict[str, int]]:
    """候補の対 (本番の _candidate_pairs) のうち、遅い方の事象の first が窓に入る対。"""
    from src.eventnews.relation_features import load_event_features
    from src.eventnews.relations import _INDEXED, WINDOW_DAYS, _candidate_pairs, _df, _Stats

    events = load_event_features(repo, days=WINDOW_DAYS, now=core.WINDOW_END)
    stats = _Stats({attr: _df(events, attr) for attr in _INDEXED})
    pairs = sorted(_candidate_pairs(events, stats))
    counts = {"events": len(events), "candidate_pairs": len(pairs)}
    out: list[Unit] = []
    for i, j in pairs:
        a, b = events[i], events[j]
        if a.item_id == b.item_id or not core.in_window(max(a.first, b.first)):
            continue
        s = core.relation_stratum(
            shared_victims_or_cves=bool(
                stats.rare("victims", a.victims & b.victims) | stats.rare("cves", a.cves & b.cves)
            ),
            shared_capability=bool(
                stats.rare("malware", a.malware & b.malware)
                | stats.rare("tools", a.tools & b.tools)
            ),
            shared_subjects=bool(a.subjects & b.subjects),
        )
        if s is None:
            continue
        out.append(
            Unit(
                core.pair_key(a.item_id, b.item_id),
                s,
                tuple(sorted(set(a.member_ids) | set(b.member_ids))),
                {
                    "a": a.item_id,
                    "b": b.item_id,
                    "members_a": list(a.member_ids),
                    "members_b": list(b.member_ids),
                },
            )
        )
    counts["pairs_in_window"] = len(out)
    return out, counts


def pick_members(members: list[str], arts: dict[str, dict[str, Any]]) -> list[str]:
    """ラベルに渡す構成記事 (最大 REL_MEMBERS 本)。本文のある記事を事象時刻の早い順 (同時刻は id 順) に先に、
    足りなければ見出しだけの記事 (暴露サイトの掲載の記録など、本文を持たない型) で埋める (prereg §10)。"""

    def order(m: str) -> tuple[str, str]:
        return (str(arts[m].get("event_ts") or ""), m)

    known = [m for m in members if m in arts]
    with_body = sorted((m for m in known if (arts[m].get("body") or "").strip()), key=order)
    title_only = sorted(
        (
            m
            for m in known
            if not (arts[m].get("body") or "").strip() and (arts[m].get("title") or "").strip()
        ),
        key=order,
    )
    return (with_body + title_only)[:REL_MEMBERS]


def deep_dive_units(repo: Any, window_no: int) -> tuple[list[Unit], dict[str, Any]]:
    """窓の候補 (本番の Stage 0+1+2、幅 180) を順位つきで。"""
    from src.digest.db_filter import fetch_for_deep_dive_candidates

    del repo  # 接続は db_backend.connect の read-only の差し替えを通る (open_repo が先に済ませる)
    end = core.DD_WINDOW_ENDS[window_no - 1]
    keys = db.f1_keys_before(_iso(end))
    pref = fetch_for_deep_dive_candidates(
        lookback_hours=int(core.DD_LOOKBACK.total_seconds() // 3600),
        now=end,
        novelty_excluded_dedup_keys=keys or None,
        pool_max=core.DD_POOL_MAX,
    )
    out = [
        Unit(
            c.article_id,
            core.dd_stratum(rank),
            (c.article_id,),
            {"rank": rank, "window_no": window_no, "window_end": _iso(end)},
        )
        for rank, c in enumerate(pref.candidates, start=1)
    ]
    meta = {
        "window_no": window_no,
        "window_end": _iso(end),
        "stage_counts": dict(pref.stage_counts),
        "novelty_keys": len(keys),
    }
    return out, meta
