"""purge_llm_ioc_noise の選別が取込の関門と同じ基準であること。"""

from __future__ import annotations

from scripts.purge_llm_ioc_noise import is_noise, select_targets


def test_is_noise_matches_ingest_gate() -> None:
    assert is_noise("ioc_domain", "Math_Symbol.js")
    assert not is_noise("ioc_domain", "evil-c2.top")
    assert is_noise("ioc_ip", "8.8.8.8")
    assert is_noise("ioc_ip", "10.0.0.5")
    assert not is_noise("ioc_ip", "45.77.10.20")
    assert not is_noise("cve", "CVE-2026-1")


def test_select_targets() -> None:
    rows = [
        {"entity_type": "ioc_domain", "value": "setup.mjs"},
        {"entity_type": "ioc_domain", "value": "c2.example.top"},
    ]
    assert [r["value"] for r in select_targets(rows)] == ["setup.mjs"]
