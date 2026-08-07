"""READ_ONLY instance の遮断境界テスト (2026-07-31 / 08-01)。

3 層の境界を固定する:

- Tier0 匿名: 公開 read API と SPA は通る。
- Tier1 認証済み (Cloudflare Access): denylist の運用系 read API と
  ジョブ即時実行 (full instance への proxy) が通る。
- Tier2: それ以外の write は認証の有無に関わらず 403。

サイドバー flash 根治の seed (``window.__READ_ONLY__`` 等) も併せて検証する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.ui.read_only_policy import (
    is_job_trigger_path,
    is_read_only_allowed_post,
    is_read_only_blocked_get,
)
from src.ui.services.cf_access import AccessConfig, Identity
from tests.unit.test_ui_app import _bootstrap_project

# 検証をすり抜けさせないための stub verifier (実際の JWT 検証は test_cf_access.py)
_VALID_TOKEN = "valid-token"


class _StubVerifier:
    """token == _VALID_TOKEN のときだけ Identity を返す AccessVerifier 相当。"""

    def __init__(self) -> None:
        self.config = AccessConfig(team_domain="team.cloudflareaccess.com", aud="aud")

    async def verify(self, token: str) -> Identity | None:
        if token == _VALID_TOKEN:
            return Identity(subject="sub-1", email="user@example.com")
        return None


# fullOnly ページの read API (公開 instance から遮断すべきもの)
BLOCKED_GET_PATHS = [
    "/api/v1/jobs",
    "/api/v1/jobs/direct-rss-fetch",
    "/api/v1/schedule",
    "/api/v1/prompts",
    "/api/v1/prompts/file",
    "/api/v1/config",
    "/api/v1/config/yaml",
    "/api/v1/config/source-quality",
    "/api/v1/config-history",
    "/api/v1/config-history/routing",
    "/api/v1/match-lists",
    "/api/v1/model-tiers",
    "/api/v1/mobile-tunnel/status",
    "/api/v1/host-watchdog/status",
    "/api/v1/taxonomy-review",
    "/api/v1/editorial-quality",
    "/api/v1/flow",
    "/api/v1/routing-rules",
    "/api/v1/product-routing",
]

# 公開ページが消費する read API (readonly でも通すべきもの)
PUBLIC_GET_PATHS = [
    "/api/v1/runtime-flags",
    "/api/v1/health-status",
    "/api/v1/history",
    "/api/v1/subscriptions",
    # ChannelChip 等の公開 UI が消費 (webhook URL はマスク済みのため遮断しない)
    "/api/v1/channels",
    "/api/v1/actors",
]


class TestBlockedGetJudgement:
    """_is_read_only_blocked_get の純関数テスト。"""

    @pytest.mark.parametrize("path", BLOCKED_GET_PATHS)
    def test_denylist_paths_are_blocked(self, path: str) -> None:
        assert is_read_only_blocked_get(path) is True

    @pytest.mark.parametrize("path", PUBLIC_GET_PATHS)
    def test_public_paths_are_not_blocked(self, path: str) -> None:
        assert is_read_only_blocked_get(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            # 境界一致: prefix の単なる前方一致 (別 endpoint) を巻き込まない
            "/api/v1/flows",
            "/api/v1/jobsx",
            "/api/v1/configuration",
            "/api/v1/scheduler-info",
        ],
    )
    def test_prefix_boundary_does_not_overmatch(self, path: str) -> None:
        assert is_read_only_blocked_get(path) is False


class TestAllowedPostJudgement:
    """_is_read_only_allowed_post の純関数テスト (既存挙動の固定)。"""

    def test_assistant_chat_is_allowed(self) -> None:
        assert is_read_only_allowed_post("/api/v1/assistant/chat") is True

    def test_article_translate_is_allowed(self) -> None:
        assert is_read_only_allowed_post("/api/v1/articles/abc/123/translate") is True

    def test_other_posts_are_not_allowed(self) -> None:
        assert is_read_only_allowed_post("/api/v1/channels") is False
        assert is_read_only_allowed_post("/api/v1/taxonomy-review/x/approve") is False


def _write_spa_stub(project: Path) -> None:
    """react_spa_fallback を有効化する frontend/dist の最小 stub。"""
    dist = project / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>kuebiko</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )


@pytest.fixture
def read_only_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    project = _bootstrap_project(tmp_path)
    _write_spa_stub(project)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    # READ_ONLY_FLAG は import 時に env から確定するため、直接 monkeypatch する。
    # env も併せて立てる (runtime-flags endpoint は env を直接読む)
    # ⚠ READ_ONLY_FLAG は src.ui.app の **import 時** に env から確定する定数。
    # READ_ONLY=1 を立てた状態で初回 import するとセッション全体に漏れ、後続の
    # 通常テストが全部 403 になる (実測)。**env より先に import** して確定させる。
    from src.ui import app as app_module

    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setattr(app_module, "READ_ONLY_FLAG", True)
    fresh_app = app_module.create_app()
    with TestClient(fresh_app) as c:
        yield c


class TestReadOnlyMiddleware:
    """middleware 統合: 実 app に対する 403 / 素通しの境界。"""

    @pytest.mark.parametrize("path", ["/api/v1/jobs", "/api/v1/config", "/api/v1/prompts/file"])
    def test_blocked_get_returns_403(self, read_only_client: TestClient, path: str) -> None:
        res = read_only_client.get(path)
        assert res.status_code == 403
        assert res.json()["detail"] == "read-only instance: this endpoint is not exposed"

    def test_blocked_head_returns_403(self, read_only_client: TestClient) -> None:
        assert read_only_client.head("/api/v1/jobs").status_code == 403

    def test_public_get_passes_through(self, read_only_client: TestClient) -> None:
        res = read_only_client.get("/api/v1/runtime-flags")
        assert res.status_code == 200
        assert res.json()["read_only"] is True

    def test_public_channels_get_passes_through(self, read_only_client: TestClient) -> None:
        # 公開 UI (ChannelChip) の依存。403 にしないことが要件 (中身は masked)
        assert read_only_client.get("/api/v1/channels").status_code != 403

    def test_write_post_still_blocked(self, read_only_client: TestClient) -> None:
        res = read_only_client.post("/api/v1/channels", json={"channels": []})
        assert res.status_code == 403
        assert "write operations are blocked" in res.json()["detail"]


class TestTriggerPathJudgement:
    """Tier1 の唯一の write (ジョブ即時実行) の path 判定。"""

    def test_run_path_matches(self) -> None:
        assert is_job_trigger_path("/api/v1/jobs/direct-rss-fetch/run") is True
        assert is_job_trigger_path("/api/v1/jobs/morning_brief.v2/run") is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/jobs/x/schedule",  # 別 write endpoint に化けない
            "/api/v1/jobs//run",
            "/api/v1/jobs/../config/run",  # traversal
            "/api/v1/jobs/x/run/extra",
            "/api/v1/jobs/x%2F..%2Fy/run",
            "/api/v1/jobs",
        ],
    )
    def test_non_run_paths_do_not_match(self, path: str) -> None:
        assert is_job_trigger_path(path) is False


@pytest.fixture
def access_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Cloudflare Access 設定済みの readonly instance (検証は stub)。"""
    project = _bootstrap_project(tmp_path)
    _write_spa_stub(project)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    # ⚠ READ_ONLY_FLAG は src.ui.app の **import 時** に env から確定する定数。
    # READ_ONLY=1 を立てた状態で初回 import するとセッション全体に漏れ、後続の
    # 通常テストが全部 403 になる (実測)。**env より先に import** して確定させる。
    from src.ui import app as app_module

    monkeypatch.setenv("READ_ONLY", "1")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "a" * 64)

    monkeypatch.setattr(app_module, "READ_ONLY_FLAG", True)
    monkeypatch.setattr(app_module, "AccessVerifier", lambda _config: _StubVerifier())
    with TestClient(app_module.create_app()) as c:
        yield c


