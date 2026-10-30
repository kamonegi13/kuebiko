# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の判定 v2 の影の記録 (``geo_rescue_shadow`` テーブル、2026-10-31)。

本番の救済 (flat) は変えない。救済の候補 1 件 = 1 行。2 段で埋める:

1. **triage の時点で写すだけ** (``record_geo_rescue_pending``): flat の判定・手掛かりの種類 (決定論)・
   判定の入力 (題・本文プレビュー)・その sha256・救済の prompt の選択。LLM は呼ばない (状態 ``pending``)
2. **毎時保守チェーンの段で判定する** (``list_pending`` → ``update_judgement``): pir・QW/QC/QD・v2 の判定・
   使ったモデルと digest。72 時間を過ぎた ``pending`` は ``expired`` (判定しない) にする

事前登録 ``docs/replay_prereg/geo_rescue_shadow.md``。

**判定の入力 (題と本文プレビュー ≤ 1,500 字) と購読ソースの名前・url を持つ** (正解付けの入力と、
判定の段で記事を組み直すため)。ローカル (Tier2) の DB にだけ置く: 公開面の書き出し
(``scripts/export_public_site.py`` / ``scripts/export_mirror.py``) はこの表を読まず、集計 API は件数だけを返し
(本文を返さない)、公開の GET の許可リストにも入れない。**ログには出さない**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 180
DEFAULT_LIST_LIMIT = 50
#: 本文プレビューの上限 (triage の本文プレビューと同じ 1,500 字。念のため保存の側でも切る)
INPUT_TEXT_MAX = 1500
#: 題の上限
TITLE_MAX = 500

#: 行の状態。``ok`` 以外は v2 の判定が無い (``judged`` は None)
STATUS_PENDING = "pending"  # triage の時点で写しただけ (判定の段を待つ)
STATUS_OK = "ok"
STATUS_ERROR = "error"  # pir の判定か問いが読めなかった
STATUS_EXPIRED = "expired"  # 判定の段が 72 時間以内に処理できなかった (判定しない)
STATUSES = (STATUS_PENDING, STATUS_OK, STATUS_ERROR, STATUS_EXPIRED)


@dataclass(frozen=True)
class GeoRescueShadowRow:
    """影の記録 1 件 (救済の候補 1 件)。"""

    article_id: str
    flat: str  # 本番の救済の判定 (high/medium/low/error)
    status: str
    prompt_mode: str = "flat"  # 本番の救済の prompt の選択 (flat / pir)
    pir: str | None = None  # pir の判定 (high/medium/low/error)。問わなかったら None
    cue_types: tuple[str, ...] = ()  # 語の関門の手掛かりの種類 (nation / theatre / cyber_geo 等)
    p_qw: float | None = None
    p_qc: float | None = None
    p_qd: float | None = None
    judged: bool | None = None  # v2 の最終判定 (status=ok のときだけ)
    model: str | None = None  # pir の判定に使ったモデル (救済のモデル)
    model_digest: str | None = None
    judge_model: str | None = None  # 問いに使ったモデル (Step.GEO_RESCUE_JUDGE の解決先)
    judge_model_digest: str | None = None
    flat_error: bool = False
    pir_error: bool = False
    judge_failed: bool = False
    input_kind: str | None = None  # body / summary (判定の入力の種類)
    input_sha256: str | None = None  # 題 + 本文プレビューの sha256 (正解付けの入力の照合用)
    title: str | None = None  # 判定が見た題 (ローカルの DB にだけ。ログに出さない)
    input_text: str | None = None  # 判定が見た本文プレビュー (≤ INPUT_TEXT_MAX 字)
    feed_title: str | None = None  # 判定の段で記事を組み直すため (prompt にフィード名が入る)
    url: str | None = None
    id: int | None = None
    created_at: str = ""
    judged_at: str | None = None


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _opt_str(v: Any) -> str | None:
    return None if v is None else str(v)


_COLUMNS = (
    "article_id, created_at, flat, prompt_mode, pir, status, cue_types, p_qw, p_qc, p_qd, judged,"
    " model, model_digest, judge_model, judge_model_digest, flat_error, pir_error, judge_failed,"
    " input_kind, input_sha256, title, input_text, feed_title, url, judged_at"
)


def _to_row(r: Any) -> GeoRescueShadowRow:
    types = str(r["cue_types"] or "")
    return GeoRescueShadowRow(
        id=int(r["id"]),
        article_id=str(r["article_id"]),
        created_at=str(r["created_at"]),
        flat=str(r["flat"]),
        prompt_mode=str(r["prompt_mode"] or "flat"),
        pir=_opt_str(r["pir"]),
        status=str(r["status"]),
        cue_types=tuple(t for t in types.split(",") if t),
        p_qw=_opt_float(r["p_qw"]),
        p_qc=_opt_float(r["p_qc"]),
        p_qd=_opt_float(r["p_qd"]),
        judged=_opt_bool(r["judged"]),
        model=_opt_str(r["model"]),
        model_digest=_opt_str(r["model_digest"]),
        judge_model=_opt_str(r["judge_model"]),
        judge_model_digest=_opt_str(r["judge_model_digest"]),
        flat_error=bool(r["flat_error"]),
        pir_error=bool(r["pir_error"]),
        judge_failed=bool(r["judge_failed"]),
        input_kind=_opt_str(r["input_kind"]),
        input_sha256=_opt_str(r["input_sha256"]),
        title=_opt_str(r["title"]),
        input_text=_opt_str(r["input_text"]),
        feed_title=_opt_str(r["feed_title"]),
        url=_opt_str(r["url"]),
        judged_at=_opt_str(r["judged_at"]),
    )


