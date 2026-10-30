"""公開リポに収集先・分類先の実在ドメインを書かない関門 (2026-10-30、docs/local_overlay.md)。

実際の一覧は運用者ローカルの ``config/local/`` (gitignore) に置く。公開版の設定には
汎用の規則 (ccTLD) と RFC 2606 の予約名 (``*.example`` / ``*.test`` / ``*.invalid``) の
placeholder だけを許す。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESERVED_TLDS = ("example", "test", "invalid")


def _is_placeholder(host: str) -> bool:
    return host.strip().lower().rstrip(".").rsplit(".", 1)[-1] in _RESERVED_TLDS


def _load(rel: str) -> dict[str, object]:
    loaded = yaml.safe_load((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_committed_hostile_list_has_only_placeholders() -> None:
    cfg = _load("config/fetch/hostile_domains.yaml")
    entries = cfg.get("domains") or []
    assert isinstance(entries, list)
    names = [e.get("domain") if isinstance(e, dict) else e for e in entries]
    real = [n for n in names if not (isinstance(n, str) and _is_placeholder(n))]
    assert not real, "公開版の hostile_domains.yaml に実在のドメイン (config/local/ へ移す)"


def test_committed_hostile_cctlds_are_the_generic_rule() -> None:
    cfg = _load("config/fetch/hostile_domains.yaml")
    cctlds = cfg.get("cctlds")
    assert isinstance(cctlds, list)
    # ccTLD は汎用の規則として公開してよい (国の単位で、個別の収集先を示さない)
    assert all(isinstance(c, str) and "." not in c for c in cctlds)


def test_committed_adversary_official_has_only_placeholders() -> None:
    tier = _load("config/sources/source_reliability.yaml").get("adversary_official")
    assert isinstance(tier, dict)
    hosts = tier.get("hosts") or []
    assert isinstance(hosts, list)
    assert all(isinstance(h, str) and _is_placeholder(h) for h in hosts)
    # title は部分一致で実名になりやすいので公開版では空に保つ
    assert not (tier.get("titles") or [])


@pytest.mark.parametrize(
    ("ignore_file", "line"), [(".gitignore", "/config/local/"), (".dockerignore", "config/local/")]
)
def test_local_layer_is_ignored(ignore_file: str, line: str) -> None:
    lines = (_REPO_ROOT / ignore_file).read_text(encoding="utf-8").splitlines()
    assert line in lines


def test_no_local_layer_file_is_tracked() -> None:
    if shutil.which("git") is None or not (_REPO_ROOT / ".git").exists():
        pytest.skip("git の作業ツリーでのみ確かめる")
    out = subprocess.run(  # noqa: S603 — 固定の引数のみ
        ["git", "-C", str(_REPO_ROOT), "ls-files", "config/local"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == ""


def test_placeholder_rule() -> None:
    assert _is_placeholder("state.kuebiko.example")
    assert _is_placeholder("a.test")
    assert not _is_placeholder("example.com")
    assert not _is_placeholder("news.example.ru")
