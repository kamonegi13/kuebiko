"""Phase 2.5 K5b: NVD CVSS client のテスト (network はモック)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.tools.nvd_client as nvd


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストで cache path と mem cache を分離。"""
    monkeypatch.setattr(nvd, "_CACHE_PATH", tmp_path / "nvd.json")
    monkeypatch.setattr(nvd, "_mem_cache", None)
    # rate limit の sleep を無効化 (テスト高速化)
    monkeypatch.setattr("src.tools.nvd_client.time.sleep", lambda *_: None)


_V31 = {
    "vulnerabilities": [
        {
            "cve": {
                "metrics": {
                    "cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]
                }
            }
        }
    ]
}
_V2_ONLY = {
    "vulnerabilities": [
        {
            "cve": {
                "metrics": {
                    "cvssMetricV2": [{"cvssData": {"baseScore": 5.0}, "baseSeverity": "MEDIUM"}]
                }
            }
        }
    ]
}


class TestParse:
    def test_parses_v31(self) -> None:
        assert nvd._parse_cvss(_V31) == (9.8, "CRITICAL")

    def test_falls_back_to_v2(self) -> None:
        score, sev = nvd._parse_cvss(_V2_ONLY)  # type: ignore[misc]
        assert score == 5.0
        assert sev == "MEDIUM"

    def test_no_metrics_returns_none(self) -> None:
        assert nvd._parse_cvss({"vulnerabilities": [{"cve": {"metrics": {}}}]}) is None
        assert nvd._parse_cvss({"vulnerabilities": []}) is None


def _nvd_payload(score: float, severity: str) -> dict[str, object]:
    """_fetch_one の新契約 (NVD 生レスポンス dict) の最小形。

    2026-06-21 の CPE affected vendor 追加で _fetch_one は raw payload を返し
    呼出側が _parse_cvss/_parse_cpe する契約に変わった (テスト陳腐化の修正)。
    """
    return {
        "vulnerabilities": [
            {
                "cve": {
                    "metrics": {
                        "cvssMetricV31": [
                            {"cvssData": {"baseScore": score, "baseSeverity": severity}}
                        ]
                    },
                    "configurations": [],
                }
            }
        ]
    }


class TestRefreshAndGet:
    def test_refresh_fetches_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nvd, "_fetch_one", lambda cve: _nvd_payload(9.8, "CRITICAL"))
        n = nvd.refresh_cvss(["CVE-2026-1111"])
        assert n == 1
        assert nvd.get_cvss("CVE-2026-1111") == (9.8, "CRITICAL")
        # 大小無視
        assert nvd.get_cvss("cve-2026-1111") == (9.8, "CRITICAL")

    def test_get_uncached_returns_none(self) -> None:
        assert nvd.get_cvss("CVE-2026-9999") is None

    def test_invalid_cve_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = []
        monkeypatch.setattr(
            nvd,
            "_fetch_one",
            # append の副作用のみ利用し、None を or で読み飛ばして payload を返す意図的な idiom
            lambda cve: (
                called.append(cve)  # type: ignore[func-returns-value]
                or _nvd_payload(1.0, "LOW")
            ),
        )
        n = nvd.refresh_cvss(["not-a-cve", "CVE-bad"])
        assert n == 0
        assert called == []

    def test_max_fetch_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nvd, "_fetch_one", lambda cve: _nvd_payload(7.0, "HIGH"))
        n = nvd.refresh_cvss([f"CVE-2026-{i:04d}" for i in range(10)], max_fetch=3)
        assert n == 3

    def test_fetch_failure_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(cve: str) -> dict[str, object] | None:
            raise RuntimeError("nvd down")

        monkeypatch.setattr(nvd, "_fetch_one", _boom)
        assert nvd.refresh_cvss(["CVE-2026-1111"]) == 0
        assert nvd.get_cvss("CVE-2026-1111") is None

    def test_fresh_cache_skips_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 既に fresh な entry があれば fetch しない
        import time as _t

        nvd._mem_cache = {
            "CVE-2026-1111": {
                "base_score": 8.0,
                "severity": "HIGH",
                "fetched_at": _t.time(),
                # vendors 後付け移行期の補完再 fetch (nvd_client.py) を受けない完全形 entry
                "vendors": [],
                "products": [],
            }
        }
        calls = []
        monkeypatch.setattr(
            nvd,
            "_fetch_one",
            # append の副作用のみ利用し、None を or で読み飛ばして payload を返す意図的な idiom
            lambda cve: (
                calls.append(cve)  # type: ignore[func-returns-value]
                or _nvd_payload(1.0, "LOW")
            ),
        )
        n = nvd.refresh_cvss(["CVE-2026-1111"])
        assert n == 0
        assert calls == []

    def test_max_cvss_picks_highest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scores = {
            "CVE-2026-0001": _nvd_payload(4.0, "MEDIUM"),
            "CVE-2026-0002": _nvd_payload(9.1, "CRITICAL"),
        }
        monkeypatch.setattr(nvd, "_fetch_one", lambda cve: scores.get(cve.upper()))
        nvd.refresh_cvss(list(scores))
        assert nvd.max_cvss(["CVE-2026-0001", "CVE-2026-0002", "CVE-2026-9999"]) == 9.1
        assert nvd.max_cvss(["CVE-2026-9999"]) == 0.0


def test_recent_cve_values(tmp_path: Path) -> None:
    """storage: 直近 CVE を頻度降順で返す。"""
    from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

    repo = RunHistoryRepository(db_path=tmp_path / "cve.db")
    now = datetime.now(UTC)
    rid = repo.start_run(RunRecord(started_at=now, pipeline="x", dry_run=False))
    for aid, cves in [("a1", ["CVE-2026-1"]), ("a2", ["CVE-2026-1", "CVE-2026-2"])]:
        repo.add_article(
            ArticleRecord(run_id=rid, article_id=aid, title="t", url=f"u{aid}", status="posted")
        )
        repo.add_article_entities(aid, [("cve", c) for c in cves], when=now)
    # 古い CVE は窓外
    repo.add_article(
        ArticleRecord(run_id=rid, article_id="old", title="t", url="uold", status="posted")
    )
    repo.add_article_entities("old", [("cve", "CVE-2000-9")], when=now - timedelta(days=40))

    vals = repo.recent_cve_values(days=14)
    assert vals[0] == "CVE-2026-1"  # 2 回出現 → 先頭
    assert "CVE-2026-2" in vals
    assert "CVE-2000-9" not in vals
