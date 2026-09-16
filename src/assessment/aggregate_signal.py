"""集約シグナルを ACH へ供給する — 蓄積から答えを組み立てるための材料。

利用者指摘 (2026-09-16):「一記事で答えてもいい。ただしそれだけではダメ」。
PIR の答えは**蓄積から組み立てられ**、単一の記事はそれを更新する。既存の A 型 (事前配置)
はこれを 2 本腕の証拠規則で実現している — 直接 (R1/R3、1 本で決定的) と間接 (R2、蓄積して
初めて意味を持つ)。

**埋めた穴**: `measure_composition` (段B-2b) は構成比を算出できたが、ACH のプロンプトへ
渡る経路が無かった。結果として判定は「証拠記事を数本読んだ印象」で、蓄積から組み立てた
ことになっていなかった。本モジュールが決定論の事実ブロックを作り、増分 ACH へ渡す。

**LLM に数えさせない**。数値はここで算出し、プロンプトは「そのまま使え」と指示する。
数えさせると収集量が判定を駆動する (CLAUDE.md §7「収集量を重要性の代理にしない」)。

⚠ 本モジュールも収集網の観測を扱う。importance / routing / 記事選抜から参照してはならない
(関門は ``tests/unit/test_composition_boundary.py``)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.assessment.composition import (
    P1,
    P2,
    AxisRow,
    CompositionMeasurement,
    measure_composition,
)
from src.logging_config import get_logger
from src.synthesis.grounded.estimate import Confidence

_log = get_logger(__name__)

#: プロンプトに載せる構成比の行数上限 (読み手が最初に見るべき動きだけ)。
_MAX_SHIFT_ROWS = 6
#: この幅未満の移動は誤差として載せない (百分率ポイント)。
_MIN_SHIFT_POINTS = 1.0


def format_aggregate_signal(
    measurement: CompositionMeasurement,
    *,
    p1_label: str,
    p2_label: str,
) -> str:
    """構成比測定 → 増分 ACH に載せる決定論の事実ブロック。

    データが無ければ **空文字**を返す — 空の表を出すと「変化なし」と誤読されるため、
    載せないことと「変化なし」を区別する。
    """
    cohort = measurement.cohort
    if not measurement.shifts or cohort.feeds == 0:
        return ""

    lines = [
        "【集約シグナル (コードが決定論で算出した事実)】",
        "⚠ **あなたは数えない**。下の数値をそのまま使うこと。件数の多寡を重要性の代理に",
        "しないこと — これは収集網が何を拾ったかの観測であって、重要性の定義ではない。",
        "",
        f"- 比較した窓: {p1_label} → {p2_label}",
        f"- 同一媒体コホート: {cohort.feeds} 媒体 (この比較の母数)",
        f"- コホートが占める割合: {cohort.article_share * 100:.1f}% (低いほどこの比較は弱い)",
        f"- 収集網の規模: {cohort.p1_feeds_total} 媒体 → {cohort.p2_feeds_total} 媒体",
        f"- 判定に使った件数: {cohort.p1_articles} → {cohort.p2_articles} 件",
    ]

    if measurement.is_thin:
        lines += [
            "",
            f"⚠ **母集団が薄い** (閾値 {measurement.thin_threshold} 件未満)。",
            "この構成比の動きは誤差に埋もれている可能性が高く、**趨勢として断定しないこと**。",
        ]

    shown = [s for s in measurement.shifts if abs(s.shift_points) >= _MIN_SHIFT_POINTS]
    if shown:
        lines += ["", "- 構成比の移動 (百分率ポイント、大きい順):"]
        for s in shown[:_MAX_SHIFT_ROWS]:
            lines.append(
                f"    {s.value}: {s.p1_share * 100:.1f}% → {s.p2_share * 100:.1f}%"
                f" ({s.shift_points:+.1f}pt, {s.p1_count}→{s.p2_count} 件)"
            )
    else:
        lines += ["", f"- 構成比の移動: {_MIN_SHIFT_POINTS}pt 以上の動きは無い"]

    lines += [
        "",
        "⚠ 上の構成比は**両窓に存在する媒体だけ**で算出しており、収集網が増減した分は",
        "除去済み。ただし**世界の報道量の変動は除去できていない** (大型事案があれば全媒体が",
        "書く)。したがって「観測の変化」仮説はこの数値だけでは棄却できない。",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class Windows:
    """比較する 2 窓 (直前の同じ長さ vs 現在)。

    **窓は隣接・等長**にする — 長さが違うと構成比の分母が変わり、比較が成立しない。
    ラベルは実日付にする (「前期」では、後からどの範囲を見たか再現できない)。
    """

    p1_start: datetime
    p1_end: datetime
    p2_start: datetime
    p2_end: datetime

    @property
    def p1_label(self) -> str:
        return f"{self.p1_start:%Y-%m-%d}〜{self.p1_end:%Y-%m-%d}"

    @property
    def p2_label(self) -> str:
        return f"{self.p2_start:%Y-%m-%d}〜{self.p2_end:%Y-%m-%d}"


def split_windows(*, now: datetime, window_days: int) -> Windows:
    """現在窓と、その直前の同じ長さの窓に割る。"""
    p2_end = now
    p2_start = now - timedelta(days=window_days)
    return Windows(
        p1_start=p2_start - timedelta(days=window_days),
        p1_end=p2_start,
        p2_start=p2_start,
        p2_end=p2_end,
    )


#: 比較窓の長さ (日)。45 日 × 2 = 90 日で、実測の母集団 (帰属済み cn 264 / kp 234 /
#: ru 1,082 件) が薄さの閾値を越える水準。短くすると軸が薄くなり判定不能が増える。
DEFAULT_WINDOW_DAYS = 45
#: 1 窓あたりに読む行の上限 (安全弁)。超えたら log に残す — no-silent-caps。
#: 実測 2026-09-16: 45 日窓に posted (recap 除く) が 8,815 件。6,000 だと窓の古い側が
#: 落ちて実効 34 日になっていた。1 窓の取得は実測 78ms なので引き上げは安価。
_ROW_CAP = 12000


def load_axis_rows(
    *,
    condition: dict[str, Any],
    windows: Windows,
    db_path: Path,
    repo: Any,
    axis: str = "intent",
) -> list[AxisRow]:
    """証拠条件に一致する記事を 2 窓ぶん集め、構成比の入力 (出所, 窓, 軸値) にする。

    条件の評価は収穫と**同じ経路** (``signals_from_candidate`` + ``_eval_condition``) を
    使う — 判定が 2 箇所に分かれると必ずずれる (本プロジェクトで再発済み)。
    """
    from src.assessment.standing import _actor_lookup, signals_from_candidate
    from src.cti.router import get_source_quality
    from src.cti.routing_rules import _eval_condition

    nation_by_key = _actor_lookup()
    sq = get_source_quality()
    out: list[AxisRow] = []
    for window, (start, end) in (
        (P1, (windows.p1_start, windows.p1_end)),
        (P2, (windows.p2_start, windows.p2_end)),
    ):
        rows = _fetch_axis_candidates(db_path, start_iso=start.isoformat(), end_iso=end.isoformat())
        if len(rows) >= _ROW_CAP:
            _log.warning("aggregate_signal_row_cap", window=window, cap=_ROW_CAP)
        keys = repo.entity_keys_for_articles(
            [r["article_id"] for r in rows], types=("actor", "involved_country")
        )
        for r in rows:
            aid = str(r["article_id"])
            signals = signals_from_candidate(
                r, entity_keys=frozenset(keys.get(aid, set())), nation_by_key=nation_by_key
            )
            if not _eval_condition(condition, signals, sq):
                continue
            out.append(
                AxisRow(
                    feed_title=str(r.get("feed_title") or ""),
                    window=window,
                    value=str(r.get(axis) or ""),
                )
            )
    return out


def _fetch_axis_candidates(db_path: Path, *, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """窓内の posted 記事 (recap 除く) を、条件評価に要る列つきで返す。"""
    from src.storage.db_backend import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT article_id, feed_title, socio_political_intent, victim_country_iso,"
            " victim_sector_canonical, importance, category"
            " FROM articles"
            " WHERE status = 'posted' AND COALESCE(category, '') != 'recap'"
            " AND datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)"
            " ORDER BY datetime(created_at) DESC LIMIT ?",
            (start_iso, end_iso, _ROW_CAP),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "article_id": str(r[0]),
            "feed_title": r[1],
            "intent": r[2],
            "victim_country": r[3],
            "victim_sector": r[4],
            "importance": r[5],
            "category": r[6],
        }
        for r in rows
    ]


def build_measurement(
    *,
    condition: dict[str, Any],
    now: datetime,
    db_path: Path,
    repo: Any,
    axis: str = "intent",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[str, CompositionMeasurement | None]:
    """集約の (プロンプト用テキスト, 測定結果)。失敗しても判定を止めない。

    テキストは LLM への提示用、測定結果は**確度上限の算出用**。テキストだけ渡しても
    ACH の集計には効かない (2026-09-16 実測) ため、両方が要る。
    """
    if not condition:
        return "", None
    try:
        windows = split_windows(now=now, window_days=window_days)
        rows = load_axis_rows(
            condition=condition, windows=windows, db_path=db_path, repo=repo, axis=axis
        )
        measurement = measure_composition(rows)
        text = format_aggregate_signal(
            measurement, p1_label=windows.p1_label, p2_label=windows.p2_label
        )
        return text, measurement
    except Exception as e:  # noqa: BLE001 — 集約が出せなくても判定は続ける
        _log.warning("aggregate_signal_failed", error=str(e))
        return "", None


def build_aggregate_signal(
    *,
    condition: dict[str, Any],
    now: datetime,
    db_path: Path,
    repo: Any,
    axis: str = "intent",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> str:
    """証拠条件 → 増分 ACH に載せる集約シグナル。失敗しても評価を止めない (空を返す)。"""
    if not condition:
        return ""
    try:
        windows = split_windows(now=now, window_days=window_days)
        rows = load_axis_rows(
            condition=condition, windows=windows, db_path=db_path, repo=repo, axis=axis
        )
        measurement = measure_composition(rows)
        return format_aggregate_signal(
            measurement, p1_label=windows.p1_label, p2_label=windows.p2_label
        )
    except Exception as e:  # noqa: BLE001 — 集約が出せなくても判定は続ける
        _log.warning("aggregate_signal_failed", error=str(e))
        return ""


#: 変化を主張する leading。これらだけが集約の支持を要する
#: (「平時の変動内」「横ばい」は fail-closed の既定なので、支持が無くても抑えない)。
_CHANGE_ASSERTING = frozenset({"trend_worsening", "trend_improving", "threshold_crossed"})
#: 悪化方向を主張する leading (集約が下がっていたら抑える)。
_ASSERTS_UP = frozenset({"trend_worsening", "threshold_crossed"})
#: 閾値の問いが見る「質的に異なる行為」。脅威スロットを持たない型で使う。
_ESCALATORY_INTENTS = ("prepositioning", "disruption")
#: この幅未満の移動は「支持していない」と扱う (百分率ポイント)。
_SUPPORT_MIN_POINTS = 2.0


def aggregate_confidence_cap(
    measurement: CompositionMeasurement | None,
    *,
    leading: str,
    threat: str | None,
) -> tuple[Confidence, str] | None:
    """集約が支持しない「変化の主張」の確度に上限をかける。None = 上限なし。

    ⚠ **leading は変えない**。ACH の証拠駆動判定を尊重し、確度だけを抑える
    (既存 `final_confidence` と同じ方向中立の思想 —「証拠が弱ければ *どの結論でも* 抑える」)。

    なぜ上限なのか: 集約を散文でプロンプトに載せても ACH の集計に席が無く、観測系の仮説が
    0/0 (未評価) のまま強い確度が出た (2026-09-16 実測)。散文は効かず**構造だけが効く**。
    カウントの捏造 (`reconcile_hypotheses` が禁じている) を避けつつ判定に効かせる唯一の座。

    ⚠ 集約が**出せないこと**を根拠に抑えない — 無知は証拠ではない。
    """
    if leading not in _CHANGE_ASSERTING or measurement is None or not measurement.shifts:
        return None
    if measurement.is_thin:
        return (
            "low",
            f"集約の母集団が薄く ({measurement.cohort.p1_articles}→"
            f"{measurement.cohort.p2_articles} 件) 変化の主張を支持しない",
        )
    watched = (threat,) if threat else _ESCALATORY_INTENTS
    delta = sum(s.shift_points for s in measurement.shifts if s.value in watched)
    label = threat or "/".join(_ESCALATORY_INTENTS)
    asserts_up = leading in _ASSERTS_UP
    if abs(delta) < _SUPPORT_MIN_POINTS:
        return ("moderate", f"集約に有意な移動がない ({label} {delta:+.1f}pt)")
    if (asserts_up and delta < 0) or (not asserts_up and delta > 0):
        return ("low", f"集約は逆方向を示す ({label} {delta:+.1f}pt)")
    return None
