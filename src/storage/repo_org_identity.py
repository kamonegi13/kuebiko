"""被害組織名の別名判定のキャッシュ (org_identity_pairs テーブル、2026-10-12)。

名前の組 (照合キーの昇順タプル) ごとに判定の余裕 (``org_identity.SAME_MARGIN`` 以上で同一) を
1 度だけ残す。列名 logit は余裕を保存する。毎時の群化は別 subprocess の
ため、プロセス内キャッシュは効かず、DB に無いと同じ組を毎時判定し直す。判定の問いを変えたら
``form`` を変えて新しい行にする (古い判定を新しい問いの結果と取り違えない)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from src.logging_config import get_logger
from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

_log = get_logger(__name__)

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

    def load_org_alias_dict(self) -> dict[str, str]:
        """別名辞書 (org_alias_dict) の写像 (照合キー → 代表キー)。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT alias_key, canonical_key FROM org_alias_dict").fetchall()
        return {str(r["alias_key"]): str(r["canonical_key"]) for r in rows}

    def replace_org_alias_dict(self, mapping: Mapping[str, str], *, source: str) -> int:
        """``source`` の行を ``mapping`` に置き換える (他の source の行は触らない・奪わない)。

        **両バックエンドで明示の BEGIN/COMMIT の 1 トランザクション**。PG の接続は
        autocommit (``db_backend.connect``) なので、ラッパーの commit/rollback は効かず、
        BEGIN を自分で発行しないと DELETE と各 INSERT が個別に確定してしまう。失敗時は ROLLBACK
        して元の例外を送出する (ROLLBACK 自体が失敗してもログに残し元の例外を優先)。
        他 source が持つ alias_key は DO UPDATE の WHERE で弾き取り込まない。
        戻り値は渡した行数 (弾いた行も含む)。
        """
        ts = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM org_alias_dict WHERE source = ?", (source,))
                for alias_key, canonical_key in mapping.items():
                    conn.execute(
                        "INSERT INTO org_alias_dict (alias_key, canonical_key, source, updated_at)"
                        " VALUES (?, ?, ?, ?)"
                        " ON CONFLICT(alias_key) DO UPDATE SET"
                        " canonical_key = excluded.canonical_key, updated_at = excluded.updated_at"
                        " WHERE org_alias_dict.source = excluded.source",
                        (alias_key[:_KEY_MAX], canonical_key[:_KEY_MAX], source, ts),
                    )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception as rollback_error:  # noqa: BLE001 — 元の例外を優先する
                    _log.error("org_alias_dict_rollback_failed", error=str(rollback_error)[:160])
                raise
        return len(mapping)

    def org_alias_dict_stats(self, *, source: str) -> tuple[int, str | None]:
        """(``source`` の行数, 最新の updated_at)。鮮度の監査用。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM org_alias_dict WHERE source = ?",
                (source,),
            ).fetchone()
        return int(row[0]), (str(row[1]) if row[1] is not None else None)
