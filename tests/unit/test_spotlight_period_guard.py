"""Spotlight の期間を画面側で固定しない (2026-09-16 の取り残しの再発防止)。

⚠ 実際に起きた: 2026-08-29 の日次化で生成は `rolling7` に移ったのに、SIR 詳細ページと
ダッシュボード widget が `"weekly"` を固定したまま取り残され、**18 日前の内容を出して
いた** (実測: weekly の最終生成 2026-08-29 / rolling7 は当日)。しかも画面は「古い」とは
言わない — 「同型経路の片方だけ直す」の再発で、静かに腐る形だった。

生成側の期間が変わったとき、呼出側が固定していると必ず腐る。**既定が決定を持つ**形に
統一し、画面側は上書きしない。
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path("frontend/src")
#: 期間の既定を宣言してよい唯一の場所。
_OWNER = Path("frontend/src/api/spotlight.ts")


def _spotlight_call_lines() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for f in (*_SRC.rglob("*.ts"), *_SRC.rglob("*.tsx")):
        if f == _OWNER or "__fixtures__" in str(f):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if "spotlightApi." in line or "SpotlightPeriod" in line:
                out.append((f, line.strip()))
    return out


def test_no_screen_hardcodes_a_spotlight_period() -> None:
    """画面側が期間の文字列リテラルを渡さないこと。"""
    offenders = [
        f"{f}: {line}"
        for f, line in _spotlight_call_lines()
        if re.search(r'"(daily|weekly|monthly|rolling7)"', line)
    ]

    assert offenders == [], "画面が spotlight の期間を固定している:\n" + "\n".join(offenders)


def test_default_period_is_declared_once() -> None:
    """既定は 1 箇所で宣言する (複製すると片方だけ腐る)。"""
    body = _OWNER.read_text(encoding="utf-8")

    assert body.count("SPOTLIGHT_DEFAULT_PERIOD: SpotlightPeriod") == 1


def test_default_period_matches_what_is_generated() -> None:
    """既定が**実際に生成されている**期間であること。

    生成側 (`SPOTLIGHT_ONLY_PERIODS`) を SSoT に照合する — 片方だけ変えられない。
    """
    from src.spotlight.models import SPOTLIGHT_ONLY_PERIODS

    body = _OWNER.read_text(encoding="utf-8")
    m = re.search(r'SPOTLIGHT_DEFAULT_PERIOD: SpotlightPeriod = "([a-z0-9]+)"', body)

    assert m is not None
    assert m.group(1) in SPOTLIGHT_ONLY_PERIODS