def _auth(token: str = _VALID_TOKEN) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": token}


class TestTier1Access:
    """認証済みだけが到達できる層 (denylist 閲覧 + 即時実行)。"""

    def test_blocked_get_is_allowed_when_authenticated(self, access_client: TestClient) -> None:
        assert access_client.get("/api/v1/jobs", headers=_auth()).status_code == 200

    def test_blocked_get_stays_403_when_anonymous(self, access_client: TestClient) -> None:
        assert access_client.get("/api/v1/jobs").status_code == 403

    def test_blocked_get_stays_403_with_invalid_token(self, access_client: TestClient) -> None:
        res = access_client.get("/api/v1/jobs", headers=_auth("forged"))
        assert res.status_code == 403

    def test_cookie_token_is_accepted(self, access_client: TestClient) -> None:
        access_client.cookies.set("CF_Authorization", _VALID_TOKEN)
        try:
            assert access_client.get("/api/v1/jobs").status_code == 200
        finally:
            access_client.cookies.clear()

    def test_runtime_flags_report_auth_state(self, access_client: TestClient) -> None:
        anon = access_client.get("/api/v1/runtime-flags").json()
        assert anon == {"read_only": True, "authenticated": False, "auth_available": True}
        authed = access_client.get("/api/v1/runtime-flags", headers=_auth()).json()
        assert authed["authenticated"] is True

    def test_llm_post_requires_auth_once_access_configured(self, access_client: TestClient) -> None:
        anon = access_client.post("/api/v1/assistant/chat", json={})
        assert anon.status_code == 403
        assert "認証が必要です" in anon.json()["detail"]
        # 認証済みは middleware を通過する (422 = endpoint のスキーマ検証まで到達)
        authed = access_client.post("/api/v1/assistant/chat", json={}, headers=_auth())
        assert authed.status_code != 403

    def test_llm_post_stays_anonymous_when_access_not_configured(
        self, read_only_client: TestClient
    ) -> None:
        # 段階導入: Access 未設定なら従来どおり匿名で使える
        assert read_only_client.post("/api/v1/assistant/chat", json={}).status_code != 403

    def test_tier2_write_stays_blocked_even_when_authenticated(
        self, access_client: TestClient
    ) -> None:
        res = access_client.post("/api/v1/channels", json={"channels": []}, headers=_auth())
        assert res.status_code == 403
        assert "write operations are blocked" in res.json()["detail"]


