"""s26 の要約の importance の付け直し (docs/replay_prereg/s26_importance_relabel.md) の共通部品。

判定役の指示 (§3)・出力の形・まとめ記事の除外・入出力の補助を持つ。純関数が中心で、DB にも
Opus にも触れない (Opus は opus.py、判定の手順は protocol.py)。

data/ は gitignore のため、置き場は環境変数 KUEBIKO_DATA_ROOT (既定 = リポジトリの root) で変えられる。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("KUEBIKO_DATA_ROOT") or ROOT)

PREREG = "s26_importance_relabel"
RUBRIC_ID = "s26-imp-v8r7"
JUDGE = "claude-opus-5-5"
EFFORT_GOLD = "high"
EFFORT_TEACHER = "medium"
BATCH_N = 4
BATCH_CHARS = 48_000
BODY_MAX = 40_000
MAX_TOKENS = 3000
CONCURRENCY = 4
RETRIES = 3  # 判定の失敗は 3 回再試行してから「失敗」に数える (§2.1)

SEED_GOLD = 20261102
SEED_GOLD_2 = 20261103
SEED_TEACHER = (20261104, 20261105, 20261106)

RUBRIC_V8R7_REL = "data/eval/S24A_rubric_v8r7.json"
DATASET_DRY_REL = "data/mlx/dataset_s26_dry"
GUARD_S26_REL = "data/mlx/s26_teacher/guard_s26.json"
PLAN_REL = "data/eval/plan_ledger/plan.yaml"
LEDGER_REL = "data/eval/adjudication_ledger.jsonl"
LOCK_REL = "data/eval/adjudication.lock"

IMPORTANCES = ("high", "medium", "low")
ConfirmedBy = Literal["被害者", "当局", "独立研究者", "主張のみ", "無し"]
ArticleForm = Literal["事案の報道", "分析", "勧告", "まとめ", "その他"]

QUESTION = "次の記事それぞれの importance を、上の基準だけに照らして判定してください。"

_PREAMBLE = (
    "あなたは CTI アナリストで、要約の教師データの importance を付け直す裁定者です。各記事の**本文全体**を読み、"
    "事象そのものの重さ (平たい深刻さ) だけで importance を判定します。日本・特定の国・常設の情報要求 (SIR) への"
    "該当などの**関連性では上げ下げしません**。記事どうしは独立に判定し、同じ回の他の記事と比べて相対的に決めません。\n\n"
)
_OUTPUT = (
    "## 出力\n"
    "- 入力の記事ごとに 1 つ (id はそのまま写す)。importance は high / medium / low\n"
    "- criterion: 当てはめた基準の項目を短く (例「high-1」「medium: 単一組織の侵害」「low: 暴露サイトへの掲載のみ」)\n"
    "- reason: 判定理由を 150 字以内で。本文の事実だけを書く\n"
    "- confirmed_victim_orgs: 本文で確認された被害組織の数 (整数、数えられなければ null)。"
    "攻撃者の主張だけの組織は数えない\n"
    "- confirmed_by: 被害の確認の出どころ (被害者 / 当局 / 独立研究者 / 主張のみ / 無し)\n"
    "- article_form: 記事の形 (事案の報道 / 分析 / 勧告 / まとめ / その他)\n"
    "- confirmed_victim_orgs・confirmed_by・article_form は importance を決める材料にしない "
    "(importance は上の基準だけで決める)\n"
)
_OUTPUT_BOUNDARY = (
    "- boundary: 上の基準の文からどちらとも言える (結果が変わる) とき true、そうでなければ false\n"
)


class ItemVerdict(BaseModel):
    id: str
    importance: Literal["high", "medium", "low"]
    criterion: str
    reason: str
    confirmed_victim_orgs: int | None
    confirmed_by: ConfirmedBy
    article_form: ArticleForm


class BatchVerdict(BaseModel):
    items: list[ItemVerdict]


class ItemVerdictBoundary(ItemVerdict):
    boundary: bool


class BatchVerdictBoundary(BaseModel):
    items: list[ItemVerdictBoundary]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_importance_section(path: Path | None = None) -> str:
    """v8r7 の importance 節の本文 (逐語)。"""
    p = path or (DATA_ROOT / RUBRIC_V8R7_REL)
    sections = json.loads(p.read_text("utf-8"))["sections"]
    matches = [s["body"] for s in sections if s["field_id"] == "importance"]
    if len(matches) != 1:
        raise RuntimeError(f"importance 節が 1 つでない: {len(matches)}")
    body: str = matches[0]
    if not body.strip():
        raise RuntimeError("importance 節が空")
    return body


def build_instruction(importance_body: str, *, with_boundary: bool = False) -> str:
    """判定役の指示。importance 節の本文は v8r7 の逐語 (§3)。"""
    section = (
        "## importance の基準 (要約の基準 v8r7 の importance 節から逐語)\n"
        f"{importance_body.rstrip()}\n\n"
    )
    out = _OUTPUT + (_OUTPUT_BOUNDARY if with_boundary else "")
    return f"{_PREAMBLE}{section}{out}"


def rubric_for_cache(instruction: str) -> str:
    return f"{instruction}\n<!-- {RUBRIC_ID} -->"


def instruction_sha(instruction: str) -> str:
    return sha256_text(rubric_for_cache(instruction))


def is_roundup_row(*titles: str | None) -> bool:
    """見出し・DB の title・original_title のどれかがまとめ記事の見出しの型に当たれば True (§6)。"""
    sys.path.insert(0, str(ROOT))
    from src.cti.roundup_detector import roundup_basis

    return any(t and roundup_basis(t) == "title" for t in titles)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").split("\n") if x.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    """書いて sha256 を返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    path.write_text(text, "utf-8")
    return sha256_text(text)


