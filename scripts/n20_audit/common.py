"""T3/T4 の共通部品: 置き場・定数・読み取り専用の SQL・jsonl の入出力。

data/ は gitignore。置き場は環境変数 KUEBIKO_DATA_ROOT (既定 = リポジトリの root) で変えられる。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("KUEBIKO_DATA_ROOT") or ROOT)

T3_DIR = DATA_ROOT / "data" / "eval" / "n20_t3"
T4_DIR = DATA_ROOT / "data" / "eval" / "n20_t4"
N20_TEACHER = DATA_ROOT / "data" / "mlx" / "n20_teacher"
GUARD_V2 = N20_TEACHER / "guard_n20_v2.json"
LEAK_ROOTS_V2 = N20_TEACHER / "leak_roots_v2.json"
TR_RAW = N20_TEACHER / "work" / "tr_raw.jsonl"
ACTOR_HOLDOUT = N20_TEACHER / "work" / "actor_sync_holdout.json"

JUDGE = "claude-opus-5-5"
EFFORT = "medium"

#: 標本の seed (契約の pilot の tuning・heldout・heldout2 と重ならない新しい seed、D §4.2)
SEED_T3 = 20261104
SEED_T4 = 20261105

TASKS_T3 = ("event_news", "synthesis", "spotlight")
N_PER_TASK = 30
N_TRANSLATION = 30
#: T4 の層 (チャンクの種類)。母集団は translate 1,826 : actor_sync 128 なので和訳は多めに取る
T4_ALLOCATION = {"translate": 24, "actor_sync": 6}

#: n19 が narrative ティアに入った版 47 系の最初 (app_config_versions version 32)
N19_SINCE = "2026-09-29T03:39:12+00:00"

_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def psql_json(sql: str) -> list[dict[str, Any]]:
    """SELECT を流し、行の list を返す (json_agg で包む)。

    SELECT/WITH 以外は拒否し、接続も ``default_transaction_read_only=on`` にする (二重の防御)。
    """
    if not _SELECT_ONLY.match(sql):
        raise ValueError("SELECT / WITH だけを受け付ける (接続も read-only)")
    wrapped = f"select coalesce(json_agg(t), '[]'::json) from ({sql.strip().rstrip(';')}) t"
    out = subprocess.run(  # noqa: S603 — 引数は固定の list、SQL はコード内の定数
        [
            "docker",
            "exec",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "postgres",
            "psql",
            "-U",
            "kuebiko",
            "-d",
            "kuebiko",
            "-At",
            "-c",
            wrapped,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, Any]] = json.loads(out.stdout)
    return rows


def sql_list(values: Iterable[str]) -> str:
    """SQL の IN (...) 用に文字列をリテラル化する (' は二重化)。"""
    quoted = ["'" + v.replace("'", "''") + "'" for v in values]
    return ",".join(quoted) if quoted else "NULL"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