class TestJobRunProxy:
    """即時実行は full instance へ narrow proxy される (readonly は scheduler 不在)。"""

    @pytest.fixture
    def proxy_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        import httpx

        calls: list[str] = []

        class _FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"ok": True, "job_id": "direct-rss-fetch"}

        class _FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def post(self, url: str) -> _FakeResponse:
                calls.append(url)
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        return calls

    def test_authenticated_run_is_proxied_to_full_instance(
        self, access_client: TestClient, proxy_calls: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FULL_INSTANCE_URL", "http://kuebiko:8000")
        res = access_client.post("/api/v1/jobs/direct-rss-fetch/run", headers=_auth())
        assert res.status_code == 200
        assert res.json()["ok"] is True
        assert proxy_calls == ["http://kuebiko:8000/api/v1/jobs/direct-rss-fetch/run"]

    def test_anonymous_run_is_rejected_without_proxying(
        self, access_client: TestClient, proxy_calls: list[str]
    ) -> None:
        assert access_client.post("/api/v1/jobs/direct-rss-fetch/run").status_code == 403
        assert proxy_calls == []

    def test_proxy_failure_returns_502(
        self, access_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        class _BrokenClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> _BrokenClient:
                return self

            async def __aexit__(self, *args: Any) -> bool:
                return False

            async def post(self, url: str) -> Any:
                raise RuntimeError("connection refused")

        monkeypatch.setattr(httpx, "AsyncClient", _BrokenClient)
        res = access_client.post("/api/v1/jobs/direct-rss-fetch/run", headers=_auth())
        assert res.status_code == 502


class TestAuthRoutes:
    """ログイン導線 (認証自体は Cloudflare Access が edge で行う)。"""

    def test_login_redirects_to_app(self, access_client: TestClient) -> None:
        res = access_client.get("/auth/login", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/app/"

    def test_logout_clears_cookie_then_returns_to_app(self, access_client: TestClient) -> None:
        # 素の redirect だと Cloudflare のログアウト画面で行き止まりになるため、
        # 中継ページで cookie を破棄してからアプリへ戻す (2026-08-01)
        res = access_client.get("/logout", follow_redirects=False)
        assert res.status_code == 200
        assert "/cdn-cgi/access/logout" in res.text
        assert "/app/" in res.text  # JS・meta refresh・リンクのいずれでも戻れる
        assert "noscript" in res.text

    def test_logout_is_outside_access_protected_prefix(self, access_client: TestClient) -> None:
        # ログアウトが /auth/ 配下にあると、cookie 破棄後の未認証リクエストが
        # Access のログイン画面へ送られてしまう (実運用で踏んだ罠の回帰テスト)
        assert not access_client.app.url_path_for("logout").startswith("/auth")  # type: ignore[attr-defined]

    def test_legacy_auth_logout_redirects_to_public_logout(self, access_client: TestClient) -> None:
        res = access_client.get("/auth/logout", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/logout"

    def test_logout_falls_back_to_app_when_unconfigured(self, read_only_client: TestClient) -> None:
        res = read_only_client.get("/logout", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/app/"


class TestSpaReadOnlySeed:
    """react_spa_fallback の runtime flag 埋め込み (flash 根治)。"""

    def test_read_only_true_is_embedded(self, read_only_client: TestClient) -> None:
        res = read_only_client.get("/app/daily-brief")
        assert res.status_code == 200
        assert "window.__READ_ONLY__=true" in res.text
        assert "window.__AUTH_AVAILABLE__=false" in res.text
        assert res.headers["cache-control"] == "no-cache"

    def test_auth_state_is_embedded(self, access_client: TestClient) -> None:
        anon = access_client.get("/app/")
        assert "window.__AUTHENTICATED__=false" in anon.text
        assert "window.__AUTH_AVAILABLE__=true" in anon.text
        authed = access_client.get("/app/", headers=_auth())
        assert "window.__AUTHENTICATED__=true" in authed.text

    def test_full_instance_embeds_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _bootstrap_project(tmp_path)
        _write_spa_stub(project)
        monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
        monkeypatch.chdir(project)

        from src.ui import app as app_module

        monkeypatch.setattr(app_module, "READ_ONLY_FLAG", False)
        with TestClient(app_module.create_app()) as c:
            res = c.get("/app")
            assert res.status_code == 200
            assert "window.__READ_ONLY__=false" in res.text
