"""公開面 到達性監視の回帰テスト。

**なぜこの監視が要るか**: 2026-08-24、cloudflared が QUIC でエッジへ繋いでいたため
**HTTP/2・HTTP/3 のクライアントだけ**が応答途中で切断され、スマホ / PWA から開けない
状態が半日続いた。ローカル (127.0.0.1) は 2ms で正常、host からの ``curl --http1.1``
も正常だったため、**既存の死活監視はすべて緑**のまま。検知できたのは利用者の報告だけ。

したがってここで固定すべき不変条件は 2 つ:
1. 検査は **HTTP/2 で** 行う (HTTP/1.1 で測ると障害が見えない)
2. 到達不能なら **ops へ通知する** (静かに失敗しない)
"""

from __future__ import annotations

from typing import Any

import pytest

from src.ui.services import public_reachability as pr


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _Client:
    """httpx.AsyncClient の代役。生成時の kwargs を記録する。"""

    seen_kwargs: list[dict[str, Any]] = []

    def __init__(self, status: int = 200, raises: Exception | None = None, **kwargs: Any) -> None:
        self._status = status
        self._raises = raises
        _Client.seen_kwargs.append(kwargs)

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str) -> _Resp:
        if self._raises is not None:
            raise self._raises
        return _Resp(self._status)


def _install(monkeypatch: pytest.MonkeyPatch, **client_kwargs: Any) -> None:
    import httpx

    _Client.seen_kwargs = []
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _Client(**{**client_kwargs, **kw}), raising=True
    )
    # 再試行の待ちを消す (2 回失敗で通知する仕様の検証を高速化)
    monkeypatch.setattr(pr, "_RETRY_WAIT_SECONDS", 0.0, raising=False)


@pytest.mark.asyncio
async def test_probes_over_http2(monkeypatch: pytest.MonkeyPatch) -> None:
    """検査は HTTP/2 で行う — HTTP/1.1 では 2026-08-24 の障害が再現しない。"""
    # Arrange
    _install(monkeypatch, status=200)
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "https://example.test")

    # Act
    result = await pr.check_public_reachability()

    # Assert
    assert result is not None and result.ok
    assert _Client.seen_kwargs and _Client.seen_kwargs[0].get("http2") is True


@pytest.mark.asyncio
async def test_skips_when_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """公開していなければ監視対象外 (通知も出さない)。"""
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "")

    assert await pr.check_public_reachability() is None
    assert (await pr.run_public_reachability_check()) == {"skipped": "no_public_url"}


@pytest.mark.asyncio
async def test_notifies_ops_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """到達不能なら ops へ通知する — 静かに失敗させない。"""
    # Arrange
    import httpx

    _install(monkeypatch, raises=httpx.ConnectError("closed"))
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "https://example.test")
    sent: list[dict[str, Any]] = []

    async def _fake_post(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", _fake_post)

    # Act
    out = await pr.run_public_reachability_check()

    # Assert
    assert out["ok"] is False
    assert len(sent) == 1
    assert "到達" in str(sent[0]["title"])


@pytest.mark.asyncio
async def test_non_200_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 以外は失敗として扱う (502/530 はトンネル側の典型的な故障)。"""
    _install(monkeypatch, status=530)
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "https://example.test")
    sent: list[dict[str, Any]] = []

    async def _fake_post(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", _fake_post)

    out = await pr.run_public_reachability_check()

    assert out["ok"] is False and out["status"] == 530
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_notify_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """通知が失敗しても監視ジョブ自体は落とさない。"""
    import httpx

    _install(monkeypatch, raises=httpx.ConnectError("closed"))
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "https://example.test")

    async def _boom(**kwargs: Any) -> None:
        raise RuntimeError("webhook down")

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", _boom)

    out = await pr.run_public_reachability_check()
    assert out["ok"] is False


def test_job_is_registered() -> None:
    """ジョブが registry に居る (居なければ一度も走らない)。"""
    from src.scheduler.job_registry import load_jobs

    job = next((j for j in load_jobs() if j.id == "public-reachability"), None)
    assert job is not None
    assert job.interval_minutes == 60


@pytest.mark.asyncio
async def test_probe_failure_is_not_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """検査器が動かない状態を障害として通知しない — 検証不能は反証ではない。

    初回デプロイで実際に踏んだ: ``h2`` 未導入で HTTP/2 を張れず ImportError となり、
    公開面は正常なのに「到達できません」を ops へ流した。
    """
    # Arrange
    _install(monkeypatch, raises=ImportError("the 'h2' package is not installed"))
    monkeypatch.setattr(pr, "resolve_public_base_url", lambda: "https://example.test")
    sent: list[dict[str, Any]] = []

    async def _fake_post(**kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", _fake_post)

    # Act
    out = await pr.run_public_reachability_check()

    # Assert
    assert out == {"probe_broken": True, "error": out["error"]}
    assert sent == []


def test_http2_dependency_is_installed() -> None:
    """HTTP/2 を張るための ``h2`` が入っていること。

    これが無いと監視は毎時 ImportError を出し続け、故障を見張っているつもりで
    何も見ていない状態になる。
    """
    import h2  # noqa: F401
