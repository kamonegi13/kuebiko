"""常設情報要求 posture カード (段C、2026-07-13)。

board の第 3 の独立レンズ: 「国家 N は日本の重要インフラへの事前配置を進めているか」の
現在推定 (リード仮説 + 較正確度) と確度の軌跡。**データ源は situations +
situation_revisions のみ** (board 集計と独立 — 「カードの確度・軌跡 = revisions と一致、
別集計を作らない」の受入基準をクエリ構造で保証)。ラダー/世界行動には折り込まない。
設計: docs/prepositioning_posture_ledger_design.md §5.1。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.assessment.situation_store import SituationStore
from src.assessment.standing import STANDING_KIND, STANDING_SEEDS
from src.storage.run_history import DEFAULT_DB_PATH
from src.synthesis.grounded.hypotheses import get_hypothesis

# nation → 表示ラベル。SSoT は overview._NATION_LABELS (有機的結合監査 M3: 複製を
# 増やさない — 新規コピーでなく既存定義を参照する)。
from src.ui.services.overview import _NATION_LABELS

#: 推移に載せる改訂の上限と期間。月次の状況総括を畳み、長期の軌跡はここで読む (段D、
#: 2026-09-29)。1 問あたりの改訂は実測で 1 日 1 回前後なので 30 日 ≒ 30-40 版。
_TRAJECTORY_LIMIT = 60
_TRAJECTORY_DAYS = 30
_EVIDENCE_WINDOW_DAYS = 30
#: 「何が見えれば答えが変わるか」に載せる指標の上限 (新しい順)。
_INDICATOR_LIMIT = 8


def _labelize(text: str) -> str:
    """仮説の内部 id を表示名へ (PIR ブリーフと同じ変換)。"""
    from src.digest.pir_brief import _labelize as labelize

    return labelize(text)


def _split_delta_note(raw: object) -> tuple[str, list[str]]:
    """delta_note を「なぜ動いたか」と「発火した指標」に分ける。

    書き込み側 (``stateful.FIRED_INDICATOR_MARKER``) が付けた固定接頭辞で切る —
    LLM の自由文でなく**自分が書いた形式**を読むので壊れにくい。混ざったままだと
    「答えは動いていないのに『指標発火: …』が変化理由の欄に出る」誤読になる。
    """
    from src.assessment.stateful import FIRED_INDICATOR_MARKER

    note = str(raw or "")
    if FIRED_INDICATOR_MARKER not in note:
        return note.strip(), []
    head, _, tail = note.partition(FIRED_INDICATOR_MARKER)
    fired = [x.strip() for x in tail.split(";") if x.strip()]
    return head.rstrip(" /").strip(), fired


def _within_trajectory_window(revs: list[Any], *, now: datetime | None) -> list[Any]:
    """古い順の改訂 → 直近 ``_TRAJECTORY_DAYS`` 日の分 (最新の 1 版は必ず残す)。"""
    if not revs:
        return revs
    since = (now or datetime.now(UTC)) - timedelta(days=_TRAJECTORY_DAYS)
    kept = [r for r in revs if _parse_created(r[5]) >= since]
    return kept or revs[-1:]


def _parse_created(raw: object) -> datetime:
    try:
        ts = datetime.fromisoformat(str(raw).replace(" ", "T"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _json_list(raw: object) -> list[str]:
    """revision の JSON 列 → 文字列 list (壊れていれば空 = 面を落とさない)。"""
    if not raw:
        return []
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return [str(x) for x in loaded if str(x).strip()] if isinstance(loaded, list) else []


def _standing_targets(store: SituationStore, *, seed_only: bool) -> list[tuple[str, str, str]]:
    """カードを作る対象 (situation_id, 問い文, 主体の国コード)。

    既定は台帳の ``kind='standing'`` **全件** — 昇格した問い (config_store 由来) を
    含める (§6e「問いはデータ」)。``seed_only`` は重要インフラ board 専用で、
    あの面は**事前配置 posture の国別 board** なので code 所有の 4 件だけを見る
    (趨勢・閾値の問いを混ぜると面の意味が壊れる)。
    """
    if seed_only:
        return [
            (s.situation_id, s.title, s.nation)
            for s in STANDING_SEEDS
            if store.get_situation(s.situation_id) is not None
        ]
    # seed を先頭に **seed 順** で置く — この並びは脅威の序列を表しており、
    # id 順にすると意味のない並びになる (board と共有する不変条件)。昇格分は後ろへ。
    seeds = [
        (s.situation_id, s.title, s.nation)
        for s in STANDING_SEEDS
        if store.get_situation(s.situation_id) is not None
    ]
    seed_ids = {sid for sid, _t, _n in seeds}
    promoted = [
        (r.situation_id, r.title, _subject_nation(r.situation_id))
        for r in store.load_situations()
        if r.kind == STANDING_KIND and r.situation_id not in seed_ids
    ]
    return seeds + promoted


def _subject_nation(situation_id: str) -> str:
    """昇格した問いの主体 (スロット ``subject``)。持たない型 (趨勢) は空。"""
    try:
        from src.assessment.question_store import list_questions

        for q in list_questions():
            if q.get("situation_id") == situation_id:
                return str((q.get("slots") or {}).get("subject") or "")
    except Exception:  # noqa: BLE001 — 保存層の障害で面を壊さない
        return ""
    return ""


def build_standing_posture(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
    seed_only: bool = False,
) -> list[dict[str, Any]]:
    """常設情報要求の posture カード payload (situation_id 順、未開設は除外)。

    ``now`` = 30 日証拠窓の基準時刻 (既定は実時刻)。テストは fixture の固定時刻を
    渡す — 実時刻直書きだと fixture 日付 + 30 日で失効する時限テストになる
    (2026-08-13 に実際に失効した)。

    ``seed_only`` = code 所有の 4 件だけ (重要インフラ board 用)。
    """
    store = SituationStore(db_path=db_path)
    since = ((now or datetime.now(UTC)) - timedelta(days=_EVIDENCE_WINDOW_DAYS)).isoformat()
    cards: list[dict[str, Any]] = []
    for sid, title, nation in _standing_targets(store, seed_only=seed_only):
        row = store.get_situation(sid)
        if row is None:
            continue
        with store._repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の意図的共有
            rev_rows = conn.execute(
                "SELECT rev, claim, leading_hypothesis, confidence, delta_type, created_at,"
                " delta_note, confidence_basis, missing"
                " FROM situation_revisions WHERE situation_id=?"
                " ORDER BY rev DESC LIMIT ?",
                (sid, _TRAJECTORY_LIMIT),
            ).fetchall()
            indicator_rows = conn.execute(
                "SELECT indicator, status, opened_at, horizon_days"
                " FROM situation_forecasts WHERE situation_id=?"
                " ORDER BY opened_at DESC LIMIT ?",
                (sid, _INDICATOR_LIMIT),
            ).fetchall()
            counts = conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN a.victim_country_iso = 'JP' THEN 1 ELSE 0 END)"
                " FROM situation_evidence e JOIN articles a ON a.article_id = e.article_id"
                " WHERE e.situation_id = ? AND datetime(a.created_at) >= datetime(?)",
                (sid, since),
            ).fetchone()
        revs = _within_trajectory_window(list(reversed(rev_rows)), now=now)
        latest = revs[-1] if revs else None
        leading = str(latest[2]) if latest else ""
        hyp = get_hypothesis(leading) if leading else None
        cards.append(
            {
                "situation_id": sid,
                "nation": nation,
                "nation_label": _NATION_LABELS.get(nation, nation),
                "question": title,
                "assessed": latest is not None,
                "claim": str(latest[1]) if latest else "",
                "leading_hypothesis": leading,
                "leading_label": hyp.label if hyp else leading,
                "confidence": str(latest[3]) if latest else "",
                # 確度の根拠 (ACH=… / source_basis=…)。台帳 (situation_revisions) に既に
                # 記録されているが未露出だった — honesty doctrine: 確度は接地を可視化する。
                "confidence_basis": str(latest[7]) if latest else "",
                "delta_type": str(latest[4]) if latest else "",
                # 前回の答えから**なぜ**動いたか。問いを主語に読むとき、確度の数字より
                # 「何が変わったのでこうなったか」が要る (docs/pir_brief_design.md §3)。
                "delta_note": _labelize(_split_delta_note(latest[6])[0]) if latest else "",
                # 発火した指標 = 観測された事実。変化の理由とは別の欄に置く。
                "fired_indicators": _split_delta_note(latest[6])[1] if latest else [],
                # 何が分かっていないか (答えの限界を答えと同じ面に置く = honesty doctrine)。
                "missing_evidence": _json_list(latest[8]) if latest else [],
                "assessed_at": str(latest[5]) if latest else "",
                # 鮮度: この答えがいつの証拠に基づくか。静穏な問いほど重要な欄
                # (「静か≠安全」— 古いことでなく、古いと分からないことが危険)。
                "last_evidence_at": row.last_evidence_at,
                # 何が見えれば答えが変わるか (I&W)。open=未発火 / hit=発火 / expired=期限切れ。
                "indicators": [
                    {
                        "indicator": str(i[0]),
                        "status": str(i[1]),
                        "opened_at": str(i[2]),
                        "horizon_days": int(i[3] or 0),
                    }
                    for i in indicator_rows
                ],
                "evidence_related_30d": int(counts[0] or 0),
                "evidence_direct_30d": int(counts[1] or 0),
                "trajectory": [
                    {
                        "rev": int(r[0]),
                        "at": str(r[5]),
                        "confidence": str(r[3]),
                        "delta_type": str(r[4]),
                        "note": str(r[6] or "")[:100],
                        # 変化の理由だけ (発火指標の接頭辞以降を除く)。PIR ブリーフが引用する
                        "reason": _labelize(_split_delta_note(r[6])[0])[:200],
                    }
                    for r in revs
                ],
            }
        )
    return cards
