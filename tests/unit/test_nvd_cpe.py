"""nvd_client の affected vendor/product (CPE) 抽出のテスト (affected product タグの基盤)。"""

from __future__ import annotations

import src.tools.nvd_client as nc
from src.tools.nvd_client import (
    _parse_cpe,
    all_affected,
    cves_for_affected,
)


def test_parse_cpe_extracts_vendor_product() -> None:
    payload = {
        "vulnerabilities": [
            {
                "cve": {
                    "configurations": [
                        {
                            "nodes": [
                                {
                                    "cpeMatch": [
                                        {"criteria": "cpe:2.3:a:fortinet:fortios:7.0.0:*:*"},
                                        {"criteria": "cpe:2.3:a:fortinet:fortiproxy:*:*:*"},
                                        {"criteria": "cpe:2.3:o:*:*:*:*:*"},  # * は無視
                                    ]
                                }
                            ]
                        }
                    ]
                }
            }
        ]
    }
    vendors, products = _parse_cpe(payload)
    assert vendors == ["fortinet"]
    assert "fortios" in products and "fortiproxy" in products


def test_parse_cpe_empty_when_no_config() -> None:
    assert _parse_cpe({}) == ([], [])
    assert _parse_cpe({"vulnerabilities": []}) == ([], [])


def test_cves_for_affected_matches_vendor_or_product(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        nc,
        "_mem_cache",
        {
            "CVE-2026-1": {"vendors": ["fortinet"], "products": ["fortios"]},
            "CVE-2026-2": {"vendors": ["citrix"], "products": ["netscaler"]},
        },
    )
    # vendor 名でも product 名でも逆引きできる (1 入力で両粒度)
    assert cves_for_affected("Fortinet") == ["CVE-2026-1"]
    assert cves_for_affected("fortios") == ["CVE-2026-1"]
    assert cves_for_affected("netscaler") == ["CVE-2026-2"]
    assert cves_for_affected("unknown") == []
    assert all_affected() == ["citrix", "fortinet", "fortios", "netscaler"]
