"""GROK-LINK-JUDGE の標本の定義 (純粋部分 + DB 読み出し)。仕様は docs/replay_prereg/grok_link_fetch.md。

- 母集団 (§2.2 の SQL)・除外の段ごとの件数 (§2.3)・R18 の閉包 (§7)・seed 20261107 の 40 件抽出 (§4)
- DB は SELECT のみ (読み取り専用接続)。記事本文はこのモジュールから書き出さない
"""

# ruff: noqa: E501 (日本語の説明行は幅 2 で数えられる)

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
SEED_DRAW = 20261107
SAMPLE_SIZE = 40
DRAW_NOT_BEFORE = datetime(2026, 11, 7, 0, 0, tzinfo=JST)
WINDOW_START = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)  # 2026-10-25 00:00 JST
WINDOW_END = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)  # 2026-11-06 24:00 JST (排他)
RECOVERED_AFTER_HOURS = 48
FOCUS_KEY_MAX = 3  # relation_features.FOCUS_CVE_MAX と同じ (ハブの欄は鍵にしない)
MAX_COMPONENT_SHARE = 0.30
X_EPOCH_MS = 1288834974657
_STATUS_RE = re.compile(r"/status/([0-9]+)")
_DIGITS = re.compile(r"^[0-9]+$")
_CHUNK = 500


class DrawTooEarlyError(RuntimeError):
    """標本の窓が閉じる (2026-11-07 00:00 JST) 前の抽出。"""


class NotEnoughCandidatesError(RuntimeError):
    """候補が 40 未満 (§6.4 の無効)。"""


def status_id_of(url: str) -> str | None:
    m = _STATUS_RE.search(url or "")
    return m.group(1) if m else None


def posted_at(status_id: str) -> datetime:
    """X の id の上位ビットから求めた投稿の時刻 (UTC、§2.1)。"""
    return datetime.fromtimestamp(((int(status_id) >> 22) + X_EPOCH_MS) / 1000.0, tz=UTC)


def assert_draw_allowed(now: datetime, *, allow_early: bool) -> None:
    if now < DRAW_NOT_BEFORE and not allow_early:
        raise DrawTooEarlyError(
            f"標本の窓が閉じる前 ({DRAW_NOT_BEFORE.isoformat()} より前)。"
            "テスト用は --allow-early-for-test (結果を使わない)"
        )


def excluded_status_ids(sample_rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """gold:grok_link_44 の各行の url から status_id (全行にあること)。"""
    ids: list[str] = []
    for row in sample_rows:
        sid = status_id_of(str(row.get("url", "")))
        if sid is None:
            raise ValueError("gold:grok_link_44 に status_id の無い行がある")
        ids.append(sid)
    return ids


def population_sql(excluded: Sequence[str]) -> str:
    """§2.2 の SQL。除外の VALUES は数字だけを検証して埋める (パラメータ化できない CTE のため)。"""
    if not excluded or any(not _DIGITS.match(s) for s in excluded):
        raise ValueError("excluded は数字だけの status_id の非空の列")
    values = ",".join(f"('{s}')" for s in sorted(set(excluded)))
    return f"""
WITH excl(status_id) AS (VALUES {values}),
rows AS (
  SELECT a.id, a.article_id, a.url, a.created_at, a.published_at, a.linked_context,
         substring(a.url from '/status/([0-9]+)') AS status_id
  FROM articles a
  WHERE a.body_source = 'grok' AND a.linked_context IS NOT NULL
),
first_row AS (
  SELECT DISTINCT ON (status_id) * FROM rows
  WHERE status_id IS NOT NULL
  ORDER BY status_id, created_at, id
),
post AS (
  SELECT f.*,
    to_timestamp((((f.status_id::bigint) >> 22) + 1288834974657) / 1000.0) AS posted_at,
    EXISTS (SELECT 1 FROM jsonb_array_elements(f.linked_context::jsonb) p
            WHERE (p->>'ok')::boolean AND COALESCE(p->>'text', '') <> ''
              AND p->>'skip_reason' IS NULL)                          AS gate_ok,
    f.status_id IN (SELECT status_id FROM excl)                       AS excluded44
  FROM first_row f
)
SELECT status_id, article_id, created_at, posted_at,
       (created_at > posted_at + interval '{RECOVERED_AFTER_HOURS} hours') AS recovered
FROM post
WHERE posted_at >= '2026-10-24 15:00+00'
  AND posted_at <  '2026-11-06 15:00+00'
  AND created_at < '2026-11-06 15:00+00'
  AND gate_ok
  AND NOT excluded44
ORDER BY status_id
"""


#: 段ごとの件数用 (§2.3)。本文は引かない。1 行 = Grok の linked_context のある articles の行
STAGE_SQL = """
SELECT a.id, a.article_id, a.url, a.created_at,
  EXISTS (SELECT 1 FROM jsonb_array_elements(a.linked_context::jsonb) p
          WHERE (p->>'ok')::boolean AND COALESCE(p->>'text', '') <> ''
            AND p->>'skip_reason' IS NULL) AS gate_ok,
  EXISTS (SELECT 1 FROM jsonb_array_elements(a.linked_context::jsonb) p
          WHERE (p->>'ok')::boolean) AS any_ok
FROM articles a
WHERE a.body_source = 'grok' AND a.linked_context IS NOT NULL
"""


def _as_utc(value: Any) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def stage_counts(rows: Sequence[Mapping[str, Any]], excluded: Iterable[str]) -> dict[str, int]:
    """§2.3 の段ごとの件数 (畳み → 窓 → 保存の上限 → 関門 → gold44)。行は STAGE_SQL の列。"""
    excl = set(excluded)
    counts: Counter[str] = Counter()
    counts["rows_with_linked_context"] = len(rows)
    first: dict[str, Mapping[str, Any]] = {}
    for r in sorted(rows, key=lambda r: (_as_utc(r["created_at"]), r["id"])):
        sid = status_id_of(str(r["url"]))
        if sid is None:
            counts["no_status_id"] += 1
        elif sid in first:
            counts["folded_duplicate_rows"] += 1
        else:
            first[sid] = r
    counts["posts_after_fold"] = len(first)
    in_window = {s: r for s, r in first.items() if WINDOW_START <= posted_at(s) < WINDOW_END}
    counts["posts_outside_window"] = len(first) - len(in_window)
    stored = {s: r for s, r in in_window.items() if _as_utc(r["created_at"]) < WINDOW_END}
    counts["posts_stored_after_cap"] = len(in_window) - len(stored)
    gated = {s: r for s, r in stored.items() if r["gate_ok"]}
    counts["posts_no_gate_passing_page"] = len(stored) - len(gated)
    counts["posts_no_gate_but_fetch_ok"] = sum(
        1 for s, r in stored.items() if not r["gate_ok"] and r["any_ok"]
    )
    kept = {s for s in gated if s not in excl}
    counts["posts_in_gold44"] = len(gated) - len(kept)
    counts["candidates_before_r18"] = len(kept)
    return dict(counts)


@dataclass(frozen=True)
class Candidate:
    status_id: str
    article_id: str
    recovered: bool


def candidates_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Candidate]:
    return [
        Candidate(str(r["status_id"]), str(r["article_id"]), bool(r["recovered"])) for r in rows
    ]


