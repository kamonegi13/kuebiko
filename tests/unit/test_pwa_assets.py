"""PWA 資産 (manifest / アイコン) の配信と整合のテスト。

守りたいのは主に 1 点: ``/app/{path:path}`` の SPA fallback が manifest を
index.html で上書きしないこと。上書きされても HTTP は 200 を返すため、
「インストールできない」以外に兆候が出ない静かな故障になる。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
PWA_SRC_DIR = REPO_ROOT / "frontend" / "public" / "pwa"


def _bootstrap_project(tmp_path: Path) -> Path:
    """create_app() が要求する最小構成 + frontend/dist (pwa 入り) を作る。"""
    (tmp_path / "prompts" / "briefing").mkdir(parents=True)
    (tmp_path / "prompts" / "briefing/summarizer.j2").write_text("test prompt", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipelines.yaml").write_text(
        "pipelines:\n  - name: direct-rss-fetch\n"
        "    source:\n      type: rss\n      max_articles: 10\n"
        "    processor: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text("", encoding="utf-8")

    # vite build の出力を模した dist (public/pwa/* は dist/pwa/* に素通しで入る)
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
    (dist / "pwa").mkdir()
    for name in ("manifest.webmanifest", "icon-192.png", "icon.svg"):
        (dist / "pwa" / name).write_bytes((PWA_SRC_DIR / name).read_bytes())

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)

    from src.ui import app as app_module

    with TestClient(app_module.create_app()) as c:
        yield c


def _manifest() -> dict[str, Any]:
    raw = (PWA_SRC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def test_manifest_is_json_not_spa_html(client: TestClient) -> None:
    """manifest は SPA fallback に食われず、JSON として配信される。"""
    # Act
    resp = client.get("/app/pwa/manifest.webmanifest")

    # Assert
    assert resp.status_code == 200
    assert "html" not in resp.headers["content-type"]
    assert resp.json()["scope"] == "/app/"


def test_icon_is_served_as_png(client: TestClient) -> None:
    """アイコンも同様に SPA fallback ではなく実体が返る。"""
    # Act
    resp = client.get("/app/pwa/icon-192.png")

    # Assert
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    # PNG シグネチャ (HTML が返っていないことの実体確認)
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_app_path_still_falls_back_to_spa(client: TestClient) -> None:
    """/app/pwa の mount を足しても、通常の deep link は index.html に落ちる。"""
    # Act
    resp = client.get("/app/dashboard")

    # Assert
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_manifest_scope_matches_vite_base() -> None:
    """manifest の scope/start_url は vite の base と一致していなければならない。

    ずれると standalone 判定が外れ、ホーム画面から起動しても普通のタブになる。
    """
    # Arrange
    vite_config = (REPO_ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    manifest = _manifest()

    # Assert
    assert 'base: "/app/"' in vite_config
    assert manifest["scope"] == "/app/"
    assert manifest["start_url"] == "/app/"


def test_manifest_icons_exist_in_source() -> None:
    """manifest が参照するアイコンが public/pwa に実在する (リンク切れ防止)。"""
    # Arrange
    manifest = _manifest()

    # Assert
    for icon in manifest["icons"]:
        src: str = icon["src"]
        assert src.startswith("/app/pwa/"), src
        assert (PWA_SRC_DIR / src.removeprefix("/app/pwa/")).exists(), src
