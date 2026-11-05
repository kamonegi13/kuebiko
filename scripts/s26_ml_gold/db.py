"""S26-ML-GOLD の DB の読み出し (すべて read-only)。

- SQL は ``docker exec postgres psql`` を read-only セッションで (THIN-GOLD・DETECT-REJUDGE と同じ)
- relation の事象と deep_dive の候補は本番の関数を使うため、ホストの Python から read-only の接続で読む
  (``DATABASE_URL`` を 127.0.0.1:5433 に向ける。ctor の DDL を避け、接続を read-only にして SHOW で確かめる)
"""

# ruff: noqa: E501, S608
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

LATEST = (
    "latest AS (SELECT DISTINCT ON (article_id) article_id, title, feed_title, body, status,"
    " importance, created_at, published_at FROM articles"
    " ORDER BY article_id, created_at DESC, id DESC)"
)


def psql_rows(sql: str) -> list[dict[str, Any]]:
    """read-only セッションの psql で SELECT/WITH を流し、1 行 1 JSON を返す。"""
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("read-only SELECT/WITH only")
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "postgres",
            "psql",
            "-U",
            "kuebiko",
            "-d",
            "kuebiko",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


def values(items: Sequence[str]) -> str:
    return ",".join("('" + s.replace("'", "''") + "')" for s in items)


def chunks(items: Sequence[str], n: int) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def event_ts(alias: str = "a") -> str:
    from src.storage.event_time import EVENT_TS_EXPR

    return "(" + EVENT_TS_EXPR.format(a=alias) + ")"


def fetch_articles(ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """記事 id → 最新行 (見出し・出典・本文・公開日・事象時刻・状態・importance)。"""
    out: dict[str, dict[str, Any]] = {}
    for ch in chunks(sorted(set(ids)), 3000):
        sql = f"""WITH {LATEST}, ids(article_id) AS (VALUES {values(ch)})
SELECT row_to_json(t) FROM (SELECT a.article_id, a.title, a.feed_title, a.body, a.status, a.importance,
 a.published_at, {event_ts()} AS event_ts FROM latest a JOIN ids USING (article_id)) t;"""
        out.update({str(r["article_id"]): r for r in psql_rows(sql)})
    return out


def window_article_ids(start: str, end: str, extra_where: str = "") -> list[str]:
    sql = f"""WITH {LATEST}
SELECT row_to_json(t) FROM (SELECT a.article_id FROM latest a
 WHERE {event_ts()} >= '{start}' AND {event_ts()} < '{end}' {extra_where}) t;"""
    return [str(r["article_id"]) for r in psql_rows(sql)]


def detect_shadow_ids(start: str, end: str) -> set[str]:
    sql = f"""SELECT row_to_json(t) FROM (SELECT DISTINCT article_id FROM detect_ml_shadow
 WHERE run_at >= '{start}' AND run_at < '{end}') t;"""
    return {str(r["article_id"]) for r in psql_rows(sql)}


def sevc_latest_p(ids: Sequence[str]) -> dict[str, float]:
    """記事 → 問いを当てた最新の severity_c_shadow の p_s3 (skip_reason IS NULL の行)。"""
    out: dict[str, float] = {}
    for ch in chunks(sorted(set(ids)), 3000):
        sql = f"""WITH ids(article_id) AS (VALUES {values(ch)})
SELECT row_to_json(t) FROM (SELECT DISTINCT ON (s.article_id) s.article_id, s.p_s3
 FROM severity_c_shadow s JOIN ids USING (article_id)
 WHERE s.skip_reason IS NULL AND s.p_s3 IS NOT NULL ORDER BY s.article_id, s.created_at DESC, s.id DESC) t;"""
        out.update({str(r["article_id"]): float(r["p_s3"]) for r in psql_rows(sql)})
    return out


def f1_keys_before(end: str) -> set[str]:
    sql = f"""SELECT row_to_json(t) FROM (SELECT DISTINCT dedup_key FROM f1_selections
 WHERE selected_at < '{end}' AND dedup_key IS NOT NULL) t;"""
    return {str(r["dedup_key"]) for r in psql_rows(sql) if r["dedup_key"]}


def members_of_items(item_ids: Sequence[str]) -> dict[str, set[str]]:
    """事象 id → 構成記事 (その事象と、その事象へ統合された事象の構成記事を含む)。"""
    items = psql_rows("SELECT row_to_json(t) FROM (SELECT id, merged_into FROM event_items) t;")
    parent = {str(r["id"]): str(r["merged_into"] or "") for r in items}
    want = set(item_ids)
    # 事象 i の祖先 (自身を含む、merged_into を辿る) に鍵の事象があれば、i の記事を鍵の事象の記事に数える
    owners: dict[str, set[str]] = {}
    for i in parent:
        cur, seen = i, set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur in want:
                owners.setdefault(i, set()).add(cur)
            cur = parent.get(cur, "")
    out: dict[str, set[str]] = {k: set() for k in want}
    rel = sorted(owners)
    for ch in chunks(rel, 5000):
        sql = f"""WITH ids(item_id) AS (VALUES {values(ch)})
SELECT row_to_json(t) FROM (SELECT m.item_id, m.article_id FROM event_item_members m JOIN ids USING (item_id)) t;"""
        for r in psql_rows(sql):
            for target in owners.get(str(r["item_id"]), ()):
                out[target].add(str(r["article_id"]))
    return out


def host_database_url() -> str:
    """本番コンテナの DATABASE_URL のホスト部をホストの公開口 (127.0.0.1:5433) に替えたもの (表示しない)。"""
    proc = subprocess.run(  # noqa: S603
        ["docker", "exec", "kuebiko", "printenv", "DATABASE_URL"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return re.sub(r"@[^/]+/", "@127.0.0.1:5433/", proc.stdout.strip(), count=1)


def open_repo() -> Any:
    """read-only の RunHistoryRepository (ctor の DDL を避け、SHOW で read-only を確かめる)。"""
    sys.path.insert(0, str(ROOT / "scripts" / "gpu_window"))
    import detect_common as dc

    dc.install_read_only_env()
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = host_database_url()
    from src.storage import db_backend
    from src.storage.run_history import DEFAULT_DB_PATH, RunHistoryRepository

    if not os.environ.get("DATABASE_URL") or not db_backend.is_pg_enabled():
        raise SystemExit(
            "DATABASE_URL が無い。export DATABASE_URL=$(docker exec kuebiko printenv DATABASE_URL"
            " | sed -E 's#@[^/]+/#@127.0.0.1:5433/#')"
        )
    dc.prefer_read_only_role(os.environ)
    dc.install_read_only_connect()
    repo = RunHistoryRepository.__new__(RunHistoryRepository)
    repo._db_path = Path(DEFAULT_DB_PATH)  # noqa: SLF001
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute("SHOW default_transaction_read_only").fetchone()
        val = next(iter(row.values())) if hasattr(row, "values") else row[0]
        dc.assert_read_only_setting(val)
    return repo