def _values(r: GeoRescueShadowRow, ts: str) -> tuple[Any, ...]:
    return (
        r.article_id,
        ts,
        r.flat,
        r.prompt_mode,
        r.pir,
        r.status,
        ",".join(r.cue_types),
        r.p_qw,
        r.p_qc,
        r.p_qd,
        _flag(r.judged),
        r.model,
        r.model_digest,
        r.judge_model,
        r.judge_model_digest,
        int(r.flat_error),
        int(r.pir_error),
        int(r.judge_failed),
        r.input_kind,
        r.input_sha256,
        None if r.title is None else r.title[:TITLE_MAX],
        None if r.input_text is None else r.input_text[:INPUT_TEXT_MAX],
        r.feed_title,
        r.url,
        r.judged_at,
    )


class GeoRescueShadowMixin(RunHistoryRepositoryBase):
    """``geo_rescue_shadow`` テーブルの読み書き。"""

    def record_geo_rescue_shadow(self: Any, rows: Sequence[GeoRescueShadowRow]) -> int:
        """記録をまとめて追記する (append-only。triage の時点の ``pending`` もこれで書く)。空入力は 0。"""
        if not rows:
            return 0
        ts = _to_iso(datetime.now(UTC))
        values = [_values(r, ts) for r in rows]
        marks = ", ".join("?" for _ in range(len(values[0])))
        with self._connect() as conn:
            conn.executemany(f"INSERT INTO geo_rescue_shadow ({_COLUMNS}) VALUES ({marks})", values)
        return len(values)

    def list_geo_rescue_shadow(self: Any, *, days: int) -> list[GeoRescueShadowRow]:
        """直近 ``days`` 日の記録を古い順に返す (集計・標本の枠に使う)。"""
        sql = (
            f"SELECT id, {_COLUMNS} FROM geo_rescue_shadow WHERE created_at >= ?"
            " ORDER BY created_at, id"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_cutoff(days),)).fetchall()
        return [_to_row(r) for r in rows]

    def list_pending_geo_rescue_shadow(self: Any, *, limit: int) -> list[GeoRescueShadowRow]:
        """判定を待つ行を古い順に最大 ``limit`` 件。"""
        sql = (
            f"SELECT id, {_COLUMNS} FROM geo_rescue_shadow WHERE status = ?"
            " ORDER BY created_at, id LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (STATUS_PENDING, int(limit))).fetchall()
        return [_to_row(r) for r in rows]

    def update_geo_rescue_judgement(self: Any, row: GeoRescueShadowRow) -> None:
        """判定の段の結果を書き込む (``pending`` の行だけ。二重の書き込みを防ぐ)。"""
        if row.id is None:
            raise ValueError("id の無い行は更新できない")
        with self._connect() as conn:
            conn.execute(
                "UPDATE geo_rescue_shadow SET pir = ?, status = ?, p_qw = ?, p_qc = ?, p_qd = ?,"
                " judged = ?, model = ?, model_digest = ?, judge_model = ?, judge_model_digest = ?,"
                " pir_error = ?, judge_failed = ?, judged_at = ? WHERE id = ? AND status = ?",
                (
                    row.pir,
                    row.status,
                    row.p_qw,
                    row.p_qc,
                    row.p_qd,
                    _flag(row.judged),
                    row.model,
                    row.model_digest,
                    row.judge_model,
                    row.judge_model_digest,
                    int(row.pir_error),
                    int(row.judge_failed),
                    _to_iso(datetime.now(UTC)),
                    row.id,
                    STATUS_PENDING,
                ),
            )

    def expire_geo_rescue_shadow(self: Any, *, older_than_hours: float) -> int:
        """``pending`` のまま ``older_than_hours`` を過ぎた行を ``expired`` にする (判定しない)。"""
        cutoff = _to_iso(datetime.now(UTC) - timedelta(hours=older_than_hours))
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE geo_rescue_shadow SET status = ? WHERE status = ? AND created_at < ?",
                (STATUS_EXPIRED, STATUS_PENDING, cutoff),
            )
            return int(cur.rowcount or 0)

    def purge_geo_rescue_shadow(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM geo_rescue_shadow WHERE created_at < ?", (_cutoff(days),)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "INPUT_TEXT_MAX",
    "STATUSES",
    "STATUS_ERROR",
    "STATUS_EXPIRED",
    "STATUS_OK",
    "STATUS_PENDING",
    "GeoRescueShadowMixin",
    "GeoRescueShadowRow",
]
