"""識別子照合の単独所有モジュール (src/tools/identifier_match.py) のテスト。

UNC70 ⊂ UNC7005 の誤判定を再現しない (トークン境界) / defang IP の照合 /
破損識別子の repair / 全角・引用符差の吸収 / version の単語境界 / repair 曖昧時 None、
を固定する (docs/event_news_design.md §9)。
"""

from __future__ import annotations

from src.tools.identifier_match import (
    Identifier,
    IdentifierKind,
    contains_identifier,
    extract_identifiers,
    find_repair_candidate,
    normalize_identifier,
)


def _ident(kind: IdentifierKind, raw: str) -> Identifier:
    return Identifier(kind=kind, raw=raw, normalized=normalize_identifier(raw))


# ---------- UNC70 ⊂ UNC7005 (トークン境界) ----------


def test_unc70_does_not_match_unc7005_haystack() -> None:
    ident = _ident("actor_id", "UNC70")
    haystack = "UNC7005 が関与したとされる侵害が報告された。"
    assert contains_identifier(haystack, ident) is False


def test_unc70_matches_haystack_with_true_boundary() -> None:
    ident = _ident("actor_id", "UNC70")
    haystack = "UNC70 が関与したとされる侵害が報告された。"
    assert contains_identifier(haystack, ident) is True


def test_extract_identifiers_finds_actor_id_full_number() -> None:
    idents = extract_identifiers("UNC7005 の活動が観測された。")
    actor_idents = [i for i in idents if i.kind == "actor_id"]
    assert any(a.normalized == "unc7005" for a in actor_idents)
    assert not any(a.normalized == "unc70" for a in actor_idents)


# ---------- defang IP ----------


def test_extract_identifiers_finds_defanged_ip() -> None:
    idents = extract_identifiers("C2 サーバは 23.254.165[.]112 だった。")
    ip_idents = [i for i in idents if i.kind == "ip"]
    assert len(ip_idents) == 1
    assert ip_idents[0].normalized == "23.254.165.112"


def test_contains_identifier_matches_defanged_form_in_haystack() -> None:
    ident = _ident("ip", "23.254.165.112")
    haystack = "通信先は 23.254.165[.]112 だった。"
    assert contains_identifier(haystack, ident) is True


def test_normalize_identifier_restores_defang_and_keeps_punctuation() -> None:
    assert normalize_identifier("23.254.165[.]112") == "23.254.165.112"
    assert normalize_identifier("hxxps://evil[.]example[.]com") == "https://evil.example.com"


# ---------- 破損識別子の repair ----------


def test_find_repair_candidate_resolves_broken_ip() -> None:
    broken = _ident("ip", "2CA.254.165[.]112")
    allowed = (_ident("ip", "23.254.165.112"),)
    result = find_repair_candidate(broken, allowed)
    assert result is not None
    assert result.raw == "23.254.165.112"


# ---------- 全角・引用符差 ----------


def test_contains_identifier_handles_fullwidth_and_quotes() -> None:
    ident = _ident("cve", "CVE-2024-1234")
    haystack = "脆弱性 “ＣＶＥ－２０２４－１２３４” が公開された。"
    assert contains_identifier(haystack, ident) is True


def test_normalize_identifier_applies_nfkc_casefold() -> None:
    assert normalize_identifier("ＵＮＣ７００５") == "unc7005"


# ---------- version の単語境界 ----------


def test_extract_identifiers_finds_version_with_boundary() -> None:
    idents = extract_identifiers("OpenSSL 3.0.9 で修正された。")
    versions = [i for i in idents if i.kind == "version"]
    assert any(v.normalized == "3.0.9" for v in versions)


def test_extract_identifiers_does_not_split_longer_ip_into_version() -> None:
    idents = extract_identifiers("内部 IP 192.168.1.10 と通信していた。")
    versions = [i for i in idents if i.kind == "version"]
    assert versions == []


# ---------- repair 曖昧時 None ----------


def test_find_repair_candidate_returns_none_when_multiple_candidates() -> None:
    broken = _ident("ip", "2CA.254.165[.]112")
    allowed = (_ident("ip", "23.254.165.112"), _ident("ip", "88.99.1.5"))
    assert find_repair_candidate(broken, allowed) is None


def test_find_repair_candidate_returns_none_when_too_different() -> None:
    broken = _ident("ip", "1.1.1.1")
    allowed = (_ident("ip", "203.0.113.99"),)
    assert find_repair_candidate(broken, allowed) is None


def test_find_repair_candidate_ignores_different_kind() -> None:
    broken = _ident("ip", "2CA.254.165[.]112")
    allowed = (_ident("domain", "example.com"),)
    assert find_repair_candidate(broken, allowed) is None


def test_find_repair_candidate_returns_none_when_no_allowed() -> None:
    broken = _ident("ip", "23.254.165.112")
    assert find_repair_candidate(broken, ()) is None


# ---------- その他 (cve/hash/cvss の抽出サニティ) ----------


def test_extract_identifiers_finds_cve_and_hash() -> None:
    text = (
        "CVE-2024-12345 の悪用が確認された。ハッシュ値は"
        " 44d88612fea8a8f36de82e1278abb02f (MD5) と一致した。"
    )
    idents = extract_identifiers(text)
    kinds = {i.kind for i in idents}
    assert "cve" in kinds
    assert "hash" in kinds


def test_extract_identifiers_finds_cvss_score() -> None:
    idents = extract_identifiers("CVSS スコアは 9.8 (Critical) と評価された。")
    cvss_idents = [i for i in idents if i.kind == "cvss"]
    assert any(c.normalized == "9.8" for c in cvss_idents)


def test_extract_identifiers_empty_text_returns_empty_tuple() -> None:
    assert extract_identifiers("") == ()
