"""被害組織名の別名判定のキャッシュ (org_identity_pairs テーブル、2026-10-12)。

名前の組 (照合キーの昇順タプル) ごとに判定の余裕 (``org_identity.SAME_MARGIN`` 以上で同一) を
1 度だけ残す。列名 logit は余裕を保存する。毎時の群化は別 subprocess の
ため、プロセス内キャッシュは効かず、DB に無いと同じ組を毎時判定し直す。判定の問いを変えたら
``form`` を変えて新しい行にする (古い判定を新しい問いの結果と取り違えない)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

#: 問いの版。``org_identity`` の問いかしきい値を変えたらここも上げる。
FORM_VERSION = "and_event12_org-4_v2"
_KEY_MAX = 200


class OrgIdentityMixin(RunHistoryRepositoryBase):
    """org_identity_pairs テーブルの読み書き。"""

    def get_org_identity_logits(
        self, keys: list[tuple[str, str]], *, form: str = FORM_VERSION
    ) -> dict[tuple[str, str], float]:
        """判定済みの名前の組 → logit。"""
        if not keys:
            return {}
        out: dict[tuple[str, str], float] = {}
        with self._connect() as conn:
            for key_a, key_b in keys:
                row = conn.execute(
                    "SELECT logit FROM org_identity_pairs"
                    " WHERE key_a = ? AND key_b = ? AND form = ?",
                    (key_a[:_KEY_MAX], key_b[:_KEY_MAX], form),
                ).fetchone()
                if row is not None:
                    out[(key_a, key_b)] = float(row["logit"])
        return out

    def put_org_identity_logits(
        self, rows: dict[tuple[str, str], float], *, form: str = FORM_VERSION
    ) -> int:
        """判定を保存する (同じ組・同じ form は上書き)。"""
        if not rows:
            return 0
        ts = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            for (key_a, key_b), logit in rows.items():
                conn.execute(
                    "INSERT INTO org_identity_pairs (key_a, key_b, form, logit, judged_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(key_a, key_b, form) DO UPDATE SET"
                    " logit = excluded.logit, judged_at = excluded.judged_at",
                    (key_a[:_KEY_MAX], key_b[:_KEY_MAX], form, float(logit), ts),
                )
        return len(rows)

    def list_org_identity_same_pairs(
        self, *, form: str = FORM_VERSION, min_logit: float = 0.0
    ) -> list[tuple[str, str]]:
        """同一と判定済みの名前の組 (余裕が ``min_logit`` 以上)。焦点の照合の別名の材料。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key_a, key_b FROM org_identity_pairs WHERE form = ? AND logit >= ?",
                (form, float(min_logit)),
            ).fetchall()
        return [(str(r["key_a"]), str(r["key_b"])) for r in rows]
