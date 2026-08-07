"""routing rule 発火の週次監査 (2026-07-18)。

routing rules は UI から編集できる (config-driven ladder、DB SSoT)。編集がルールを
沈黙させても配信自体は続く (下位ルール/fallback が吸収) ため、壊れたことに気づけない
— R0 病理 (一度も発火しないルールが数週間放置され、手動監査で初めて発覚) の再発防止。

fill-rate 監査と同じ push 型・決定論の週次 WARN で 2 つを検知する:

1. **発火 share の急落**: 前週の share (= 発火数 / posted 全数) が過去 4 週中央値の
   半分未満。count でなく share で判定 (収集量の変動に頑健 — KPI 再設計の原則)。
   中央値 < floor の rare rule は監視対象外 (週次統計で判定不能な希少ルールを騒がせない)。
2. **enabled ルールの発火実績ゼロ** (直近 lookback 全週): UI で追加/編集した
   ルールが一度も発火していない = 条件が現実と噛み合っていない疑い。

判定は ops への週次投稿 (fill-rate 監査と同じ 1 通) に 1 セクションとして載る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

# share がこの中央値未満の rare rule は急落監視の対象外 (現行 13 ルール中、
# 30d share 1% 以上は 8 ルール。R8 等の希少ルールは never_fired 検知のみが効く)
_RULE_BASELINE_MIN_SHARE = 1.0  # %
# fill-rate 監査と同じ半減閾値
_RULE_COLLAPSE_RATIO = 0.5
# 週の posted 母数がこれ未満なら判定しない (fill-rate と同じノイズ床)
_RULE_MIN_WEEK_POSTED = 30
# 比較する過去週数 / 収集 lookback
_RULE_BASELINE_WEEKS = 4
RULE_LOOKBACK_WEEKS = _RULE_BASELINE_WEEKS + 2


@dataclass(frozen=True)
class RuleWeekCell:
    """(rule, 週) の発火セル。fired=0 の週もゼロ充填で存在する (死亡検知の要)。"""

    rule_id: str
    week_start: date
    fired: int
    posted: int

    @property
    def share(self) -> float:
        return 100.0 * self.fired / self.posted if self.posted else 0.0


@dataclass(frozen=True)
class RuleWarn:
    """ルール発火の警告 1 件。"""

    rule_id: str
    kind: str  # "collapse" | "never_fired"
    baseline_share: float = 0.0
    current_share: float = 0.0
    current_fired: int = 0


def fetch_rule_daily_rows(con: Any, since_iso: str) -> list[tuple[str, str, int]]:
    """(day, rule_id, fired) 日次行 (posted のみ、rule_id NULL は除く)。"""
    rows = con.execute(
        "SELECT substr(a.created_at, 1, 10) AS day, a.routing_rule_id, COUNT(*)"
        " FROM articles a"
        " WHERE a.status = 'posted' AND a.created_at >= ?"
        "   AND a.routing_rule_id IS NOT NULL AND a.routing_rule_id <> ''"
        " GROUP BY substr(a.created_at, 1, 10), a.routing_rule_id",
        [since_iso],
    ).fetchall()
    return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]


def fetch_posted_daily_totals(con: Any, since_iso: str) -> list[tuple[str, int]]:
    """(day, posted_n) 日次母数。"""
    rows = con.execute(
        "SELECT substr(a.created_at, 1, 10) AS day, COUNT(*)"
        " FROM articles a"
        " WHERE a.status = 'posted' AND a.created_at >= ?"
        " GROUP BY substr(a.created_at, 1, 10)",
        [since_iso],
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def bucket_rule_weeks(
    rule_daily: list[tuple[str, str, int]],
    posted_daily: list[tuple[str, int]],
) -> dict[str, list[RuleWeekCell]]:
    """日次行を ISO 週に束ね、**発火 0 の週もゼロ充填**した週セルを rule 別に返す。

    ゼロ充填が無いと「先週から発火が消えた」ルールに当該週のセルが存在せず
    死亡を検知できない (group-by の構造的盲点)。
    """
    posted_by_week: dict[date, int] = {}
    for day_str, n in posted_daily:
        try:
            wk = _week_start(date.fromisoformat(day_str[:10]))
        except ValueError:
            continue
        posted_by_week[wk] = posted_by_week.get(wk, 0) + n

    fired: dict[str, dict[date, int]] = {}
    for day_str, rule_id, n in rule_daily:
        try:
            wk = _week_start(date.fromisoformat(day_str[:10]))
        except ValueError:
            continue
        per = fired.setdefault(rule_id, {})
        per[wk] = per.get(wk, 0) + n

    weeks = sorted(posted_by_week)
    return {
        rule_id: [
            RuleWeekCell(
                rule_id=rule_id,
                week_start=wk,
                fired=per.get(wk, 0),
                posted=posted_by_week[wk],
            )
            for wk in weeks
        ]
        for rule_id, per in fired.items()
    }


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def detect_rule_warns(
    cells_by_rule: dict[str, list[RuleWeekCell]],
    enabled_rule_ids: list[str],
    *,
    eval_week: date,
) -> list[RuleWarn]:
    """急落 (share 半減、死亡=0 を含む) + 発火実績ゼロの enabled ルールを検知する。

    急落検知は **enabled ルールに限る** (2026-07-18): 意図的に無効化/削除された
    ルールの発火消滅は期待どおりで警告しない — 検知対象は「意図しない破壊」のみ。
    config ロード失敗時 (enabled_rule_ids 空) は fail-open で全ルールを急落監視する
    (config 障害を理由に監視を黙らせない)。
    """
    warns: list[RuleWarn] = []
    enabled = set(enabled_rule_ids)

    for rule_id in sorted(cells_by_rule):
        if enabled and rule_id not in enabled:
            continue  # 意図的な無効化 (config に無い/enabled=false) は急落警告しない
        cells = cells_by_rule[rule_id]
        current = next((c for c in cells if c.week_start == eval_week), None)
        if current is None or current.posted < _RULE_MIN_WEEK_POSTED:
            continue
        baseline_cells = [
            c for c in cells if c.week_start < eval_week and c.posted >= _RULE_MIN_WEEK_POSTED
        ][-_RULE_BASELINE_WEEKS:]
        if not baseline_cells:
            continue
        baseline = _median([c.share for c in baseline_cells])
        if baseline < _RULE_BASELINE_MIN_SHARE:
            continue  # rare rule は週次統計で監視不能 (never_fired 検知のみ)
        if current.share < baseline * _RULE_COLLAPSE_RATIO:
            warns.append(
                RuleWarn(
                    rule_id=rule_id,
                    kind="collapse",
                    baseline_share=baseline,
                    current_share=current.share,
                    current_fired=current.fired,
                )
            )

    # 発火実績ゼロの enabled ルール (R0 病理 / UI 新規追加ルールの空振り)
    for rule_id in enabled_rule_ids:
        rule_cells = cells_by_rule.get(rule_id)
        if rule_cells is None or all(c.fired == 0 for c in rule_cells):
            warns.append(RuleWarn(rule_id=rule_id, kind="never_fired"))
    return warns


def build_rule_section(warns: list[RuleWarn], *, rules_checked: int) -> list[str]:
    """週次監査投稿に載せるルール発火セクションの行を組み立てる (純粋関数)。"""
    if not warns:
        return [f"ルール発火: 全 {rules_checked} ルール OK"]
    lines = [f"ルール発火: ⚠️ {len(warns)} 件"]
    for w in warns:
        if w.kind == "never_fired":
            lines.append(
                f"- {w.rule_id}: 発火実績なし (直近{RULE_LOOKBACK_WEEKS}週) — "
                "条件が現実と噛み合っていない疑い (UI 編集直後なら様子見可)"
            )
        else:
            lines.append(
                f"- {w.rule_id}: share {w.baseline_share:.1f}% → {w.current_share:.1f}%"
                f" (発火 {w.current_fired} 件) — ladder 上位の変更/条件破壊の疑い"
            )
    return lines


def collect_rule_warns(con: Any, *, now_date: date, eval_week: date) -> tuple[list[RuleWarn], int]:
    """DB からルール発火監査を実行する。返り値 = (warns, 監視ルール数)。

    ルール config のロード失敗は空リスト扱い (never_fired 検知だけ縮退、他は生きる)。
    """
    since = (_week_start(now_date) - timedelta(weeks=RULE_LOOKBACK_WEEKS)).isoformat()
    cells_by_rule = bucket_rule_weeks(
        fetch_rule_daily_rows(con, since), fetch_posted_daily_totals(con, since)
    )
    enabled_ids: list[str] = []
    try:
        from src.cti.routing_rules import load_routing_rules

        enabled_ids = [
            str(r.get("id")) for r in load_routing_rules() if r.get("id") and r.get("enabled", True)
        ]
    except Exception as e:  # noqa: BLE001 — config 障害で監査全体を殺さない
        _log.warning("routing_rule_audit_config_load_failed", error=str(e))
    warns = detect_rule_warns(cells_by_rule, enabled_ids, eval_week=eval_week)
    return warns, max(len(enabled_ids), len(cells_by_rule))
