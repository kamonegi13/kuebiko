"""Cloudflare Access の JWT 検証 (2026-08-01、readonly instance の Tier1 認証)。

公開 instance (kuebiko.example) は既定で匿名閲覧 (Tier0)。Cloudflare Access で認証した
利用者だけが Tier1 (fullOnly ページの閲覧 + ジョブの即時実行) に到達できる。
Access がドメインに付与する ``CF_Authorization`` cookie / ``Cf-Access-Jwt-Assertion``
ヘッダの JWT を、team の公開鍵 (JWKS) で検証する。

設計上の不変条件:

- **fail-closed**: 署名不正・期限切れ・aud/iss 不一致・鍵取得失敗はすべて「未認証」。
  例外を握り潰して認証済みに倒すことはしない。
- **未設定なら認証機能そのものが無効** (``load_access_config`` が None)。
  この場合 middleware は従来どおり匿名前提で動く (段階導入のため)。
- **ログにメールアドレス・トークンを出さない** (CLAUDE.md §4)。識別が要る場面は
  ``Identity.subject_hash`` (sha256 先頭 12 桁) を使う。
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt
import structlog

_log = structlog.get_logger(__name__)

# Access が JWT を載せる 2 経路 (ブラウザは cookie、fetch/API 直叩きはヘッダ)
ACCESS_JWT_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_JWT_COOKIE = "CF_Authorization"

_JWKS_TTL_SECONDS = 3600.0
_JWKS_FETCH_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class AccessConfig:
    """Cloudflare Access の team ドメインと Application Audience (AUD)。"""

    team_domain: str  # 例: "kuebiko.cloudflareaccess.com" (scheme なし)
    aud: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"

    @property
    def logout_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/logout"


@dataclass(frozen=True)
class Identity:
    """検証済み ID。email は保持するがログ・API 応答には出さない。"""

    subject: str
    email: str

    @property
    def subject_hash(self) -> str:
        return hashlib.sha256(self.subject.encode("utf-8")).hexdigest()[:12]


def _normalize_team_domain(raw: str) -> str:
    """``https://x.cloudflareaccess.com/`` 等の揺らぎを host 部に正規化する。"""
    value = raw.strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.strip("/")


def load_access_config(env: Mapping[str, str] | None = None) -> AccessConfig | None:
    """env から Access 設定を読む。片方でも欠けたら None (= 認証機能を使わない)。"""
    source = env if env is not None else os.environ
    team_domain = _normalize_team_domain(source.get("CF_ACCESS_TEAM_DOMAIN", ""))
    aud = source.get("CF_ACCESS_AUD", "").strip()
    if not team_domain or not aud:
        return None
    return AccessConfig(team_domain=team_domain, aud=aud)


async def _fetch_jwks_via_httpx(url: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=_JWKS_FETCH_TIMEOUT_SECONDS) as client:
        res = await client.get(url)
        res.raise_for_status()
        data: dict[str, Any] = res.json()
        return data


class AccessVerifier:
    """JWKS を TTL キャッシュしつつ Access JWT を検証する。

    kid が未知のときだけ即時再取得する (鍵ローテーション対応)。取得失敗時は
    キャッシュを保持したまま未認証を返す (fail-closed かつ既存鍵は活かす)。
    """

    def __init__(
        self,
        config: AccessConfig,
        fetch_jwks: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._fetch_jwks = fetch_jwks or _fetch_jwks_via_httpx
        self._now = now
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None

    @property
    def config(self) -> AccessConfig:
        return self._config

    async def _load_keys(self) -> dict[str, Any]:
        try:
            payload = await self._fetch_jwks(self._config.certs_url)
            key_set = jwt.PyJWKSet.from_dict(payload)
        except Exception as e:  # noqa: BLE001 — 取得/解析失敗は未認証に倒す
            _log.warning("access_jwks_fetch_failed", error=str(e))
            return self._keys
        keys = {k.key_id: k.key for k in key_set.keys if k.key_id}
        self._keys = keys
        self._fetched_at = self._now()
        return keys

    async def _signing_key(self, kid: str) -> Any | None:
        expired = self._fetched_at is None or (self._now() - self._fetched_at) > _JWKS_TTL_SECONDS
        keys = await self._load_keys() if expired else self._keys
        if kid in keys:
            return keys[kid]
        # 未知の kid = ローテーション直後の可能性。1 度だけ強制再取得する
        if not expired:
            keys = await self._load_keys()
        return keys.get(kid)

    async def verify(self, token: str) -> Identity | None:
        """JWT を検証して Identity を返す。不正なら None (例外は投げない)。"""
        if not token:
            return None
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
        except Exception:  # noqa: BLE001
            return None
        if not kid:
            return None
        key = await self._signing_key(kid)
        if key is None:
            _log.warning("access_jwt_unknown_kid")
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._config.aud,
                issuer=self._config.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception as e:  # noqa: BLE001 — 期限切れ/署名不正等はすべて未認証
            _log.info("access_jwt_rejected", reason=type(e).__name__)
            return None
        subject = str(claims.get("sub") or claims.get("common_name") or "")
        if not subject:
            return None
        return Identity(subject=subject, email=str(claims.get("email") or ""))


def extract_access_token(headers: Mapping[str, str], cookies: Mapping[str, str]) -> str:
    """リクエストから Access JWT を取り出す (ヘッダ優先、無ければ cookie)。"""
    token = headers.get(ACCESS_JWT_HEADER) or headers.get(ACCESS_JWT_HEADER.lower()) or ""
    if token.strip():
        return token.strip()
    return cookies.get(ACCESS_JWT_COOKIE, "").strip()
