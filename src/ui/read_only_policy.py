"""readonly instance (公開 kuebiko.example) の遮断ポリシー (SSoT)。

3 層の到達範囲を 1 箇所で決める (2026-08-01 Tier1 認証を追加):

- **Tier0 匿名**: 閲覧系 read API と SPA。既定の公開面。
- **Tier1 認証済み** (Cloudflare Access): ``READ_ONLY_GET_DENYLIST`` の運用系 read API
  (ジョブ計画 / 設定 / プロンプト / ルーティング / レビューキュー) の閲覧と、
  ジョブの即時実行 (full instance へ narrow proxy)。分析チャット・記事翻訳の
  LLM 消費もこの層に上げる (Access 設定時のみ。未設定なら従来どおり匿名可)。
- **Tier2 ローカル専用**: それ以外の write (設定保存 / キー / プロンプト保存 /
  レビュー承認 等) は認証の有無に関わらず 403。full instance (127.0.0.1) でのみ可能。

readonly コンテナは scheduler を起動しないため、即時実行だけは full instance へ
HTTP proxy する (write の実行主体は full のまま = CLAUDE.md §12 の境界を保つ)。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from src.ui.services.cf_access import AccessVerifier, Identity, extract_access_token

_log = structlog.get_logger(__name__)

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# read-only instance で例外的に許可する POST (2026-07-19 ユーザー判断)。
# 分析チャットは決定論 read-only ツール + 語彙固定 (injection 主防御) で状態を変更しない。
# Access 設定後は Tier1 (認証済みのみ) に格上げされる — 匿名の LLM 資源消費を防ぐため。
_POST_ALLOWLIST: tuple[str, ...] = ("/api/v1/assistant/chat",)

# 匿名 (Tier0) が読める API の **allowlist**。ここに無い /api/ の GET はすべて 403。
#
# 2026-08-25 に denylist (既定は許可) から反転した。denylist 方式では **書き忘れが
# そのまま露出**になり、実際に `GET /api/v1/notes` (分析者の所見) が公開面から匿名で
# 読めていた。反転前の実測では 98 個の GET API のうち **67 個が匿名で読め**、PIR
# (収集関心の定義)・取込履歴・購読ソース構成・Grok アカウント状態・LLM を消費する
# 精密検索まで含まれていた。公開面は default-deny でなければ守れない。
#
# 追加するときは「未認証の第三者に見せてよいか」だけで判断すること。運用者は
# Cloudflare Access で認証すれば従来どおり全 GET を読める (Tier1)。
PUBLIC_GET_ALLOWLIST: tuple[str, ...] = (
    # 死活 (監視が叩く)
    "/api/health",
    # SPA が起動時に読む最小限: 認証状態と表示ラベル
    "/api/v1/runtime-flags",
    "/api/v1/vocabularies",
    # 公開ニュース (kuebiko が生成した事象ニュース。出典必須・原記事本文は返さない)
    "/api/v1/public/news",
)

# Tier1 の唯一の write: ジョブ即時実行。job_id の文字種を絞り、proxy 先で別 endpoint に
# 化けないよう完全一致で判定する (path traversal / endpoint すり替えの排除)。
_TRIGGER_PATH_RE = re.compile(r"^/api/v1/jobs/[A-Za-z0-9._-]{1,64}/run$")

#: 遠隔 write を開けても **決して開かない** path。資格情報そのものを扱うため、
#: Access のセッションが 1 つ破られたときの被害が「情報を読まれる」から
#: 「鍵を差し替えられる」に変わる。ここだけはローカル (Tier2) に残す。
#: §4 により鍵は .env にあり、DB には無い — つまりこれらは .env を書き換える口。
CREDENTIAL_WRITE_PATHS: tuple[str, ...] = (
    "/api/v1/model-tiers/anthropic-key",
    "/api/v1/model-tiers/claudecode-token",
    "/api/v1/model-tiers/endpoint-key",
)


def is_credential_write(path: str) -> bool:
    """資格情報を書き換える path か。遠隔 write を開けた場合でも遮断する。"""
    return path in CREDENTIAL_WRITE_PATHS


_PROXY_TIMEOUT_SECONDS = 30.0
_DEFAULT_FULL_INSTANCE_URL = "http://kuebiko:8000"


def is_read_only_allowed_post(path: str) -> bool:
    """readonly instance でも通す POST か判定する。

    記事本文のオンデマンド翻訳 (2026-07-25 ユーザー判断) はモバイル閲覧が主用途
    のため許可する。write は body_ja 1 列のキャッシュ upsert のみ。
    """
    if path in _POST_ALLOWLIST:
        return True
    return path.startswith("/api/v1/articles/") and path.endswith("/translate")


def is_public_get(path: str) -> bool:
    """匿名 (Tier0) が読める GET か判定する (境界一致: prefix そのもの or prefix/)。

    ``/api/`` 以外 (SPA の shell・assets・manifest・/auth/*) は公開のまま — ここで
    弾くと公開サイトそのものが表示できなくなる。判定対象は API だけに閉じる。
    """
    if not path.startswith("/api/"):
        return True
    return any(path == p or path.startswith(f"{p}/") for p in PUBLIC_GET_ALLOWLIST)


def is_read_only_blocked_get(path: str) -> bool:
    """匿名では遮断する GET か判定する (allowlist の否定)。"""
    return not is_public_get(path)


def is_job_trigger_path(path: str) -> bool:
    """Tier1 で許可するジョブ即時実行の path か判定する。"""
    return bool(_TRIGGER_PATH_RE.match(path))


def request_auth_state(request: Request) -> tuple[bool, bool]:
    """(authenticated, auth_available) を返す。

    middleware 未登録の full instance では常に (False, False) — full は 127.0.0.1
    バインドのローカル専用で認証の概念を持たない。
    """
    authenticated = bool(getattr(request.state, "authenticated", False))
    auth_available = bool(getattr(request.state, "auth_available", False))
    return authenticated, auth_available


def _forbidden(detail: str, request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": detail, "method": request.method, "path": request.url.path},
    )


# ---------- 認証の監査証跡 (2026-08-02) ----------
#
# 成功・失敗の**両方**を残す。従来は失敗 (access_jwt_rejected) しかログに出さず、
# 「認証層が使われている」と「一度も使われていない」を区別できなかった
# (実測 2026-08-02: 実際にはログイン運用されていたのに、失敗ログ 0 件を根拠に
# 「未使用」と誤判定した)。**成功を記録しない監視は、沈黙の意味を決められない**。
#
# 保存先は DB (access_audit) — stdout はデプロイ (コンテナ再作成) とログローテーションで
# 消えるため監査証跡にならない (同日、再作成で 40 時間分の証跡を実際に失った)。
# §4 により **email は保存しない**。識別は subject の SHA-256 先頭 12 桁のみ。

# 認証済み read の記録間隔。1 画面が多数の API を呼ぶため毎回記録すると証跡が
# 埋もれる。同一 subject はこの間隔で 1 行に畳む (write と失敗は常に記録)。
_AUTH_AUDIT_INTERVAL_SECONDS = 600.0


#: 認証済みの write を遠隔に開くか (既定 off)。将来「遠隔でもローカル同等に
#: 触りたい」場面が来たときに **env 1 つで開閉できる**ようにしておく。
#: ⚠ 開けると防御が「公開プロセスが write を持たない」という構造から
#: 「Access の設定が正しいこと」へ移る。開けるときは監査 (下記) が前提。
#: 資格情報 (CREDENTIAL_WRITE_PATHS) はこの flag に関わらず常に遮断する。
def _remote_write_enabled() -> bool:
    return os.environ.get("READ_ONLY_ALLOW_REMOTE_WRITE", "0") == "1"


_last_auth_audit: dict[str, float] = {}


def _client_meta(request: Request) -> tuple[str, str]:
    """(client_ip, country)。Cloudflare 経由なので CF ヘッダを優先する。"""
    ip = request.headers.get("Cf-Connecting-Ip") or (request.client.host if request.client else "")
    return ip, request.headers.get("Cf-Ipcountry", "")


def _record_audit(
    request: Request,
    *,
    event: str,
    subject_hash: str = "",
    detail: str = "",
) -> None:
    """監査証跡を stdout と DB の両方へ残す (DB 失敗でも閲覧は止めない)。"""
    client_ip, country = _client_meta(request)
    _log.info(
        f"access_{event}",
        subject=subject_hash,
        method=request.method,
        path=request.url.path,
        client_ip=client_ip,
        country=country,
        detail=detail,
    )
    try:
        from src.storage.run_history import RunHistoryRepository

        RunHistoryRepository().record_access_audit(
            event=event,
            subject_hash=subject_hash,
            method=request.method,
            path=request.url.path,
            client_ip=client_ip,
            country=country,
            detail=detail,
        )
    except Exception as e:  # noqa: BLE001 — 監査の書込失敗で閲覧を止めない
        # kwarg 名を audit_event にする: structlog の第 1 引数が `event` のため
        # `event=` を渡すと TypeError になり、握ったはずの失敗が例外として表に出る
        _log.warning("access_audit_persist_failed", audit_event=event, error=str(e))


def _audit_authentication(request: Request, identity: Identity, path: str) -> None:
    """認証成功を記録する (同一 subject は一定間隔に畳む)。"""
    key = identity.subject_hash
    now = time.monotonic()
    last = _last_auth_audit.get(key)
    if last is not None and now - last < _AUTH_AUDIT_INTERVAL_SECONDS:
        return
    _last_auth_audit[key] = now
    _record_audit(request, event="authenticated", subject_hash=key, detail=path[:120])


async def _proxy_job_run(path: str, identity: Identity) -> JSONResponse:
    """認証済みの即時実行を full instance へ転送する (readonly は scheduler 不在)。"""
    import httpx

    base = os.environ.get("FULL_INSTANCE_URL", _DEFAULT_FULL_INSTANCE_URL).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT_SECONDS) as client:
            res = await client.post(f"{base}{path}")
        payload: Any = res.json()
    except Exception as e:  # noqa: BLE001 — 到達不可/非 JSON は 502 に畳む
        _log.warning("job_run_proxy_failed", path=path, error=str(e))
        return JSONResponse(
            status_code=502,
            content={"detail": "実行の転送に失敗しました (full instance へ到達できません)"},
        )
    _log.info(
        "job_run_proxied",
        path=path,
        status=res.status_code,
        identity=identity.subject_hash,
    )
    return JSONResponse(status_code=res.status_code, content=payload)


def build_read_only_middleware(
    verifier: AccessVerifier | None,
) -> Callable[[Request, Callable[[Request], Awaitable[Any]]], Awaitable[Any]]:
    """READ_ONLY=1 用の middleware を組み立てる (verifier=None なら認証層なし)。"""

    async def read_only_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        identity: Identity | None = None
        if verifier is not None:
            token = extract_access_token(request.headers, request.cookies)
            if token:
                identity = await verifier.verify(token)
                if identity is None:
                    # 資格情報が提示されたのに検証に失敗した = 監査対象の事象
                    # (トークン無しの匿名アクセスは「拒否」ではないので記録しない —
                    #  公開 URL には bot が来るため証跡が埋もれる)
                    _record_audit(request, event="rejected", detail="invalid_or_expired_token")
        request.state.authenticated = identity is not None
        request.state.auth_available = verifier is not None

        path = request.url.path
        method = request.method

        # Tier1 の唯一の write: 即時実行 (認証必須、full instance へ proxy)
        if method == "POST" and is_job_trigger_path(path):
            if identity is None:
                return _forbidden("認証が必要です (ログインしてください)", request)
            # write は畳まず必ず記録する (何がいつ実行されたかは監査の核心)
            _record_audit(
                request, event="tier1_write", subject_hash=identity.subject_hash, detail=path
            )
            return await _proxy_job_run(path, identity)

        if method == "POST" and is_read_only_allowed_post(path):
            # Access 未設定なら従来どおり匿名可 (段階導入で既存挙動を壊さない)
            if verifier is None or identity is not None:
                return await call_next(request)
            return _forbidden("認証が必要です (ログインしてください)", request)

        if method in WRITE_METHODS:
            # 遠隔 write が開いている場合だけ、認証済みに限って通す。
            # ⚠ **記録してから通す**。何を変更したか追えない遠隔 write は、
            #    開いていないのと同じくらい危険 (事後に何が起きたか再構成できない)。
            if _remote_write_enabled() and identity is not None:
                if is_credential_write(path):
                    # flag に関わらず遮断。鍵の差し替えはローカルでしか行わせない
                    _record_audit(
                        request,
                        event="rejected",
                        subject_hash=identity.subject_hash,
                        detail=f"credential_write_blocked:{path}",
                    )
                    return _forbidden("資格情報の変更はローカルでのみ可能です", request)
                _record_audit(
                    request,
                    event="tier1_write",
                    subject_hash=identity.subject_hash,
                    detail=f"{method} {path}",
                )
                return await call_next(request)
            return _forbidden("read-only instance: write operations are blocked", request)

        if method in ("GET", "HEAD") and is_read_only_blocked_get(path):
            if identity is not None:
                # Tier1 の到達 (運用ページの閲覧) を記録する
                _audit_authentication(request, identity, path)
                return await call_next(request)
            return _forbidden("read-only instance: this endpoint is not exposed", request)

        return await call_next(request)

    return read_only_middleware
