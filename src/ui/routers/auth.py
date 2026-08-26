"""ログイン / ログアウトの導線 (2026-08-01、Cloudflare Access Tier1)。

認証そのものは Cloudflare Access が edge で行う (``/auth/*`` に Access アプリを
被せる)。origin 側の役目は 2 つだけ:

- ``/auth/login`` と ``/auth/``: Access の認証を通過した後の着地点
  (**Access の保護対象**)。cookie は Access がドメイン全体に付与済みなので、SPA に
  戻すだけでよい。**着地点を 1 つに決め打ちしない** — Access は認証後に
  アプリケーションのパス (``/auth/``) へ戻すことがあり、そこにルートが無いと
  認証は成功しているのに 404 になる (2026-08-26 実測)。
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


_LOGIN_DONE_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ログイン完了</title>
<meta http-equiv="refresh" content="2;url=/app/">
<style>body{font-family:system-ui,sans-serif;margin:0;height:100vh;display:flex;
flex-direction:column;align-items:center;justify-content:center;gap:12px;
background:#0b0d11;color:#e5e7eb;text-align:center;padding:0 24px}
p{margin:0;line-height:1.9}.sub{color:#9ca3af;font-size:14px}
a{color:#93c5fd}</style>
</head><body>
<p style="font-size:40px">✓</p>
<p><strong>ログインしました</strong></p>
<p class="sub"><a href="/app/">切り替わらない場合はこちら</a></p>
<script>setTimeout(function(){window.location.replace("/app/")}, 600);</script>
</body></html>"""


def _logout_destination() -> str:
    """ログアウト後の戻り先。

    運用画面 (ops ホスト) に戻すと「ログアウトしたのに運用ドメインに居る」状態に
    なる (2026-08-27 利用者指摘)。公開ニュースサイトがあるならそちらへ戻す。
    実ドメインはコードに書かない (公開リポの汎用化規約) — ``.env`` の
    ``PUBLIC_SITE_URL`` で注入し、未設定なら従来どおりアプリへ戻る。
    """
    import os

    return os.environ.get("PUBLIC_SITE_URL", "").strip() or _APP_HOME


def _logout_html() -> str:
    """中継ページ: cookie 破棄 → 公開サイトへ戻る。JS 無効時と失敗時も必ず戻れる
    ように noscript リンクと meta refresh を併置する (行き止まりを作らない)。"""
    dest = _logout_destination()
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>ログアウト</title>
<meta http-equiv="refresh" content="5;url={dest}">
<style>body{{font-family:system-ui,sans-serif;margin:0;height:100vh;display:flex;
align-items:center;justify-content:center;color:#444}}</style>
</head><body>
<p>ログアウトしています… <a href="{dest}">戻らない場合はこちら</a></p>
<noscript><p><a href="{_EDGE_LOGOUT_PATH}">ログアウト</a></p></noscript>
<script>
fetch({_EDGE_LOGOUT_PATH!r}, {{credentials: "same-origin", cache: "no-store"}})
  .catch(function () {{}})
  .then(function () {{ window.location.replace({dest!r}); }});
</script>
</body></html>"""


def build_auth_router(config: AccessConfig | None) -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get("/auth/login")
    async def login(display: str = "") -> Response:
        # ここに到達した時点で Access の認証は完了している (未認証なら edge で止まる)
        #
        # PWA (standalone) からのログインは完了ページ経由でアプリへ自動遷移する。
        # manifest の scope を / に広げた後は、iOS が Access からの復帰時に
        # オーバーレイを自動で畳み **PWA 本体に着地する** (2026-08-27 実測 —
        # 当初は「✕ で戻る」案内を出したが、✕ 自体が存在しない状態になった)。
        # 完了ページは 0.6 秒でアプリへ進む。SPA の直接 302 にしないのは、
        # 認証直後の Set-Cookie とアプリ起動の競合を 1 拍分離するため。
        if display == "standalone":
            return HTMLResponse(_LOGIN_DONE_HTML, headers={"Cache-Control": "no-store"})
        return RedirectResponse(url=_APP_HOME, status_code=302)

    # ⚠ **着地点を 1 つに決め打ちしない**。Cloudflare Access は認証後に
    # アプリケーションのパス (`/auth/`) へ戻すことがあり、そこにルートが無いと
    # **認証は成功しているのに 404 が出る** (2026-08-26 実測。ドメイン移行で顕在化した)。
    # 保護対象の配下はどこに着いても SPA へ流す。
    @router.get("/auth")
    @router.get("/auth/")
    async def login_landing() -> RedirectResponse:
        return RedirectResponse(url=_APP_HOME, status_code=302)

    # 戻り型は Response に揃える (Union だと FastAPI が response model を組めない)
    @router.get("/logout")
    async def logout() -> Response:
        if config is None:
            return RedirectResponse(url=_APP_HOME, status_code=302)
        return HTMLResponse(_logout_html(), headers={"Cache-Control": "no-store"})

    # 旧 URL (ブックマーク / 既存 bundle からの遷移) を新しいログアウトへ寄せる。
    # ここは Access 保護下なので、認証済みの人だけが通り抜けて /logout に着く。
    @router.get("/auth/logout")
    async def legacy_logout() -> RedirectResponse:
        return RedirectResponse(url="/logout", status_code=302)

    return router
