"""Cloudflare Access JWT 検証のテスト (2026-08-01)。

fail-closed (署名不正 / 期限切れ / aud・iss 不一致 / 鍵取得失敗は未認証) が核心。
テスト用 RSA 鍵を生成し、JWKS 取得を注入して実 JWT を検証する。
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.ui.services.cf_access import (
    AccessConfig,
    AccessVerifier,
    Identity,
    extract_access_token,
    load_access_config,
)

TEAM_DOMAIN = "kuebiko.cloudflareaccess.com"
AUD = "a" * 64
KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}


@pytest.fixture
def config() -> AccessConfig:
    return AccessConfig(team_domain=TEAM_DOMAIN, aud=AUD)


def make_token(
    key: rsa.RSAPrivateKey,
    *,
    aud: str = AUD,
    issuer: str = f"https://{TEAM_DOMAIN}",
    expires_in: int = 3600,
    kid: str = KID,
    email: str = "user@example.com",
    subject: str = "sub-123",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": [aud],
            "iss": issuer,
            "sub": subject,
            "email": email,
            "iat": now,
            "exp": now + expires_in,
        },
        key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def build_verifier(
    config: AccessConfig, jwks: dict[str, Any], *, fail: bool = False
) -> AccessVerifier:
    calls: list[str] = []

    async def fetch(url: str) -> dict[str, Any]:
        calls.append(url)
        if fail:
            raise RuntimeError("network down")
        return jwks

    verifier = AccessVerifier(config, fetch_jwks=fetch)
    # テストから取得回数を観測できるようにする
    verifier.fetch_calls = calls  # type: ignore[attr-defined]
    return verifier


class TestLoadAccessConfig:
    def test_returns_none_when_unset(self) -> None:
        assert load_access_config({}) is None

    def test_returns_none_when_partially_set(self) -> None:
        assert load_access_config({"CF_ACCESS_TEAM_DOMAIN": TEAM_DOMAIN}) is None
        assert load_access_config({"CF_ACCESS_AUD": AUD}) is None

    def test_normalizes_scheme_and_slash(self) -> None:
        cfg = load_access_config(
            {"CF_ACCESS_TEAM_DOMAIN": f"https://{TEAM_DOMAIN}/", "CF_ACCESS_AUD": f"  {AUD} "}
        )
        assert cfg is not None
        assert cfg.team_domain == TEAM_DOMAIN
        assert cfg.aud == AUD
        assert cfg.issuer == f"https://{TEAM_DOMAIN}"
        assert cfg.certs_url == f"https://{TEAM_DOMAIN}/cdn-cgi/access/certs"
        assert cfg.logout_url == f"https://{TEAM_DOMAIN}/cdn-cgi/access/logout"


class TestExtractAccessToken:
    def test_prefers_header(self) -> None:
        token = extract_access_token({"Cf-Access-Jwt-Assertion": "hdr"}, {"CF_Authorization": "ck"})
        assert token == "hdr"

    def test_falls_back_to_cookie(self) -> None:
        assert extract_access_token({}, {"CF_Authorization": "ck"}) == "ck"

    def test_returns_empty_when_absent(self) -> None:
        assert extract_access_token({}, {}) == ""


@pytest.mark.asyncio
class TestVerify:
    async def test_valid_token_returns_identity(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        identity = await verifier.verify(make_token(rsa_key))
        assert identity == Identity(subject="sub-123", email="user@example.com")
        assert len(identity.subject_hash) == 12

    async def test_expired_token_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        assert await verifier.verify(make_token(rsa_key, expires_in=-60)) is None

    async def test_wrong_audience_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        assert await verifier.verify(make_token(rsa_key, aud="b" * 64)) is None

    async def test_wrong_issuer_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        token = make_token(rsa_key, issuer="https://evil.cloudflareaccess.com")
        assert await verifier.verify(token) is None

    async def test_token_signed_by_other_key_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any]
    ) -> None:
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = build_verifier(config, jwks)
        assert await verifier.verify(make_token(attacker)) is None

    async def test_unsigned_alg_none_token_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any]
    ) -> None:
        now = int(time.time())
        token = jwt.encode(
            {
                "aud": [AUD],
                "iss": f"https://{TEAM_DOMAIN}",
                "sub": "s",
                "iat": now,
                "exp": now + 60,
            },
            key="",
            algorithm="none",
            headers={"kid": KID},
        )
        verifier = build_verifier(config, jwks)
        assert await verifier.verify(token) is None

    async def test_unknown_kid_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        assert await verifier.verify(make_token(rsa_key, kid="other-kid")) is None

    async def test_missing_or_garbage_token_is_rejected(
        self, config: AccessConfig, jwks: dict[str, Any]
    ) -> None:
        verifier = build_verifier(config, jwks)
        assert await verifier.verify("") is None
        assert await verifier.verify("not-a-jwt") is None

    async def test_jwks_fetch_failure_is_fail_closed(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks, fail=True)
        assert await verifier.verify(make_token(rsa_key)) is None

    async def test_jwks_is_cached_between_verifications(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        await verifier.verify(make_token(rsa_key))
        await verifier.verify(make_token(rsa_key))
        assert len(verifier.fetch_calls) == 1  # type: ignore[attr-defined]

    async def test_jwks_refetched_once_for_unknown_kid(
        self, config: AccessConfig, jwks: dict[str, Any], rsa_key: rsa.RSAPrivateKey
    ) -> None:
        verifier = build_verifier(config, jwks)
        await verifier.verify(make_token(rsa_key))  # 1 回目で cache 済み
        await verifier.verify(make_token(rsa_key, kid="rotated"))
        assert len(verifier.fetch_calls) == 2  # type: ignore[attr-defined]
