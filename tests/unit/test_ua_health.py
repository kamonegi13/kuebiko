"""UA 自己修復ジョブの単体テスト (2026-07-27)。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from src.storage.run_history import RunHistoryRepository, RunRecord
from src.ui.services import ua_health


@pytest.fixture(autouse=True)
def _bypass_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """canary は MockTransport の擬似ホストなので SSRF/DNS 検証を no-op に (単体テスト用)。"""
    monkeypatch.setattr(ua_health, "assert_safe_public_url", lambda url: None)


def _canary_client(*, ok_uas: set[str]) -> httpx.AsyncClient:
    """許可 UA のみ 200、他は 403 を返す MockTransport のクライアント (WAF 模倣)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("User-Agent", "")
        return httpx.Response(200 if ua in ok_uas else 403, text="ok")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_candidate_uas_bumps_chrome_major() -> None:
    """候補 UA は現行 Chrome major から +1..+3 の Mac/Win を生成する。"""
    cands = ua_health._candidate_uas("... Chrome/133.0.0.0 Safari/537.36")
    assert any("Chrome/134" in c for c in cands)
    assert any("Chrome/136" in c for c in cands)
    assert any("Windows NT" in c for c in cands)
    # Chrome 形式でなければ空
    assert ua_health._candidate_uas("Firefox/1.0") == []


@pytest.mark.asyncio
async def test_probe_success_ratio_counts_200() -> None:
    ok = "Mozilla/5.0 GOOD"
    async with _canary_client(ok_uas={ok}) as client:
        assert await ua_health._probe_success_ratio(client, ok, ["https://a.example/x"]) == 1.0
        assert await ua_health._probe_success_ratio(client, "BAD", ["https://a.example/x"]) == 0.0
        assert await ua_health._probe_success_ratio(client, ok, []) == 0.0


@pytest.mark.asyncio
async def test_self_heals_when_current_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """現行 UA が 403、候補 (Chrome+1) が 200 なら .env/os.environ を更新して self_healed。"""
    canaries = ["https://a.example/1", "https://b.example/2"]
    healed_ua = ua_health._chrome_ua(134)  # Chrome+1 (Mac)

    class _Repo:
        def sample_healthy_source_urls(self, limit: int = 8) -> list[str]:
            return canaries

    adopted: dict[str, str] = {}
    monkeypatch.setattr(ua_health, "browser_user_agent", lambda: ua_health._chrome_ua(133))
    monkeypatch.setattr(ua_health, "_adopt_ua", lambda ua: adopted.update(ua=ua))

    async def _fake_notify(title: str, body: str) -> None:
        return None

    monkeypatch.setattr(ua_health, "_notify", _fake_notify)

    with patch("httpx.AsyncClient", return_value=_canary_client(ok_uas={healed_ua})):
        result = await ua_health.run_ua_health_check(_Repo())  # type: ignore[arg-type]

    assert result["status"] == "self_healed"
    assert adopted["ua"] == healed_ua


@pytest.mark.asyncio
async def test_healthy_current_no_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """現行 UA が健全なら候補を試さず healthy を返す。"""
    current = ua_health._chrome_ua(133)

    class _Repo:
        def sample_healthy_source_urls(self, limit: int = 8) -> list[str]:
            return ["https://a.example/1"]

    monkeypatch.setattr(ua_health, "browser_user_agent", lambda: current)
    with patch("httpx.AsyncClient", return_value=_canary_client(ok_uas={current})):
        result = await ua_health.run_ua_health_check(_Repo())  # type: ignore[arg-type]
    assert result["status"] == "healthy"


def test_adopt_ua_sets_environ(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """_adopt_ua は os.environ を即時更新する (再起動不要の in-process 反映)。"""
    monkeypatch.setattr(ua_health, "update_env", lambda *a, **k: None, raising=False)
    new = ua_health._chrome_ua(140)
    with patch("src.ui.services.env_editor.update_env"):
        ua_health._adopt_ua(new)
    assert os.environ["CONTENT_EXTRACTOR_USER_AGENT"] == new


# ---- canary 抽出クエリ (実 SQL を通す。stub repo では素通りしていた層) ----


def _repo_with_run(db_path: Path) -> tuple[RunHistoryRepository, int]:
    """空 DB と、articles の FK を満たす run を 1 件用意する。"""
    repo = RunHistoryRepository(db_path=db_path)
    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="ua-test", dry_run=True)
    )
    return repo, run_id


def _insert_article(
    repo: RunHistoryRepository,
    run_id: int,
    *,
    aid: str,
    url: str,
    feed: str,
    body_source: str,
    created: str,
) -> None:
    """canary クエリが読む列だけを直接 INSERT する (add_article は body_source を書かない)。"""
    with repo._connect() as conn:
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, feed_title, status,"
            " body_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, aid, aid, url, feed, "posted", body_source, created),
        )


def test_sample_healthy_source_urls_picks_latest_per_feed(tmp_path: Path) -> None:
    """feed ごとに最新 1 件だけを新しい順に返す (実 SQL を実行する回帰テスト)。

    旧実装は ``SELECT url, MAX(created_at) … GROUP BY feed_title`` で、SQLite の bare
    column 拡張に依存していた。PG では GroupingError になり production で全滅した。
    """
    repo, rid = _repo_with_run(tmp_path / "ua.db")
    _insert_article(
        repo,
        rid,
        aid="a1",
        url="u/old",
        feed="Alpha",
        body_source="full_extract",
        created="2026-08-01",
    )
    _insert_article(
        repo,
        rid,
        aid="a2",
        url="u/new",
        feed="Alpha",
        body_source="full_extract",
        created="2026-08-03",
    )
    _insert_article(
        repo,
        rid,
        aid="b1",
        url="u/beta",
        feed="Beta",
        body_source="playwright_extract",
        created="2026-08-02",
    )

    # feed ごとに最新 1 件 (Alpha は u/new)、全体は created_at の新しい順
    assert repo.sample_healthy_source_urls() == ["u/new", "u/beta"]
    assert repo.sample_healthy_source_urls(limit=1) == ["u/new"]


def test_sample_healthy_source_urls_filters_non_canary_rows(tmp_path: Path) -> None:
    """全文でない body_source と Grok/ransomware.live (大小混在) は canary にしない。"""
    repo, rid = _repo_with_run(tmp_path / "ua2.db")
    _insert_article(
        repo,
        rid,
        aid="ok",
        url="u/ok",
        feed="Alpha",
        body_source="full_extract",
        created="2026-08-01",
    )
    # 抜粋 fallback 等の非全文は「取れていた」証拠にならない
    _insert_article(
        repo,
        rid,
        aid="x1",
        url="u/x1",
        feed="Beta",
        body_source="feed_summary",
        created="2026-08-02",
    )
    # URL 直取得しない経路。feed_title の大小は取込元により揺れるため両方除外する
    for i, feed in enumerate(("Grok", "grok", "Ransomware.live", "ransomware.live")):
        _insert_article(
            repo,
            rid,
            aid=f"s{i}",
            url=f"u/s{i}",
            feed=feed,
            body_source="full_extract",
            created="2026-08-03",
        )

    assert repo.sample_healthy_source_urls() == ["u/ok"]
