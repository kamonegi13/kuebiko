"""公開の一覧が SSoT であることと、ローカル層の取り扱いの関門 (docs/local_overlay.md)。

敵対国の政府・国営媒体・軍の発信元の一覧は公開してよい (2026-10-30 利用者判断)。公開しないのは
運用者の帰属・識別につながるもの・資格情報・実インシデントの詳細で、``config/local/`` は
gitignore のまま追跡されないことだけを固定する。
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


def test_committed_hostile_list_is_the_source_of_truth() -> None:
    cfg = _load("config/fetch/hostile_domains.yaml")
    entries = cfg.get("domains") or []
    assert isinstance(entries, list)
    names = [e.get("domain") if isinstance(e, dict) else e for e in entries]
    # 公開版が SSoT (2026-10-30 利用者判断)。形式だけ確かめる (空でなく、host の形)
    assert names
    assert all(isinstance(n, str) and "." in n and n == n.lower() for n in names)


def test_committed_hostile_cctlds_are_the_generic_rule() -> None:
    cfg = _load("config/fetch/hostile_domains.yaml")
    cctlds = cfg.get("cctlds")
    assert isinstance(cctlds, list)
    assert all(isinstance(c, str) and "." not in c for c in cctlds)


def test_committed_adversary_official_hosts_are_listed() -> None:
    tier = _load("config/sources/source_reliability.yaml").get("adversary_official")
    assert isinstance(tier, dict)
    hosts = tier.get("hosts") or []
    assert isinstance(hosts, list) and hosts
    assert all(isinstance(h, str) and "." in h for h in hosts)


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
