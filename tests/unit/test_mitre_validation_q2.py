"""Phase 1 Q2: MITRE technique ID の妥当性検証 (egregious hallucination の drop)。"""

from __future__ import annotations

import pytest

from src.cti.ioc_extractor import (
    ExtractedIocs,
    is_plausible_technique_id,
    merge_techniques,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tid", "ok"),
    [
        ("T1566", True),
        ("T1566.001", True),
        ("T1059.003", True),
        ("T0883", True),  # ICS レンジ
        ("t1566", True),  # case-insensitive
        ("T9999", False),  # base 過大
        ("T0001", False),  # base 過小
        ("T123", False),  # 桁数不足
        ("T12345", False),  # 桁数過多
        ("G0016", False),  # group は technique ではない
        ("Cobalt Strike", False),
        ("", False),
    ],
)
def test_is_plausible_technique_id(tid: str, ok: bool) -> None:
    assert is_plausible_technique_id(tid) is ok


@pytest.mark.unit
def test_merge_drops_invalid_keeps_valid_groups_and_others() -> None:
    extracted = ExtractedIocs(
        mitre_techniques=("T1566", "T9999"),
        mitre_groups=("G0016",),
    )
    out = merge_techniques(["T1059.003", "T0001", "Cobalt Strike"], extracted)
    # 妥当な technique は保持
    assert "T1566" in out
    assert "T1059.003" in out
    # ATT&CK レンジ外の hallucination は drop
    assert "T9999" not in out
    assert "T0001" not in out
    # group (G####) と非 technique 文字列はそのまま保持
    assert "G0016" in out
    assert "COBALT STRIKE" in out


@pytest.mark.unit
def test_merge_dedup_and_case() -> None:
    out = merge_techniques(["T1566", "t1566"], ExtractedIocs())
    assert out == ["T1566"]
