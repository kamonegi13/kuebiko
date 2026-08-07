"""発生日時の前後関係を synthesis に供給する時系列ヘルパ (因果推論はしない)。

**事実だけを渡す**: 報道日・事象日 (event_date) と、事象が報道より十分古ければ「再浮上」フラグ。
ACH の ``reporting_artifact`` (旧事案の再報道) の正しい発火と、応答系仮説 (reciprocal_response 等)
の前後判定に使う。窓は報道時刻基準のまま。**単なる時系列から因果を断定しない** (原則: [[cyber_
geopolitical_correlation]] 「報告時刻≠実発生 / 因果推論禁止」)。設計: docs/actor... n/a。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# 事象日が報道日よりこの日数以上前なら「再浮上 (旧事案の再報道)」とみなす。
_RESURFACE_DAYS = 14


def _parse_date(raw: str | None) -> date | None:
    """ "YYYY-MM-DD" / "YYYY-MM" / "YYYY" を date に (不正・空は None)。"""
    if not raw:
        return None
    s = str(raw).strip()[:10]
    for candidate in (s, f"{s[:7]}-01", f"{s[:4]}-01-01"):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _report_date(report: str | datetime | None) -> date | None:
    if isinstance(report, datetime):
        return report.date()
    return _parse_date(report if isinstance(report, str) else None)


@dataclass(frozen=True)
class Chronology:
    """1 記事の時系列事実 (プロンプト供給用の compact 表現)。"""

    report_short: str  # 報道日 "MM-DD"
    event_short: str  # 事象日 "YYYY-MM-DD" (無ければ "")
    event_basis: str  # disclosed / occurred / compromise 等
    resurfaced: bool  # 事象が報道より十分古い (旧事案の再報道)
    label: str  # プロンプト 1 行用 (例: "報道 07-01 / 事象 2024-03-15(occurred・再浮上)")


def article_chronology(
    *,
    report: str | datetime | None,
    event_date: str | None = None,
    event_date_basis: str | None = None,
    resurface_days: int = _RESURFACE_DAYS,
) -> Chronology:
    """記事の (報道日, 事象日, 再浮上フラグ) を組み立てる。因果は判定しない。"""
    rep = _report_date(report)
    ev = _parse_date(event_date)
    basis = (event_date_basis or "").strip()
    report_short = rep.strftime("%m-%d") if rep else "??"
    resurfaced = False
    if rep and ev and (rep - ev).days > resurface_days:
        resurfaced = True
    if ev:
        ev_short = ev.isoformat()
        tag = "・".join(x for x in (basis, "再浮上" if resurfaced else "") if x)
        event_seg = f" / 事象 {ev_short}" + (f"({tag})" if tag else "")
    else:
        event_seg = " / 事象日 不明"
    return Chronology(
        report_short=report_short,
        event_short=ev.isoformat() if ev else "",
        event_basis=basis,
        resurfaced=resurfaced,
        label=f"報道 {report_short}{event_seg}",
    )
