"""ログイン / ログアウトの導線 (2026-08-01、Cloudflare Access Tier1)。

認証そのものは Cloudflare Access が edge で行う (``/auth/*`` に Access アプリを
被せる)。origin 側の役目は 2 つだけ:

- ``/auth/login``: Access の認証を通過した後の着地点 (**Access の保護対象**)。cookie は
  Access がドメイン全体に付与済みなので、SPA に戻すだけでよい。
- ``/logout``: 同一オリジンの ``/cdn-cgi/access/logout`` (Cloudflare edge が処理し、
  origin には届かない) を叩いて cookie を破棄し、**アプリに戻す**。

ログアウトを ``/auth/`` 配下に置かないのは、そこが Access の保護対象だから
(2026-08-01 実運用で判明): cookie を捨てた直後の未認証リクエストが再びログイン画面へ
送られ、ログアウトのつもりがログインを求められる。**ログアウト経路は必ず保護対象外**に置く。

``/cdn-cgi/access/logout`` へ素の redirect をすると Cloudflare のログアウト画面で
行き止まりになる (戻り先を指定する parameter が無い) ため、小さな中継ページから
fetch して破棄だけ済ませ、こちらで ``/app/`` へ戻す。

Access 未設定なら両方 SPA に戻すだけの no-op (段階導入で壊れない)。
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from src.ui.services.cf_access import AccessConfig

_APP_HOME = "/app/"
# Cloudflare edge が処理する logout endpoint (同一オリジン。origin には到達しない)
_EDGE_LOGOUT_PATH = "/cdn-cgi/access/logout"

# 中継ページ: cookie 破棄 → アプリへ戻る。JS 無効時と失敗時も必ず戻れるように
# noscript リンクと meta refresh を併置する (行き止まりを作らない)。
_LOGOUT_HTML = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>ログアウト</title>
<meta http-equiv="refresh" content="5;url={_APP_HOME}">
<style>body{{font-family:system-ui,sans-serif;margin:0;height:100vh;display:flex;
align-items:center;justify-content:center;color:#444}}</style>
</head><body>
<p>ログアウトしています… <a href="{_APP_HOME}">戻らない場合はこちら</a></p>
<noscript><p><a href="{_EDGE_LOGOUT_PATH}">ログアウト</a></p></noscript>
<script>
fetch({_EDGE_LOGOUT_PATH!r}, {{credentials: "same-origin", cache: "no-store"}})
  .catch(function () {{}})
  .then(function () {{ window.location.replace({_APP_HOME!r}); }});
</script>
</body></html>"""


def build_auth_router(config: AccessConfig | None) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get("/auth/login")
    async def login() -> RedirectResponse:
        # ここに到達した時点で Access の認証は完了している (未認証なら edge で止まる)
        return RedirectResponse(url=_APP_HOME, status_code=302)

    # 戻り型は Response に揃える (Union だと FastAPI が response model を組めない)
    @router.get("/logout")
    async def logout() -> Response:
        if config is None:
            return RedirectResponse(url=_APP_HOME, status_code=302)
        return HTMLResponse(_LOGOUT_HTML, headers={"Cache-Control": "no-store"})

    # 旧 URL (ブックマーク / 既存 bundle からの遷移) を新しいログアウトへ寄せる。
    # ここは Access 保護下なので、認証済みの人だけが通り抜けて /logout に着く。
    @router.get("/auth/logout")
    async def legacy_logout() -> RedirectResponse:
        return RedirectResponse(url="/logout", status_code=302)

    return router
