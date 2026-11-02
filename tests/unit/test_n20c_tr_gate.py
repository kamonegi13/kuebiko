# ruff: noqa: E501
"""N20C: 翻訳・和訳の教師の関門 (data/mlx/n20_teacher/tr_gate.py) の単体試験。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data/mlx/n20_teacher"))

pytest.importorskip("tr_gate")  # data/ は gitignore: n20 の教師ディレクトリが無い環境では飛ばす

from tr_gate import (  # type: ignore[import-not-found]  # noqa: E402
    check_translation,
    detect_lang,
    echo_candidates,
    missing_identifiers,
    real_garble,
    refined_echo,
    structure_ok,
)

PROSE = (
    "Attackers can exploit critical vulnerabilities in Splunk Enterprise to escalate privileges and "
    "execute arbitrary code on the affected servers."
)
URL = "https://example.com/news/20261015/no-subject-for-talk-on-ukraine-with-e3--russian-deputy-foreign-minister.html"
JA_PROSE = "攻撃者は Splunk Enterprise の重大な脆弱性を悪用して権限を昇格し、影響を受けるサーバー上で任意のコードを実行できる。"


def test_faithful_translation_with_retained_url_passes_refined_gate() -> None:
    src = f"{PROSE}\n{URL}"
    out = f"{JA_PROSE}\n{URL}"

    res = check_translation(src, out)

    assert res.passed, res.reasons
    assert res.prod_echo_only  # 本番の判定だけは発火する (URL 行の保持)


def test_prose_echo_is_rejected() -> None:
    src = f"{PROSE}\n{URL}"

    assert refined_echo(src, f"{PROSE}\n{URL}")
    assert "echo" in check_translation(src, f"{PROSE}\n{URL}").reasons


def test_untranslated_prose_line_is_rejected() -> None:
    src = f"{PROSE}\n{PROSE} Second sentence adds more words here."
    out = f"{JA_PROSE}\nSecond line stayed English and has many words in it as well here."

    assert "untranslated_line" in check_translation(src, out).reasons


def test_missing_identifier_is_rejected() -> None:
    src = f"{PROSE} See CVE-2026-12345."
    out = f"{JA_PROSE} 参照: CVE-2026-12346。"

    assert missing_identifiers(src, out) == ["CVE-2026-12345"]
    assert "identifier_missing" in check_translation(src, out).reasons


def test_number_present_in_source_is_not_garble() -> None:
    """文末の年 (「since at least 2016.」) を訳文に書いても化けではない (検出器の文末の数字の取りこぼし)。"""
    src = "The group has been active since at least 2016. It targets banks in 2018, 2019 and later."
    out = (
        "このグループは少なくとも 2016 年から活動している。2018 年、2019 年以降も銀行を標的とする。"
    )

    assert real_garble(src, out) == (0, 0, 0)


def test_structure_allows_merged_lines_but_not_explosion() -> None:
    src = "\n".join(f"line {i} of the wrapped paragraph" for i in range(10))

    assert structure_ok(src, "段落を 1 行にまとめた訳")
    assert not structure_ok(src, "\n".join(f"行{i}" for i in range(30)))


def test_fence_and_preface_are_rejected() -> None:
    src = PROSE
    assert "fence_added" in check_translation(src, f"```\n{JA_PROSE}\n```").reasons
    assert "preface_or_fence" in check_translation(src, f"以下が訳です。\n{JA_PROSE}").reasons


def test_url_only_chunk_has_no_translatable_text() -> None:
    src = f"[1] {URL}\n[2] {URL}"

    assert "no_translatable_text" in check_translation(src, src).reasons


def test_echo_candidates_report_retainable_kind() -> None:
    kinds = [k for _, k in echo_candidates(f"{PROSE}\n{URL}")]

    assert kinds == [None, "url"]


def test_language_detection_distinguishes_french_from_portuguese() -> None:
    fr = "Une vulnérabilité a été découverte dans les produits Cisco. Elle permet une exécution de code à distance."
    pt = "Uma vulnerabilidade foi descoberta nos produtos da Cisco. Ela permite a execução de código não autorizado."

    assert detect_lang(fr) == "fr"
    assert detect_lang(pt) == "pt"
