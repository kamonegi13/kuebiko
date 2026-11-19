"""翻訳の保持行の追加規則 (タグ行・コード行) と旗 TRANSLATE_RETAIN_LINES_V2。"""

from __future__ import annotations

import pytest

from src.cti.body_translator import broken_translation_reason, find_echo_match
from src.cti.translate_passthrough import protect_chunk
from src.cti.translate_retain_rules import (
    KIND_CODE,
    KIND_TAG,
    classify_lines,
    retain_lines_v2_mode,
)

TAG = "place a, country b, some agency (abc), topic one, topic two, event"
PROSE = "The attackers abused a misconfigured gateway to deploy a loader across the whole network."


def _kind(line: str) -> str | None:
    return classify_lines([line])[0]


@pytest.mark.parametrize(
    "line",
    [
        TAG,
        "ukraine, russia, black sea, russian defense ministry, cargo ship, strike",
    ],
)
def test_tag_line_is_detected(line: str) -> None:
    assert _kind(line) == KIND_TAG


@pytest.mark.parametrize(
    "line",
    [
        "static bool heur_dissect_dplay(tvbuff_t *tvb, packet_info *pinfo, proto_tree *tree)",
        "DEFINE_GUID(IID_IDirectPlay2, 0x2b74f7c0, 0x9154, 0x11cf, 0xa9, 0xcd)",
        '| WHERE KQL("""user_agent.original : (curl* or python*)""")',
        "385: my $message = $params->{msg};",
    ],
)
def test_code_line_is_detected(line: str) -> None:
    assert _kind(line) == KIND_CODE


def test_hash_comment_is_code_only_next_to_code() -> None:
    code = "static bool f(tvbuff_t *tvb, packet_info *pinfo);"
    assert classify_lines(["# comment in script", code]) == [KIND_CODE, KIND_CODE]
    assert classify_lines(["# Heading of a section", PROSE]) == [None, None]
    assert classify_lines([code, "#Aspera #IBM"])[1] is None


@pytest.mark.parametrize(
    "line",
    [
        PROSE,
        "# Overview",
        "## Key findings",
        "- Attackers exploited the flaw in the wild, and patches are available now.",
        "Russia, Ukraine, and the European Union agreed to talks on the grain deal.",
        "Apple, Google, Microsoft, Amazon",  # 大文字始まりの固有名詞の列は対象外
        "ロシア、ウクライナ、欧州連合、北大西洋条約機構、国連、各国の政府や企業",
        "Check the list, patch what you have, and hit the ones marked urgent first — CVE-2026-1",
        "Industry: IT services, office equipment, cybersecurity, distribution",
        "The loader uses snake_case_names and 0x41 offsets to resolve the API table.",
        "https://example.test/some_path/some_file.php?file=1&x=2",
        "",
    ],
)
def test_prose_and_headings_are_not_retained(line: str) -> None:
    assert _kind(line) is None


def test_mode_defaults_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_RETAIN_LINES_V2", raising=False)
    assert retain_lines_v2_mode() == "0"
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "shadow")
    assert retain_lines_v2_mode() == "shadow"
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "ON")
    assert retain_lines_v2_mode() == "1"
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "bogus")
    assert retain_lines_v2_mode() == "0"


def _echo_pair() -> tuple[str, str]:
    source = f"{PROSE}\n{TAG * 2}"
    return source, "日本語の訳文。\n" + TAG * 2


def test_echo_flag_off_and_shadow_keep_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    source, output = _echo_pair()
    for mode in ("0", "shadow"):
        monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", mode)
        assert find_echo_match(source, output) is not None
        assert broken_translation_reason(source, output) == "原文がそのまま写っている"


def test_echo_flag_on_exempts_tag_line_but_not_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    source, output = _echo_pair()
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "1")
    assert find_echo_match(source, output) is None
    assert find_echo_match(source, output + "\n" + PROSE) is not None


def test_passthrough_tokenizes_new_kinds_only_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk = f"{PROSE}\n{TAG * 2}\n{PROSE}"
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "shadow")
    assert protect_chunk(chunk).kept_lines == 0
    monkeypatch.setenv("TRANSLATE_RETAIN_LINES_V2", "1")
    protected = protect_chunk(chunk)
    assert protected.kept_lines == 1
    assert protected.runs == (TAG * 2,)
