"""BRIEF_SYNTHESIS_LINE: Discord ブリーフの「📊 状況総括」1 行の表示切替 (2026-11-17 暫定)。

状況総括の見出しは台帳の事案 1 件になりやすく「状況総括: <単独の事案>」と誤読される。
横断のパターンへ作り直すまで、0 で Discord の 1 行を出さない (朝は PIR ブリーフが動いた問いを出す)。
"""

from __future__ import annotations

import pytest

from src.digest import discord_brief

_HEADLINE = "架空銀行Aが従業員のメールアカウントへの不正アクセスを公表した。見立ては偶発。"


def _payload() -> dict[str, object]:
    return {
        "synthesis": {"headline": _HEADLINE, "sections": []},
        "high_threats": {
            "total": 1,
            "items": [{"title": "架空の脅威", "url": "https://kuebiko.example/a"}],
        },
        "pir_brief": None,
        "pir": [],
    }


def test_default_keeps_synthesis_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIEF_SYNTHESIS_LINE", raising=False)
    text = discord_brief.compose_morning(_payload(), base_url=None).text
    assert "状況総括" in text


@pytest.mark.parametrize("slot", ["morning", "evening"])
def test_flag_off_drops_synthesis_line(monkeypatch: pytest.MonkeyPatch, slot: str) -> None:
    monkeypatch.setenv("BRIEF_SYNTHESIS_LINE", "0")
    if slot == "morning":
        text = discord_brief.compose_morning(_payload(), base_url=None).text
    else:
        text = discord_brief.compose_evening(_payload(), base_url=None).text
    assert "状況総括" not in text
    assert "架空銀行A" not in text


def test_flag_off_evening_quiet_line_does_not_mention_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIEF_SYNTHESIS_LINE", "0")
    payload = {**_payload(), "high_threats": {"total": 0, "items": []}}
    text = discord_brief.compose_evening(payload, prior_headline=_HEADLINE, base_url=None).text
    assert "状況総括" not in text
    assert "高脅威 0 件" in text
