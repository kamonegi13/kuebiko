"""src.cti.ioc_extractor のテスト (Phase 4 + 5T-N)。"""

from __future__ import annotations

import pytest

from src.cti.ioc_extractor import (
    extract_iocs,
    merge_iocs,
    merge_techniques,
    refang,
)

# ---------- refang ----------


def test_refang_brackets() -> None:
    assert refang("evil[.]malicious-test[.]io") == "evil.malicious-test.io"
    assert refang("evil(.)malicious-test(.)io") == "evil.malicious-test.io"
    assert refang("evil{.}io") == "evil.io"


def test_refang_hxxp() -> None:
    assert refang("hxxp://evil[.]com/x") == "http://evil.com/x"
    assert refang("hxxps://evil[.]com") == "https://evil.com"
    assert refang("HXXP://EVIL[.]COM") == "HTTP://EVIL.COM"


def test_refang_at_sign() -> None:
    assert refang("attacker[@]malicious-test.io") == "attacker@malicious-test.io"


def test_refang_empty() -> None:
    assert refang("") == ""
    assert refang("plain text") == "plain text"


# ---------- CVE ----------


def test_extract_cve() -> None:
    text = "Multiple CVEs: CVE-2024-12345 and CVE-2025-99999"
    out = extract_iocs(text)
    assert "CVE-2024-12345" in out.cves
    assert "CVE-2025-99999" in out.cves
    assert len(out.cves) == 2


def test_extract_cve_old_year() -> None:
    """1999 形式も拾う (古い CVE 参照)。"""
    out = extract_iocs("Old CVE-1999-0001")
    assert "CVE-1999-0001" in out.cves


def test_extract_cve_lowercase() -> None:
    out = extract_iocs("cve-2024-1234 and CVE-2024-5678")
    assert len(out.cves) == 2


# ---------- MITRE ATT&CK ----------


def test_extract_mitre_techniques() -> None:
    text = "Used T1078 (Valid Accounts) and T1059.003 (cmd.exe)"
    out = extract_iocs(text)
    assert "T1078" in out.mitre_techniques
    assert "T1059.003" in out.mitre_techniques


def test_extract_mitre_groups() -> None:
    out = extract_iocs("Attributed to G0016 (APT29)")
    assert "G0016" in out.mitre_groups


# ---------- IPv4 ----------


def test_extract_ipv4_global() -> None:
    # Phase 5T-N: 8.8.8.8 は IoC context (C2) ありで keep される、
    # 203.0.113.45 は RFC 5737 doc range なので別の例 (45.55.66.77) に変更
    out = extract_iocs("C2 server: 8.8.8.8 and attacker 45.55.66.77")
    assert "8.8.8.8" in out.ipv4
    assert "45.55.66.77" in out.ipv4


def test_extract_ipv4_excludes_private() -> None:
    text = "Internal: 10.0.0.1, 192.168.1.1, 172.16.0.1, 127.0.0.1, 0.0.0.0"
    out = extract_iocs(text)
    assert out.ipv4 == ()


def test_extract_ipv4_with_defang() -> None:
    out = extract_iocs("C2: 1[.]2[.]3[.]4")
    assert "1.2.3.4" in out.ipv4


# ---------- ドメイン / URL ----------


def test_extract_domain() -> None:
    out = extract_iocs("Phishing: malicious-domain.tk hosts the C2")
    assert "malicious-domain.tk" not in out.domains  # tk は TLD にないので拾わない
    out2 = extract_iocs("Phishing: evil.malicious-test.io hosts payload")
    assert "evil.malicious-test.io" in out2.domains


def test_extract_url() -> None:
    out = extract_iocs("Payload at https://evil.malicious-test.io/payload.exe")
    assert any("evil.malicious-test.io" in u for u in out.urls)


def test_extract_defanged_url() -> None:
    out = extract_iocs("Payload at hxxps://evil[.]malicious-test[.]io/payload.exe")
    assert any("evil.malicious-test.io" in u for u in out.urls)


def test_benign_domain_excluded() -> None:
    """CISA / MITRE 等の解説ドメインは IOC として出さない。"""
    text = "See https://attack.mitre.org/techniques/T1078/ and https://www.cisa.gov/alerts"
    out = extract_iocs(text)
    assert out.urls == ()
    assert out.domains == ()


