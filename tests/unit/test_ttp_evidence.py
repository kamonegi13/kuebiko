"""TTP の本文裏付け (2026-09-27) — 当て推量の技術を落とし、本文にある手口は残す。"""

from __future__ import annotations

import re

import pytest

from src.cti.ttp_evidence import (
    filter_llm_techniques,
    find_ttp_evidence,
    load_evidence_patterns,
)

_NAMES = {"T1053.005": "Scheduled Task", "T1190": "Exploit Public-Facing Application"}
_PATS = {
    "T1190": (re.compile(r"actively\s+exploit", re.IGNORECASE),),
    "T1566.001": (re.compile(r"添付ファイル"),),
}


def _find(tid: str, text: str) -> str | None:
    return find_ttp_evidence(tid, text, patterns=_PATS, names=_NAMES)


def test_explicit_technique_id_is_evidence() -> None:
    assert _find("T1589", "uses target knowledge from research (T1589)") == "id"


def test_parent_id_supports_sub_technique() -> None:
    assert _find("T1566.001", "initial access via T1566 lures") == "id"


def test_id_does_not_match_longer_number() -> None:
    assert _find("T1190", "ticket T11901 closed") is None


def test_technique_name_is_evidence() -> None:
    assert _find("T1053.005", "the loader creates a scheduled task for persistence") == "name"


def test_keyword_in_japanese_is_evidence() -> None:
    assert _find("T1566.001", "添付ファイルを開かせる手口") == "keyword"


def test_generic_advisory_is_not_evidence_for_exploitation() -> None:
    assert _find("T1190", "A vulnerability could allow remote attackers to execute code.") is None


def test_filter_drops_unsupported_and_keeps_non_technique_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTP_EVIDENCE_GATE", "on")
    text = "Attackers used PowerShell (T1059.001) to stage the payload."

    got = filter_llm_techniques(["T1059.001", "T1566.001", "CVE-2026-1"], text)

    assert got == ["T1059.001", "CVE-2026-1"]


def test_filter_shadow_keeps_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTP_EVIDENCE_GATE", "shadow")

    assert filter_llm_techniques(["T1566.001"], "no evidence here") == ["T1566.001"]


def test_filter_passes_all_when_body_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTP_EVIDENCE_GATE", "on")

    assert filter_llm_techniques(["T1566.001"], "   ") == ["T1566.001"]


def test_shipped_patterns_compile_and_are_keyed_by_technique_id() -> None:
    pats = load_evidence_patterns()

    assert pats, "config/cti/ttp_evidence.yaml が読めない"
    assert all(re.fullmatch(r"T\d{4}(\.\d{3})?", k) for k in pats)


class TestFilterByQuotes:
    """技術ごとの原文の引用が本文に在るものだけを採る (2026-09-29)。"""

    BODY = (
        "The attackers sent spear-phishing emails with a malicious Word attachment. "
        "Later they deployed a cron job for persistence on the server."
    )

    def test_keeps_technique_whose_quote_is_in_body(self) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        ev = [("T1566.001", "spear-phishing emails with a malicious Word attachment")]
        assert filter_by_quotes(["T1566.001"], ev, self.BODY) == ["T1566.001"]

    def test_drops_technique_without_quote_or_with_absent_quote(self) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        ev = [("T1041", "exfiltrated the data over the C2 channel")]  # 本文に無い
        assert filter_by_quotes(["T1041", "T1190"], ev, self.BODY) == []

    def test_quote_match_ignores_whitespace_and_case(self) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        ev = [("T1053.003", "Deployed  a CRON job\nfor persistence")]
        assert filter_by_quotes(["t1053.003"], ev, self.BODY) == ["t1053.003"]

    def test_too_short_quote_is_not_evidence(self) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        assert filter_by_quotes(["T1053.003"], [("T1053.003", "cron job")], self.BODY) == []

    def test_non_technique_values_pass_through(self) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        assert filter_by_quotes(["CVE-2026-0001"], [], self.BODY) == ["CVE-2026-0001"]

    def test_shadow_mode_keeps_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.cti.ttp_evidence import filter_by_quotes

        monkeypatch.setenv("TTP_EVIDENCE_GATE", "shadow")
        assert filter_by_quotes(["T1041"], [], self.BODY) == ["T1041"]


def test_summary_schema_for_uses_evidence_form_only_for_trained_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.pipeline import summary as mod

    monkeypatch.setattr(mod, "EVIDENCE_TRAINED_MODELS", frozenset({"kuebiko-sft:s22"}))
    assert mod.summary_schema_for("kuebiko-sft:s22") is mod.SummaryEvidenceOutput
    assert mod.summary_schema_for("kuebiko-sft:s21") is mod.SummaryOutput
    schema = mod.SummaryEvidenceOutput.model_json_schema()
    assert "mitre_evidence" in schema["required"]
    assert list(schema["properties"])[-1] == "mitre_evidence"