def make_item(article_id: str, title: str, feed: str, body: str) -> dict[str, str]:
    return {"id": article_id, "title": title, "feed": feed, "body": body[:BODY_MAX]}


def batch_items(
    items: Sequence[dict[str, Any]], *, size: int = BATCH_N, chars: int = BATCH_CHARS
) -> list[list[dict[str, Any]]]:
    """4 件まで・本文の合計 chars 字までで順に区切る (s25 と同じ)。"""
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    total = 0
    for it in items:
        n = len(it["body"])
        if cur and (len(cur) >= size or total + n > chars):
            out.append(cur)
            cur, total = [], 0
        cur.append(it)
        total += n
    if cur:
        out.append(cur)
    return out


def batch_inputs(group: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """Opus に渡す入力と 札 → 記事 id。id は短い札 (記事1..) で渡す (長い id の写し間違いの対策)。"""
    labels = {f"記事{i + 1}": g["id"] for i, g in enumerate(group)}
    arts = [
        {"id": lab, "title": g["title"], "feed": g["feed"], "body": g["body"]}
        for lab, g in zip(labels, group, strict=True)
    ]
    return {"articles": arts}, labels


def question_for_pass(pass_no: int) -> str:
    """2 回目以降は札を足す (同じ問い・同じ入力だとキャッシュが同じ答えを返し独立にならない)。"""
    return QUESTION if pass_no <= 1 else f"{QUESTION} [判定 {pass_no} 回目]"


def run_gate(caller: Path, *, dry_run: bool, out_dir: Path | None = None) -> Any:
    """prereg の関門 (scripts/replay/_gate)。dry-run は失敗しても警告にとどめる (None を返す)。"""
    from datetime import UTC, datetime

    sys.path.insert(0, str(ROOT))
    from scripts.replay._gate import GateRefusedError, require_prereg

    try:
        return require_prereg(
            PREREG, ROOT, run_started_at=datetime.now(UTC), caller=caller, out_dir=out_dir
        )
    except GateRefusedError as exc:
        if not dry_run:
            raise
        print(f"[dry-run] prereg の関門は通らない (本番の実行では拒否される): {exc}")
        return None


def event_ts_expr() -> str:
    sys.path.insert(0, str(ROOT))
    from src.storage.event_time import EVENT_TS_EXPR

    return str(EVENT_TS_EXPR.format(a="a"))


def open_db() -> Any:
    """read-only セッションの DB 接続 (scripts/replay の open_readonly)。"""
    sys.path.insert(0, str(ROOT))
    from scripts.replay._common import open_readonly

    return open_readonly(os.environ["DATABASE_URL"], allow_owner_connection=True)


def fetch_latest(c: Any, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """article_id → 最新の 1 行 (title, original_title, feed_title, body, importance, category)。"""
    rows = c.execute(
        "SELECT a.article_id, a.title, a.original_title, a.feed_title, a.importance, a.category,"
        " a.body, a.created_at FROM articles a WHERE a.article_id = ANY(?) ORDER BY a.created_at",
        (list(ids),),
    ).fetchall()
    return {r["article_id"]: r for r in rows}


def verify_body_sha(row: Mapping[str, Any], expected: str) -> bool:
    return sha256_text(row.get("body") or "") == expected


def git_head() -> str:
    import subprocess

    return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
