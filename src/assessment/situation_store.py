"""Situation Ledger (状況台帳) の永続層 (段A)。

Situation = 継続追跡される 1 つの評価可能な情勢 (docs/synthesis_situation_ledger_design.md §2)。
本モジュールは純粋な CRUD (LLM を呼ばない)。schema は run_history._SCHEMA (idempotent) に同居し、
PG/SQLite の dialect 差は db_backend.translate_sql が吸収する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from src.assessment.evidence_verify import excerpt_is_supported
from src.logging_config import get_logger
from src.storage.run_history import DEFAULT_DB_PATH, RunHistoryRepository

_log = get_logger(__name__)

SituationStatus = Literal["active", "dormant", "closed"]
ClaimType = Literal["ongoing_activity", "discrete_event", "structural"]
DeltaType = Literal[
    "opened",
    "strengthened",
    "weakened",
    "hypothesis_flip",
    "escalated",
    "claim_revised",
    "reopened",
    "no_change",
    "closing",
]
AssignedBy = Literal["seed", "anchor", "nation", "token", "llm", "standing"]

# anchors の肥大 (identity 汚染) を防ぐ上限。超過時は既存を優先し新規を捨てる。
_MAX_ANCHORS = 24
_MAX_PIR_IDS = 8
# add_revision の UNIQUE(situation_id, rev) 衝突 retry 上限 (並行採番の微小窓のみ想定)
_ADD_REVISION_MAX_ATTEMPTS = 3


def _is_unique_violation(e: Exception) -> bool:
    """UNIQUE 制約違反かを backend 非依存で判定する (add_revision の retry 判定)。"""
    import sqlite3

    if isinstance(e, sqlite3.IntegrityError):
        return "unique" in str(e).lower()
    try:
        from psycopg import errors as pg_errors
    except ImportError:  # pragma: no cover — PG 非導入環境 (SQLite のみ)
        return False
    return isinstance(e, pg_errors.UniqueViolation)


@dataclass(frozen=True)
class SituationRow:
    """situations テーブルの 1 行 (frozen)。anchors は "type:value" キーの集合。"""

    situation_id: str
    title: str
    domain: str
    status: SituationStatus
    anchors: frozenset[str]
    pir_ids: tuple[str, ...]
    opened_at: str
    last_evidence_at: str
    closed_at: str | None = None
    # 段A (prepositioning posture): 'event'=従来 / 'standing'=常設情報要求
    # (dormant/close 対象外・汎用 matcher 非対象・評価は段B まで除外)
    kind: str = "event"


@dataclass(frozen=True)
class RevisionRow:
    """situation_revisions の 1 行 (frozen)。judgment のスナップショット + delta。"""

    situation_id: str
    rev: int
    claim: str
    claim_type: str
    leading_hypothesis: str
    confidence: str
    confidence_basis: str
    hypotheses_json: str
    assumptions_json: str
    missing_json: str
    indicators_json: str
    implication: str
    delta_type: DeltaType
    delta_note: str
    created_at: str
    run_id: str = ""


def situation_id_for(title: str) -> str:
    """title から決定論的な situation_id を生成 (空白除去 + 小文字化 + sha1 短縮)。"""
    norm = "".join((title or "").lower().split())
    return "s-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — id 用途


def _row_to_situation(r: Any) -> SituationRow:
    return SituationRow(
        situation_id=str(r["situation_id"]),
        title=str(r["title"]),
        domain=str(r["domain"]),
        status=cast(SituationStatus, str(r["status"])),
        anchors=frozenset(json.loads(str(r["anchors"] or "[]"))),
        pir_ids=tuple(json.loads(str(r["pir_ids"] or "[]"))),
        opened_at=str(r["opened_at"]),
        last_evidence_at=str(r["last_evidence_at"]),
        closed_at=str(r["closed_at"]) if r["closed_at"] else None,
        kind=str(r["kind"] or "event"),
    )


class SituationStore:
    """状況台帳 CRUD。RunHistoryRepository の接続 seam (PG/SQLite 透過) を再利用する。"""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        # schema は RunHistoryRepository が idempotent に保証する (situations 系も同 _SCHEMA)
        self._repo = RunHistoryRepository(db_path=db_path)

    # ---------- situations ----------

    def load_situations(
        self, statuses: tuple[SituationStatus, ...] = ("active", "dormant")
    ) -> list[SituationRow]:
        ph = ",".join("?" for _ in statuses)
        with self._repo._connect() as conn:  # noqa: SLF001 — 接続 seam の意図的共有
            rows = conn.execute(
                f"SELECT * FROM situations WHERE status IN ({ph})"  # noqa: S608 — ph は ? 固定
                " ORDER BY situation_id",
                list(statuses),
            ).fetchall()
        return [_row_to_situation(r) for r in rows]

    def get_situation(self, situation_id: str) -> SituationRow | None:
        with self._repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT * FROM situations WHERE situation_id=?", (situation_id,)
            ).fetchone()
        return _row_to_situation(row) if row else None

    def open_situation(
        self,
        *,
        title: str,
        domain: str,
        anchors: frozenset[str],
        pir_ids: tuple[str, ...],
        now_iso: str,
        kind: str = "event",
        situation_id: str | None = None,
    ) -> SituationRow:
        """Situation を開設する (同一正規化 title は同一 id = 冪等)。既存なら既存行を返す。

        ``situation_id`` を渡すと title ハッシュでなく明示 id で開設する (常設 standing は
        title が claim 追従で動きうるため、安定 id を code 側が指定する — 設計 doc §2.1)。
        """
        sid = situation_id or situation_id_for(title)
        existing = self.get_situation(sid)
        if existing is not None:
            return existing
        row = SituationRow(
            situation_id=sid,
            title=title,
            domain=domain or "unclassified",
            status="active",
            anchors=frozenset(sorted(anchors)[:_MAX_ANCHORS]),
            pir_ids=tuple(pir_ids[:_MAX_PIR_IDS]),
            opened_at=now_iso,
            last_evidence_at=now_iso,
            kind=kind,
        )
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT OR IGNORE INTO situations "
                "(situation_id, title, domain, status, anchors, pir_ids,"
                " opened_at, last_evidence_at, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row.situation_id,
                    row.title,
                    row.domain,
                    row.status,
                    json.dumps(sorted(row.anchors), ensure_ascii=False),
                    json.dumps(list(row.pir_ids), ensure_ascii=False),
                    row.opened_at,
                    row.last_evidence_at,
                    row.kind,
                ),
            )
        return row

    def touch_situation(
        self,
        situation_id: str,
        *,
        last_evidence_at: str,
        status: SituationStatus | None = None,
    ) -> None:
        """新着証拠で last_evidence_at を進める (dormant 復帰は status='active' を渡す)。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            if status is None:
                conn.execute(
                    "UPDATE situations SET last_evidence_at=? WHERE situation_id=?",
                    (last_evidence_at, situation_id),
                )
            else:
                conn.execute(
                    "UPDATE situations SET last_evidence_at=?, status=? WHERE situation_id=?",
                    (last_evidence_at, status, situation_id),
                )

    def update_pir_ids(self, situation_id: str, pir_ids: tuple[str, ...]) -> None:
        """situation の pir_ids を更新する (L1b: 取込 inline タグ遅延で空だった分の自己修復)。

        pir_ids は従来 open_situation で一度だけ導出され再導出されなかった。取込時 PIR
        タグが間に合わず空で開いた situation を、後続 run で現 entity から再導出して埋める。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situations SET pir_ids=? WHERE situation_id=?",
                (json.dumps(list(pir_ids[:_MAX_PIR_IDS]), ensure_ascii=False), situation_id),
            )

    def update_title(self, situation_id: str, title: str) -> None:
        """title を評価済み claim に追従させる (situation の同一性=現在の判定文)。

        単発事象として開いた situation が続報で scope 拡大しても title が「〜が実施された」の
        まま固定される乖離 (2026-07-11 実測: キーウ攻撃 105 証拠が初日 title のまま) を防ぐ。
        situation_id は変えない (id は開設時 title のハッシュで安定、照合 token は
        build_situation_keys が現 title から導出するため自然に追従する)。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situations SET title=? WHERE situation_id=?",
                (title, situation_id),
            )

    def evidence_article_ids(self, situation_id: str, *, limit: int = 8) -> list[str]:
        """situation の証拠 article_id を新しい順に返す (standing の staleness 再評価用)。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT article_id FROM situation_evidence WHERE situation_id=?"
                " ORDER BY added_at DESC LIMIT ?",
                (situation_id, int(limit)),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def unread_evidence(
        self,
        statuses: tuple[SituationStatus, ...] = ("active", "dormant"),
        *,
        per_situation_limit: int = 20,
    ) -> dict[str, list[str]]:
        """接地 prompt に一度も供給されていない割当記事 ({situation_id: [article_id 新しい順]})。

        毎時 refresh (割当のみ・LLM ゼロ) が台帳に載せた記事を、定時 run が増分 ACH で
        読むための受け渡しキュー。旧実装 (unassessed_evidence) は「最終 revision より後に
        割当」をキューと見なしたため、revision が立つたびに読まれなかった記事 (キュー 20 件中
        読むのは 8 件) が黙って脱落し永久未評価になっていた。read_at を状態として持つことで
        読むまでキューに残る (silent drop なし・新しい順なので新着が常に優先)。
        """
        ph = ",".join("?" for _ in statuses)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT e.situation_id, e.article_id FROM situation_evidence e "
                "JOIN situations s ON s.situation_id = e.situation_id "
                f"WHERE s.status IN ({ph}) AND e.read_at IS NULL "  # noqa: S608 — ph は ? 固定
                "ORDER BY e.situation_id, e.added_at DESC",
                list(statuses),
            ).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            sid = str(r["situation_id"])
            lst = out.setdefault(sid, [])
            if len(lst) < per_situation_limit:
                lst.append(str(r["article_id"]))
        return out

    def set_status(
        self, situation_id: str, status: SituationStatus, *, closed_at: str | None = None
    ) -> None:
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situations SET status=?, closed_at=? WHERE situation_id=?",
                (status, closed_at, situation_id),
            )

    def reopen_situation(self, situation_id: str, *, now_iso: str) -> None:
        """closed/dormant → active へ復帰する (closed_at はクリア)。

        監査 backlog 2026-07-05: 同一正規化 title の判定が再出現すると
        ``open_situation`` が closed 行をそのまま返し、以後の revision/証拠が
        「閉じたまま不可視の Situation」へ無音で吸い込まれていた (ゾンビ経路)。
        reopen は必ず status を戻して可視化する。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situations SET status='active', closed_at=NULL, last_evidence_at=?"
                " WHERE situation_id=?",
                (now_iso, situation_id),
            )

    # ---------- revisions ----------

    def revisions_since(
        self, since_iso: str, *, until_iso: str | None = None
    ) -> dict[str, list[RevisionRow]]:
        """created_at >= since (until 指定時は <= until も) の revision を返す。

        situation_id 別・rev 昇順。weekly/monthly の「期間内 revision 軌跡」render
        (段D) 用。ISO UTC 文字列比較。
        監査 backlog 2026-07-05: 上限なしだと過去期間の再生成 (backfill) で期間後の
        revision が混入する (射影非対称)。until は当 run の書込 (created_at == period_end)
        を落とさないよう inclusive。
        """
        sql = "SELECT * FROM situation_revisions WHERE created_at >= ?"
        params: list[str] = [since_iso]
        if until_iso is not None:
            sql += " AND created_at <= ?"
            params.append(until_iso)
        sql += " ORDER BY situation_id, rev"
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(sql, params).fetchall()
        out: dict[str, list[RevisionRow]] = {}
        for r in rows:
            out.setdefault(str(r["situation_id"]), []).append(self._row_to_revision(r))
        return out

    def latest_revision_before(self, situation_id: str, *, until_iso: str) -> RevisionRow | None:
        """created_at <= until の最新 revision (期間射影で未来の revision を混ぜない)。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            r = conn.execute(
                "SELECT * FROM situation_revisions WHERE situation_id=? AND created_at <= ?"
                " ORDER BY rev DESC LIMIT 1",
                (situation_id, until_iso),
            ).fetchone()
        if r is None:
            return None
        return self._row_to_revision(r)

    def revision_latest_created_at(self) -> dict[str, str]:
        """situation_id → 最新 revision の created_at (revision の無い sid は含まない)。

        forecast 採点で「予測を開いた後にその情勢が再評価されたか」を判定するのに使う
        (較正バグ修正 2026-08-14)。全 situation を 1 クエリで返す — 対象は数百件規模で、
        sid ごとの N+1 クエリを避ける。集約 + GROUP BY のみなので PG/SQLite 双方で可搬
        (bare column の GROUP BY は PG で落ちるため使わない)。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT situation_id, MAX(created_at) AS last_created_at"
                " FROM situation_revisions GROUP BY situation_id"
            ).fetchall()
        return {str(r["situation_id"]): str(r["last_created_at"] or "") for r in rows}

    def latest_revision(self, situation_id: str) -> RevisionRow | None:
        with self._repo._connect() as conn:  # noqa: SLF001
            r = conn.execute(
                "SELECT * FROM situation_revisions WHERE situation_id=? ORDER BY rev DESC LIMIT 1",
                (situation_id,),
            ).fetchone()
        if r is None:
            return None
        return self._row_to_revision(r)

    @staticmethod
    def _row_to_revision(r: Any) -> RevisionRow:
        return RevisionRow(
            situation_id=str(r["situation_id"]),
            rev=int(r["rev"]),
            claim=str(r["claim"]),
            claim_type=str(r["claim_type"]),
            leading_hypothesis=str(r["leading_hypothesis"]),
            confidence=str(r["confidence"]),
            confidence_basis=str(r["confidence_basis"]),
            hypotheses_json=str(r["hypotheses"]),
            assumptions_json=str(r["assumptions"]),
            missing_json=str(r["missing"]),
            indicators_json=str(r["indicators"]),
            implication=str(r["implication"]),
            delta_type=cast(DeltaType, str(r["delta_type"])),
            delta_note=str(r["delta_note"]),
            created_at=str(r["created_at"]),
            run_id=str(r["run_id"]),
        )

    def add_revision(self, row: RevisionRow) -> RevisionRow:
        """revision を追記する (rev は単文 INSERT...SELECT で採番、UNIQUE 衝突は retry)。

        監査 backlog 2026-07-05: autocommit=True では transaction() が見かけのみで、
        旧実装 (SELECT MAX+1 → INSERT の 2 文) は cron × auto-trigger の並行実行が
        同じ rev を採番し UNIQUE(situation_id, rev) 衝突で片方が落ちうる。採番と挿入を
        1 文に畳み、それでも残る微小窓 (PG の同時 MAX 評価) は retry で吸収する。
        """
        last_error: Exception | None = None
        for _attempt in range(_ADD_REVISION_MAX_ATTEMPTS):
            try:
                with self._repo._connect() as conn:  # noqa: SLF001
                    cur = conn.execute(
                        "INSERT INTO situation_revisions "
                        "(situation_id, rev, run_id, claim, claim_type, leading_hypothesis,"
                        " confidence, confidence_basis, hypotheses, assumptions, missing,"
                        " indicators, implication, delta_type, delta_note, created_at)"
                        " SELECT ?, COALESCE(MAX(rev), 0) + 1, ?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                        " FROM situation_revisions WHERE situation_id=?"
                        " RETURNING rev",
                        (
                            row.situation_id,
                            row.run_id,
                            row.claim,
                            row.claim_type,
                            row.leading_hypothesis,
                            row.confidence,
                            row.confidence_basis,
                            row.hypotheses_json,
                            row.assumptions_json,
                            row.missing_json,
                            row.indicators_json,
                            row.implication,
                            row.delta_type,
                            row.delta_note,
                            row.created_at,
                            row.situation_id,
                        ),
                    )
                    got = cur.fetchone()
                return replace(row, rev=int(got["rev"]))
            except Exception as e:  # noqa: BLE001 — unique 衝突のみ retry、他は即 raise
                if not _is_unique_violation(e):
                    raise
                last_error = e
        raise RuntimeError(
            f"add_revision: UNIQUE 衝突が {_ADD_REVISION_MAX_ATTEMPTS} 回連続 "
            f"(situation_id={row.situation_id})"
        ) from last_error

    # ---------- evidence (観測と判断は別状態: assignment → read → assessment) ----------

    def record_assignment(
        self,
        *,
        situation_id: str,
        article_id: str,
        added_at: str,
        assigned_by: AssignedBy,
    ) -> bool:
        """記事を Situation に割当てる (観測のみ・未読/未評価)。新規挿入なら True。

        評価フィールド (polarity/excerpt 等) は書かない — 割当は「観測」であり
        「判断」ではない。旧 add_evidence はこれを polarity='neutral' の既定値で
        書いたため、割当だけの行が UI で「中立の証拠 (内容なし)」に化けていた。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            before = conn.execute(
                "SELECT 1 FROM situation_evidence WHERE situation_id=? AND article_id=?",
                (situation_id, article_id),
            ).fetchone()
            if before is not None:
                return False
            conn.execute(
                "INSERT OR IGNORE INTO situation_evidence "
                "(situation_id, article_id, polarity, attribution_basis, excerpt,"
                " source_tier, added_at, assigned_by) VALUES (?,?,?,?,?,?,?,?)",
                (
                    situation_id,
                    article_id,
                    "neutral",
                    "unattributed",
                    "",
                    "unknown",
                    added_at,
                    assigned_by,
                ),
            )
        return True

    def _fetch_article_bodies(self, article_id: str) -> tuple[bool, list[str]]:
        """(記事が実在するか, 照合に使う本文群)。原文と日本語訳を並べて返す。

        引用検査は原文と訳文の**両方**を haystack にする — 英語記事の要点が日本語訳から
        引用される運用が実在するため。本文取得は既存の repo API に委ねる
        (表示行の重複解決 ``_DISPLAY_ROW_ORDER`` を再実装しない)。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            exists = conn.execute(
                "SELECT 1 FROM articles WHERE article_id=? LIMIT 1", (article_id,)
            ).fetchone()
        if exists is None:
            return False, []
        bodies = [
            b
            for b in (
                self._repo.get_article_body(article_id),
                self._repo.get_article_body_ja(article_id),
            )
            if b
        ]
        return True, bodies

    def record_assessment(
        self,
        *,
        situation_id: str,
        article_id: str,
        assessed_at: str,
        polarity: str,
        attribution_basis: str,
        excerpt: str,
        source_tier: str,
        assigned_by: AssignedBy = "seed",
    ) -> bool:
        """ACH が引用した証拠の評価を刻む (割当済み行は upgrade・無ければ挿入)。

        旧 add_evidence は既存ペアを冪等 skip したため、毎時割当が先行した記事の
        評価結果 (polarity/excerpt) が永久に落ちていた (2026-07-16 監査で実測:
        台帳 neutral 909 行中 878 行が excerpt 空)。評価は常に最新判定で上書きし、
        added_at / assigned_by (観測の来歴) は元の値を保持する。返り値=新規挿入なら True。

        **引用実在の関門 (2026-08-22)**: 本 seam は LLM 出力を台帳へ入れる唯一の口で、
        検査が無かった (実測: 記事参照の 3.14% が実在せず、引用の 59.9% が本文に不在)。
        ここで 2 段の決定論検査を掛ける — ①記事が実在しなければ**書かない** (False)
        ②引用が本文に逐語で無ければ **excerpt を空にして行は残す** (観測の事実は残し、
        たどれない引用だけ落とす)。本文が purge 済み等で照合不能なときは引用を保持する
        (検証不能と反証は別物 — 実測で照合可能率 92.5%、不能は 1.7%)。
        """
        exists, bodies = self._fetch_article_bodies(article_id)
        if not exists:
            _log.warning(
                "evidence_article_not_found", situation=situation_id, article=article_id[:80]
            )
            return False
        verified_excerpt = excerpt
        if excerpt and bodies and not excerpt_is_supported(excerpt, *bodies):
            _log.info(
                "evidence_excerpt_unsupported", situation=situation_id, article=article_id[:80]
            )
            verified_excerpt = ""
        with self._repo._connect() as conn:  # noqa: SLF001
            before = conn.execute(
                "SELECT 1 FROM situation_evidence WHERE situation_id=? AND article_id=?",
                (situation_id, article_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO situation_evidence "
                "(situation_id, article_id, polarity, attribution_basis, excerpt,"
                " source_tier, added_at, assigned_by, read_at, assessed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (situation_id, article_id) DO UPDATE SET"
                " polarity=excluded.polarity,"
                " attribution_basis=excluded.attribution_basis,"
                " excerpt=excluded.excerpt,"
                " source_tier=excluded.source_tier,"
                " read_at=excluded.read_at,"
                " assessed_at=excluded.assessed_at",
                (
                    situation_id,
                    article_id,
                    polarity,
                    attribution_basis,
                    verified_excerpt[:500],
                    source_tier,
                    assessed_at,
                    assigned_by,
                    assessed_at,
                    assessed_at,
                ),
            )
        return before is None

    def mark_read(self, *, situation_id: str, article_ids: list[str], read_at: str) -> None:
        """接地 prompt に本文を供給した記事の read_at を刻む (割当済み行のみ・冪等)。

        読了は「未読キュー (unread_evidence) からの離脱」を意味する。引用に至らなかった
        記事も読了は事実として残す (読んだが証拠採用しなかった、を未読と区別する)。
        """
        if not article_ids:
            return
        ph = ",".join("?" for _ in article_ids)
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                f"UPDATE situation_evidence SET read_at=?"  # noqa: S608 — ph は ? 固定
                f" WHERE situation_id=? AND article_id IN ({ph})",
                [read_at, situation_id, *article_ids],
            )

    def evidence_ids_added_since(self, situation_id: str, *, since_iso: str) -> list[str]:
        """added_at >= since の証拠 article_id (新しい順)。夜間 deep-review の当日窓抽出用。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT article_id FROM situation_evidence"
                " WHERE situation_id=? AND added_at >= ? ORDER BY added_at DESC",
                (situation_id, since_iso),
            ).fetchall()
        return [str(r["article_id"]) for r in rows]

    def evidence_ids_by_situation(self, situation_ids: list[str]) -> dict[str, set[str]]:
        """複数 Situation の証拠 article_id を一括取得 ({situation_id: {article_id}})。"""
        if not situation_ids:
            return {}
        ph = ",".join("?" for _ in situation_ids)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT situation_id, article_id FROM situation_evidence"
                f" WHERE situation_id IN ({ph})",  # noqa: S608 — ph は ? 固定
                list(situation_ids),
            ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(str(r["situation_id"]), set()).add(str(r["article_id"]))
        return out

    def evidence_items(self, situation_id: str, *, limit: int = 5) -> list[dict[str, str]]:
        """射影/週次 sweep 用: **評価済み** 証拠行を EvidenceItem 互換 dict で返す (新しい順)。

        未評価の割当行 (assessed_at IS NULL) は返さない — polarity/excerpt が無意味な行を
        EvidenceItem に化けさせない (観測と判断は別状態)。割当件数は evidence_state_counts。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT article_id, polarity, attribution_basis, excerpt, source_tier"
                " FROM situation_evidence WHERE situation_id=? AND assessed_at IS NOT NULL"
                " ORDER BY assessed_at DESC LIMIT ?",
                (situation_id, limit),
            ).fetchall()
        return [
            {
                "article_id": str(r["article_id"]),
                "polarity": str(r["polarity"]),
                "attribution_basis": str(r["attribution_basis"]),
                "excerpt": str(r["excerpt"]),
                "source_tier": str(r["source_tier"]),
            }
            for r in rows
        ]

    def evidence_state_counts(self, situation_ids: list[str]) -> dict[str, dict[str, int]]:
        """複数 Situation の証拠状態別件数 ({sid: {total, assessed, unread}})。

        UI / 射影の正直な件数表示用 (「接地証拠 N / 未評価の割当 M」)。
        """
        if not situation_ids:
            return {}
        ph = ",".join("?" for _ in situation_ids)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT situation_id,"
                " COUNT(*) AS total,"
                " SUM(CASE WHEN assessed_at IS NOT NULL THEN 1 ELSE 0 END) AS assessed,"
                " SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread"
                f" FROM situation_evidence WHERE situation_id IN ({ph})"  # noqa: S608
                " GROUP BY situation_id",
                list(situation_ids),
            ).fetchall()
        return {
            str(r["situation_id"]): {
                "total": int(r["total"] or 0),
                "assessed": int(r["assessed"] or 0),
                "unread": int(r["unread"] or 0),
            }
            for r in rows
        }

    def add_relation(
        self, *, a_id: str, b_id: str, rel_type: str, basis: str, now_iso: str
    ) -> None:
        """関係エッジを追記 (a<b 正規順・冪等)。"""
        lo, hi = sorted((a_id, b_id))
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT OR IGNORE INTO situation_relations"
                " (a_id, b_id, rel_type, basis, created_at) VALUES (?,?,?,?,?)",
                (lo, hi, rel_type, basis[:200], now_iso),
            )

    def relations_for(self, situation_ids: list[str]) -> list[dict[str, str]]:
        """指定 Situation 間の関係エッジ ({a_id,b_id,rel_type,basis})。"""
        if not situation_ids:
            return []
        ph = ",".join("?" for _ in situation_ids)
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                f"SELECT a_id, b_id, rel_type, basis FROM situation_relations"  # noqa: S608
                f" WHERE a_id IN ({ph}) AND b_id IN ({ph})",
                [*situation_ids, *situation_ids],
            ).fetchall()
        return [
            {
                "a_id": str(r["a_id"]),
                "b_id": str(r["b_id"]),
                "rel_type": str(r["rel_type"]),
                "basis": str(r["basis"]),
            }
            for r in rows
        ]

    def evidence_excerpts(self, situation_id: str, *, limit: int = 5) -> list[dict[str, str]]:
        """増分 ACH prompt 用の主要証拠抜粋 ({polarity, excerpt}) — 評価済み行のみ。

        supports/contradicts を優先し neutral は後回し (prompt 有界化のため上位のみ)。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT polarity, excerpt FROM situation_evidence"
                " WHERE situation_id=? AND assessed_at IS NOT NULL AND excerpt <> ''"
                " ORDER BY CASE polarity WHEN 'supports' THEN 0 WHEN 'contradicts' THEN 0"
                " ELSE 1 END, assessed_at DESC LIMIT ?",
                (situation_id, limit),
            ).fetchall()
        return [{"polarity": str(r["polarity"]), "excerpt": str(r["excerpt"])} for r in rows]

    def assigned_article_ids(self) -> set[str]:
        """いずれかの Situation に証拠として割当済みの article_id 全集合。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute("SELECT DISTINCT article_id FROM situation_evidence").fetchall()
        return {str(r["article_id"]) for r in rows}

    # ---------- detection log (選定の監査台帳) ----------

    def log_detection(
        self,
        *,
        run_at: str,
        article_id: str,
        decision: Literal["assigned", "opened", "reopened", "rejected", "unassigned"],
        reason: str = "",
        situation_id: str | None = None,
    ) -> None:
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO situation_detection_log "
                "(run_at, article_id, decision, reason, situation_id) VALUES (?,?,?,?,?)",
                (run_at, article_id, decision, reason[:300], situation_id),
            )

    def purge_detection_log(self, *, before_iso: str) -> int:
        """選定台帳の retention (既定 90d は呼出側)。削除行数を返す。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            cur = conn.execute(
                "DELETE FROM situation_detection_log WHERE run_at < ?", (before_iso,)
            )
            return int(cur.rowcount if cur.rowcount is not None else 0)

    # ---------- forecasts (監査 2026-07-05 P4: indicators の open→scored lifecycle) ----------

    def open_forecast_rows(self) -> list[dict[str, str | int]]:
        """status='open' の forecast 全件 (採点と重複防止の照合用)。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT id, situation_id, indicator, opened_at, horizon_days, presented_count "
                "FROM situation_forecasts WHERE status = 'open'"
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "situation_id": str(r["situation_id"]),
                "indicator": str(r["indicator"]),
                "opened_at": str(r["opened_at"]),
                "horizon_days": int(r["horizon_days"]),
                "presented_count": int(r["presented_count"] or 0),
            }
            for r in rows
        ]

    def open_indicators_for(self, situation_id: str, *, limit: int) -> tuple[str, ...]:
        """まだ発火/期限切れしていない監視指標を新しい順に返す (増分 ACH の持ち越し用)。

        直前 revision の indicators (最大 3 件) だけを LLM に再提示していた旧挙動では、
        指標は 3 件上限に押し出されて 1 回照会されたきり 30 日の horizon を待つだけになり、
        「30 日の予測を 1 日しか見ない」不整合が生じていた。open な予測は horizon の間
        持ち越して毎回照会する。``limit`` は prompt 有界化のための上限。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT indicator FROM situation_forecasts"
                " WHERE situation_id = ? AND status = 'open'"
                " ORDER BY opened_at DESC, id DESC LIMIT ?",
                (situation_id, limit),
            ).fetchall()
        return tuple(str(r["indicator"]) for r in rows)

    def mark_forecasts_presented(self, situation_id: str, indicators: Sequence[str]) -> int:
        """指定の指標を「LLM に提示した」と記録する (presented_count += 1)。返り値=更新行数。

        採点規則がこのカウンタを見るため、**実際に prompt に載せた指標だけ**を渡すこと
        (cap で載せ切れなかった分に印を付けると、照会していない予測を外れと採点してしまう)。
        """
        names = [i.strip() for i in indicators if i.strip()]
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        with self._repo._connect() as conn:  # noqa: SLF001
            cur = conn.execute(
                "UPDATE situation_forecasts SET presented_count = presented_count + 1"
                f" WHERE situation_id = ? AND status = 'open' AND indicator IN ({placeholders})",
                (situation_id, *names),
            )
            return int(cur.rowcount if cur.rowcount is not None else 0)

    def open_forecast(
        self,
        *,
        situation_id: str,
        indicator: str,
        opened_at: str,
        horizon_days: int,
    ) -> None:
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO situation_forecasts "
                "(situation_id, indicator, opened_at, horizon_days) VALUES (?,?,?,?)",
                (situation_id, indicator[:500], opened_at, horizon_days),
            )

    def score_forecast(self, forecast_id: int, *, status: str, note: str, scored_at: str) -> None:
        """open forecast を hit/expired/unevaluated に採点する。

        unevaluated = 一度も再照会されないまま終了 (外れではない)。
        判定規則は forecast.py の ``_terminal_status`` が SSoT。
        """
        with self._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situation_forecasts SET status = ?, note = ?, scored_at = ? "
                "WHERE id = ? AND status = 'open'",
                (status, note[:300], scored_at, forecast_id),
            )

    def forecasts_scored_between(self, start_iso: str, end_iso: str) -> list[dict[str, str]]:
        """期間内に採点された forecast (report の scorecard 射影用)。"""
        with self._repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT situation_id, indicator, status, note, scored_at "
                "FROM situation_forecasts "
                "WHERE status IN ('hit', 'expired', 'unevaluated') "
                "AND scored_at >= ? AND scored_at < ? "
                "ORDER BY scored_at DESC",
                (start_iso, end_iso),
            ).fetchall()
        return [
            {
                "situation_id": str(r["situation_id"]),
                "indicator": str(r["indicator"]),
                "status": str(r["status"]),
                "note": str(r["note"] or ""),
                "scored_at": str(r["scored_at"] or ""),
            }
            for r in rows
        ]
