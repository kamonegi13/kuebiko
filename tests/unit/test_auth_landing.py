"""Cloudflare Access 認証後の着地点 (src/ui/routers/auth.py)。

認証は edge (Cloudflare Access) が行い、origin はその後の受け皿を用意するだけ。
**着地点を 1 つに決め打ちしない** — Access は認証後にアプリケーションのパス
(``/auth/``) へ戻すことがあり、そこにルートが無いと **認証は成功しているのに
404 が出る**。2026-08-26 のドメイン移行 (kuebiko.example → ops.kuebiko.example) で顕在化した。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ui.routers.auth import build_auth_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_auth_router(None))
    return TestClient(app, follow_redirects=False)


def test_login_path_lands_on_the_app() -> None:
    response = _client().get("/auth/login")

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"


def test_bare_auth_path_also_lands_on_the_app() -> None:
    # Access がアプリケーションのパスへ戻す場合。ここが 404 だと認証が通っても入れない
    response = _client().get("/auth/")

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"


def test_auth_without_trailing_slash_lands_on_the_app() -> None:
    response = _client().get("/auth")

    assert response.status_code == 302
    assert response.headers["location"] == "/app/"


def test_logout_is_outside_the_protected_path() -> None:
    """ログアウトを /auth/ 配下に置くと、cookie 破棄直後に再ログインを求められる。"""
    import inspect

    from src.ui.routers import auth

    source = inspect.getsource(auth.build_auth_router)
    assert '@router.get("/logout")' in source


class TestLogoutDestination:
    """ログアウト後は公開サイトへ戻す (運用ドメインに残さない。2026-08-27 利用者指摘)。

    実ドメインはコードに書かない — .env の PUBLIC_SITE_URL で注入し、
    未設定なら従来どおりアプリへ戻る (段階導入で壊れない)。
    """

    def test_destination_comes_from_the_environment(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.ui.routers import auth

        monkeypatch.setenv("PUBLIC_SITE_URL", "https://news.kuebiko.example/")

        assert auth._logout_destination() == "https://news.kuebiko.example/"
        assert "https://news.kuebiko.example/" in auth._logout_html()

    def test_falls_back_to_the_app_when_unset(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.ui.routers import auth

        monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)

        assert auth._logout_destination() == "/app/"

    def test_no_real_domain_in_code(self) -> None:
        import inspect

        from src.ui.routers import auth

        assert "kuebiko.example" not in inspect.getsource(auth)