def test_domain_already_in_url_not_duplicated() -> None:
    """URL に含まれているドメインは bare domain として重複追加しない。"""
    text = "hosted at https://evil.malicious-test.io/x and evil.malicious-test.io itself"
    out = extract_iocs(text)
    # urls に 1 件だけ、domains は空 (URL 内に含まれているため)
    assert len(out.urls) == 1
    assert "evil.malicious-test.io" not in out.domains


# ---------- Hash ----------


def test_extract_md5() -> None:
    text = "MD5: 5d41402abc4b2a76b9719d911017c592"
    out = extract_iocs(text)
    assert "5d41402abc4b2a76b9719d911017c592" in out.md5


def test_extract_sha256() -> None:
    text = "SHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    out = extract_iocs(text)
    assert "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" in out.sha256


def test_hash_categorization_disjoint() -> None:
    """SHA256 は MD5 リストに重複して入らない。"""
    out = extract_iocs(
        "MD5 5d41402abc4b2a76b9719d911017c592 "
        "SHA1 aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d "
        "SHA256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    assert len(out.md5) == 1
    assert len(out.sha1) == 1
    assert len(out.sha256) == 1
    # SHA256 は 64 桁、SHA1 は 40 桁、MD5 は 32 桁でそれぞれ独立判定される


# ---------- Email ----------


def test_extract_email() -> None:
    out = extract_iocs("Send to attacker@malicious-test.io")
    assert "attacker@malicious-test.io" in out.emails


def test_extract_defanged_email() -> None:
    out = extract_iocs("Send to attacker[@]malicious-test.io")
    assert "attacker@malicious-test.io" in out.emails


# ---------- マージ ----------


def test_merge_iocs_dedupes() -> None:
    extracted = extract_iocs("CVE-2024-1234 evil.malicious-test.io")
    merged = merge_iocs(["CVE-2024-1234", "CVE-2025-9999"], extracted)
    # 重複なし、両方含む
    assert "CVE-2024-1234" in merged
    assert "CVE-2025-9999" in merged
    assert "evil.malicious-test.io" in merged
    assert merged.count("CVE-2024-1234") == 1


def test_merge_iocs_handles_defanged_llm_output() -> None:
    """LLM が defang 形式で返してきても重複と認識する。"""
    extracted = extract_iocs("evil.malicious-test.io")
    # LLM 由来が defang 形式
    merged = merge_iocs(["evil[.]malicious-test[.]io"], extracted)
    # 重複として 1 件だけ
    assert len(merged) == 1


def test_merge_techniques_normalizes_case() -> None:
    extracted = extract_iocs("T1078 and t1059.003")
    merged = merge_techniques(["T1078", "T1505.003"], extracted)
    # 大文字統一、重複なし
    assert "T1078" in merged
    assert "T1059.003" in merged
    assert "T1505.003" in merged
    assert merged.count("T1078") == 1


# ---------- 全体 ----------


def test_complex_cti_text() -> None:
    """実際の CTI 報告に近い複合テキストの抽出。"""
    text = (
        "CISA issued an emergency alert about CVE-2024-99999. "
        "The attacker (G0016, attributed APT29) used T1078 and T1059.003 "
        "techniques. C2 infrastructure: 45.55.66.77 and "
        "hxxps://evil[.]malicious-test[.]io/payload.exe. "
        "Malware MD5: 5d41402abc4b2a76b9719d911017c592. "
        "See https://www.cisa.gov/alerts/aa24-XXX for details."
    )
    out = extract_iocs(text)
    assert "CVE-2024-99999" in out.cves
    assert "G0016" in out.mitre_groups
    assert "T1078" in out.mitre_techniques
    assert "T1059.003" in out.mitre_techniques
    assert "45.55.66.77" in out.ipv4
    assert any("evil.malicious-test.io" in u for u in out.urls)
    assert "5d41402abc4b2a76b9719d911017c592" in out.md5
    # cisa.gov は除外
    assert all("cisa.gov" not in u for u in out.urls)


# ---------- Phase 5F: filter_benign + 拡張 benign list ----------


def test_filter_benign_strips_vendor_urls() -> None:
    """LLM 出力に出典 URL が混入した場合の二重防御。"""
    from src.cti.ioc_extractor import filter_benign

    iocs = [
        "https://www.tenable.com/blog/article",
        "https://x.com/example/status/1",
        "evil.malicious.example",
        "CVE-2024-99999",
        "45.55.66.77",
    ]
    out = filter_benign(iocs)
    # benign URL は除外
    assert all("tenable.com" not in s for s in out)
    assert all("x.com" not in s for s in out)
    # 悪性ドメイン / CVE / IP は残る
    assert "evil.malicious.example" in out
    assert "CVE-2024-99999" in out
    assert "45.55.66.77" in out


def test_filter_benign_strips_grok_urls() -> None:
    from src.cti.ioc_extractor import filter_benign

    iocs = ["https://grok.com/chat/abc", "CVE-2024-12345"]
    out = filter_benign(iocs)
    assert "https://grok.com/chat/abc" not in out
    assert "CVE-2024-12345" in out


def test_extended_benign_list_includes_cti_vendors() -> None:
    """CTI ベンダードメインが extract_iocs の段で除外されることを確認。"""
    from src.cti.ioc_extractor import extract_iocs

    text = (
        "新たな侵害が観測された。詳細は https://www.sentinelone.com/labs/abc を参照。"
        "またフラグは https://blog.talosintelligence.com/x にも掲載。"
        "実際の C2 ドメインは evil-c2.malicious-test.io。"
    )
    out = extract_iocs(text)
    # ベンダードメインは除外、悪性ドメインは抽出
    domains = list(out.domains)
    assert all("sentinelone.com" not in d for d in domains)
    assert all("talosintelligence.com" not in d for d in domains)
    assert any("evil-c2.malicious-test.io" in d for d in domains)


# ---------- Phase 5J-3: extended benign list + refang in merge_iocs ----------


def test_extended_benign_list_includes_news_media() -> None:
    """Phase 5J-3: 観測ベース拡張で gbhackers / socradar / 中国語 / 日本語メディアが除外。"""
    from src.cti.ioc_extractor import extract_iocs

    text = (
        "Source: https://www.gbhackers.com/article/ "
        "Related: https://socradar.io/blog/x "
        "JP: https://it.itmedia.co.jp/news/y "
        "CN: https://www.scmp.com/news/z "
        "Real C2: malicious-c2.bad-actor.tools"
    )
    out = extract_iocs(text)
    domains = list(out.domains)
    assert all("gbhackers.com" not in d for d in domains)
    assert all("socradar.io" not in d for d in domains)
    assert all("itmedia.co.jp" not in d for d in domains)
    assert all("scmp.com" not in d for d in domains)
    # 悪性 C2 は残る
    assert any("malicious-c2.bad-actor.tools" in d for d in domains)


def test_merge_iocs_refangs_defanged_input() -> None:
    """Phase 5J-3: LLM 出力の defanged 表記を refang してから保存。"""
    from src.cti.ioc_extractor import ExtractedIocs, merge_iocs

    llm_iocs = [
        "evil[.]malicious-test[.]io",  # defanged domain
        "hxxp://attacker.example/",  # defanged URL
        "1[.]2[.]3[.]4",  # defanged IPv4
        "CVE-2024-12345",  # 通常
    ]
    out = merge_iocs(llm_iocs, ExtractedIocs())
    # refang 済みになっている
    assert "evil.malicious-test.io" in out
    assert "http://attacker.example/" in out
    assert "1.2.3.4" in out
    assert "CVE-2024-12345" in out
    # defanged 形式は残らない
    assert all("[.]" not in s for s in out)
    assert all("hxxp" not in s for s in out)


def test_merge_iocs_dedupes_after_refang() -> None:
    """defanged と refanged が同じ IOC を指す場合、1 件にまとまる。"""
    from src.cti.ioc_extractor import ExtractedIocs, merge_iocs

    llm_iocs = ["evil[.]malicious-test[.]io"]
    extracted = ExtractedIocs(domains=("evil.malicious-test.io",))
    out = merge_iocs(llm_iocs, extracted)
    # 重複排除されて 1 件
    assert len([s for s in out if "evil.malicious-test.io" in s]) == 1


# ---------- Phase 5T-N: 誤抽出 FP 抑制 (version/DNS/URL/self-ref/library/git) ----------


class TestPhase5tnVersionAsIp:
    """Software version 番号が IPv4 として誤抽出される問題の修正検証。"""

    def test_fuji_electric_version_dropped(self) -> None:
        """JVN advisory で実観測した 'ver 6.2.10.0 and prior' は drop される。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Fuji Electric Co., Ltd. V-SFT ver 6.2.10.0 and prior Opening a crafted V7 file"
        out = extract_iocs(text)
        assert "6.2.10.0" not in out.ipv4

    def test_cisco_product_version_dropped(self) -> None:
        """'Cisco IOS XE 17.16.1.4' のような vendor + version は drop。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Cisco IOS XE 17.16.1.4 has the fix for this vulnerability"
        out = extract_iocs(text)
        assert "17.16.1.4" not in out.ipv4

    def test_explicit_version_keyword_dropped(self) -> None:
        """'version 1.2.3.4' は drop される。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "The patched version 1.2.3.4 fixes this. Older versions are vulnerable."
        out = extract_iocs(text)
        assert "1.2.3.4" not in out.ipv4

    def test_range_expression_dropped(self) -> None:
        """'7.0.5 and prior' のような range 表現は drop される。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Affected: 7.0.5 and prior. Fixed in 7.0.6"
        out = extract_iocs(text)
        assert "7.0.5" not in out.ipv4
        assert "7.0.6" not in out.ipv4

    def test_real_ip_with_ioc_context_kept(self) -> None:
        """C2 文脈の IP は keep される (false negative 防止)。"""
        from src.cti.ioc_extractor import extract_iocs

        # 全 octet ≤ 20 だが C2 context あり → keep
        text = "The C2 server at 5.6.7.8 received the exfiltrated data"
        out = extract_iocs(text)
        assert "5.6.7.8" in out.ipv4

    def test_real_global_ip_kept(self) -> None:
        """大きい octet を含む通常の IPv4 は keep。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Connection attempt from 198.51.50.100 was blocked"
        out = extract_iocs(text)
        assert "198.51.50.100" in out.ipv4


class TestPhase5tnBenignIps:
    """Public DNS / RFC 5737 documentation IP の benign 扱い。"""

    def test_rfc5737_documentation_ips_dropped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        text = "Documentation example: 192.0.2.5 and 198.51.100.42 and 203.0.113.1"
        out = extract_iocs(text)
        assert "192.0.2.5" not in out.ipv4
        assert "198.51.100.42" not in out.ipv4
        assert "203.0.113.1" not in out.ipv4

    def test_public_dns_dropped_without_ioc_context(self) -> None:
        """1.1.1.1 / 8.8.8.8 は 攻撃文脈なしなら drop。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Cloudflare provides 1.1.1.1 and Google has 8.8.8.8 as public DNS"
        out = extract_iocs(text)
        assert "1.1.1.1" not in out.ipv4
        assert "8.8.8.8" not in out.ipv4

    def test_public_dns_kept_with_ioc_context(self) -> None:
        """1.1.1.1 でも 攻撃文脈ありなら keep (malware が DNS として使うケース)。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "The malware resolved to 1.1.1.1 to retrieve C2 commands"
        out = extract_iocs(text)
        assert "1.1.1.1" in out.ipv4


class TestPhase5tnUrlImprovements:
    """URL truncation, self-ref, CDN, library 名 抑制。"""

    def test_url_trailing_semicolon_stripped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        text = "See https://evil.example.io/path/page; for details"
        out = extract_iocs(text)
        # ; が末尾に残らない
        assert all(not u.endswith(";") for u in out.urls)
        # URL 自体は抽出される (末尾 ; なし版)
        assert any("evil.example.io" in u for u in out.urls)

    def test_url_trailing_japanese_punctuation_stripped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        text = "詳細は https://evil.example.io/x/y。確認してください"
        out = extract_iocs(text)
        assert all(not u.endswith("。") for u in out.urls)

    def test_self_referencing_url_dropped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        text = (
            "See related article at https://example.io/related/post and "
            "external link https://other.io/x"
        )
        out = extract_iocs(text, source_url="https://example.io/article")
        # self-ref は drop、外部 URL は keep
        assert not any("example.io" in u for u in out.urls)
        assert any("other.io" in u for u in out.urls)

    def test_cdn_subdomain_url_dropped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        text = "Image hosted at https://cdn.example.io/img/photo.jpg"
        out = extract_iocs(text)
        assert not any("cdn.example.io" in u for u in out.urls)

    def test_library_pseudo_domain_dropped(self) -> None:
        """Socket.IO のような library 名は domain として扱わない。"""
        from src.cti.ioc_extractor import extract_iocs

        text = "Real-time Socket.IO applications and Node.js servers"
        out = extract_iocs(text)
        assert "socket.io" not in [d.lower() for d in out.domains]


class TestPhase5tnGitCommitContext:
    """Git commit hash (40-hex) を SHA-1 IoC として扱わない。"""

    def test_github_commit_link_hash_dropped(self) -> None:
        from src.cti.ioc_extractor import extract_iocs

        commit_hash = "a" * 40  # 40 hex chars
        text = f"See github.com/owner/repo/commit/{commit_hash} for the fix"
        out = extract_iocs(text)
        assert commit_hash not in out.sha1

    def test_malware_hash_kept(self) -> None:
        """通常の SHA-1 malware hash は keep。"""
        from src.cti.ioc_extractor import extract_iocs

        malware_hash = "1234567890abcdef1234567890abcdef12345678"  # 40 hex
        text = f"Malicious file detected with SHA1: {malware_hash}"
        out = extract_iocs(text)
        assert malware_hash in out.sha1


class TestPhase5tnLlmVerifier:
    """Layer 2: LLM verifier の動作検証 (mock LLM)。"""

    @pytest.mark.asyncio
    async def test_llm_keeps_real_ioc(self) -> None:
        from unittest.mock import AsyncMock

        from src.cti.ioc_extractor import ExtractedIocs
        from src.cti.ioc_llm_verifier import IocDecision, IocVerifyOutput, verify_iocs_with_llm
        from src.tools.llm_client import LLMClient

        extracted = ExtractedIocs(ipv4=("9.8.7.6",))
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=IocVerifyOutput(
                decisions=[IocDecision(id=0, action="keep", reason="C2 IP")],
            ),
        )
        out = await verify_iocs_with_llm(extracted, "C2 at 9.8.7.6", llm=llm)
        assert "9.8.7.6" in out.ipv4

    @pytest.mark.asyncio
    async def test_llm_drops_misidentified_ioc(self) -> None:
        from unittest.mock import AsyncMock

        from src.cti.ioc_extractor import ExtractedIocs
        from src.cti.ioc_llm_verifier import IocDecision, IocVerifyOutput, verify_iocs_with_llm
        from src.tools.llm_client import LLMClient

        extracted = ExtractedIocs(domains=("socket.io",))
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=IocVerifyOutput(
                decisions=[IocDecision(id=0, action="drop", reason="library name")],
            ),
        )
        out = await verify_iocs_with_llm(extracted, "uses socket.io for real-time", llm=llm)
        assert "socket.io" not in out.domains

    @pytest.mark.asyncio
    async def test_llm_failure_returns_original(self) -> None:
        """LLM 失敗時は regex 結果をそのまま返す (graceful degradation)。"""
        from unittest.mock import AsyncMock

        from src.cti.ioc_extractor import ExtractedIocs
        from src.cti.ioc_llm_verifier import verify_iocs_with_llm
        from src.tools.llm_client import LLMClient

        extracted = ExtractedIocs(ipv4=("1.2.3.4",))
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
        out = await verify_iocs_with_llm(extracted, "...", llm=llm)
        # LLM 失敗時は original を返す
        assert "1.2.3.4" in out.ipv4

    @pytest.mark.asyncio
    async def test_empty_extracted_skips_llm(self) -> None:
        """候補なしのとき LLM を呼ばずに早期 return。"""
        from unittest.mock import AsyncMock

        from src.cti.ioc_extractor import ExtractedIocs
        from src.cti.ioc_llm_verifier import verify_iocs_with_llm
        from src.tools.llm_client import LLMClient

        extracted = ExtractedIocs()
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock()
        out = await verify_iocs_with_llm(extracted, "no iocs here", llm=llm)
        llm.generate_structured.assert_not_called()
        assert out == extracted