def limit_hub_keys(
    values_by_article: Mapping[str, Iterable[str]], max_n: int = FOCUS_KEY_MAX
) -> dict[str, frozenset[str]]:
    """記事あたり max_n 件を超える欄 (CVE・被害組織) は鍵にしない (§7 のハブの切り方)。"""
    out: dict[str, frozenset[str]] = {}
    for aid, vals in values_by_article.items():
        s = frozenset(v for v in vals if v)
        if 0 < len(s) <= max_n:
            out[aid] = s
    return out


def r18_filter(
    candidates: Sequence[Candidate],
    gold44_article_ids: Iterable[str],
    component_of: Mapping[str, str],
) -> tuple[list[Candidate], list[Candidate]]:
    """gold44 と同じ成分に入る候補を外す。返り値 = (残り, 外した)。"""
    gold_components = {component_of.get(a, a) for a in gold44_article_ids}
    kept: list[Candidate] = []
    removed: list[Candidate] = []
    for c in candidates:
        (
            removed if component_of.get(c.article_id, c.article_id) in gold_components else kept
        ).append(c)
    return kept, removed


def draw(
    candidates: Sequence[Candidate], *, seed: int = SEED_DRAW, n: int = SAMPLE_SIZE
) -> list[Candidate]:
    """status_id の数値の昇順に並べ、random.Random(seed).sample で n 件 (§4 の 3)。"""
    if len(candidates) < n:
        raise NotEnoughCandidatesError(f"候補 {len(candidates)} 件 < {n}")
    ordered = sorted(candidates, key=lambda c: int(c.status_id))
    return random.Random(seed).sample(ordered, n)


# ---- DB 読み出し (SELECT のみ) --------------------------------------------------------


def _chunks(items: Sequence[str]) -> list[list[str]]:
    return [list(items[i : i + _CHUNK]) for i in range(0, len(items), _CHUNK)]


def load_r18_inputs(conn: Any, article_ids: Sequence[str]) -> dict[str, Any]:
    """build_components の入力。事象の根は merged_into を辿った最新の根 (複数所属は最小の根)。"""
    import numpy as np

    from scripts.replay._closure import resolve_roots
    from src.assessment.evidence_verify import normalize_for_match

    ids = sorted(set(article_ids))
    merged = {
        str(r["id"]): (str(r["merged_into"]) if r["merged_into"] else None)
        for r in conn.execute("SELECT id, merged_into FROM event_items").fetchall()
    }
    roots = resolve_roots(merged)
    root_of: dict[str, str] = {}
    cves: dict[str, set[str]] = {}
    orgs: dict[str, set[str]] = {}
    embeddings: dict[str, Any] = {}
    for chunk in _chunks(ids):
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT article_id, item_id FROM event_item_members WHERE article_id IN ({ph})",  # noqa: S608
            tuple(chunk),
        ).fetchall():
            root = roots.get(str(r["item_id"]), str(r["item_id"]))
            aid = str(r["article_id"])
            root_of[aid] = min(root, root_of.get(aid, root))
        for r in conn.execute(
            "SELECT article_id, entity_type, value FROM article_entities"  # noqa: S608
            f" WHERE entity_type IN ('cve','victim_org') AND article_id IN ({ph})",
            tuple(chunk),
        ).fetchall():
            aid, kind, val = str(r["article_id"]), str(r["entity_type"]), str(r["value"]).strip()
            if kind == "cve":
                cves.setdefault(aid, set()).add(val.upper())
            elif val:
                orgs.setdefault(aid, set()).add(normalize_for_match(val))
        for r in conn.execute(
            f"SELECT article_id, vector, dim FROM summary_embeddings WHERE article_id IN ({ph})",  # noqa: S608
            tuple(chunk),
        ).fetchall():
            vec = np.frombuffer(bytes(r["vector"]), dtype=np.float32)
            if vec.size == int(r["dim"]):
                embeddings[str(r["article_id"])] = vec
    return {
        "root_of": root_of,
        "cves": limit_hub_keys(cves),
        "victim_orgs": limit_hub_keys(orgs),
        "embeddings": embeddings,
    }
