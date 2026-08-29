"""遠隔 (Tier1) write の境界。

線引きは「書き先が DB か、ファイルか」(2026-08-29 利用者判断)。DB 由来の運用設定は
版履歴が残り revert できる。ファイル由来 (.env / raw YAML / .j2 直編集 / 名簿 yaml) は
版管理が無く、readonly では :ro マウントで物理的にも書けない。

⚠ **止めるものだけでなく通すものも固定する。** 名簿は fail-closed なので、
   足し忘れると「遠隔で書けない」形で静かに欠ける。
"""

from __future__ import annotations

import pytest

from src.ui.read_only_policy import (
    CREDENTIAL_WRITE_PATHS,
    REMOTE_WRITE_ALLOWLIST,
    is_credential_write,
    is_remote_writable,
)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/routing-rules",
        "/api/v1/channels",
        "/api/v1/product-routing",
        "/api/v1/pir/save",
        "/api/v1/pir/pir_china_apt",
        "/api/v1/pir/pir_china_apt/toggle",
        "/api/v1/match-lists",
        "/api/v1/sources/register",
        "/api/v1/sources/bulk",
        "/api/v1/model-tiers",
        "/api/v1/model-tiers/endpoints",
        "/api/v1/prompts/summarizer/rubric",
        "/api/v1/prompts/pir_spotlight/blocks",
        "/api/v1/dashboard/layout",
        "/api/v1/grok/tasks",
        "/api/v1/jobs/morning-brief/schedule",
        "/api/v1/schedule/direct-rss-fetch/cron",
        "/api/v1/schedule/web-scraper-watchers/scrapers/nicter/toggle",
        "/api/v1/spotlight/pir_china_apt/regenerate",
        "/api/v1/config-history/pir/revert",
        "/api/v1/jp-ci-operators",
        "/api/v1/config/source-quality",
    ],
)
def test_db_backed_paths_are_remote_writable(path: str) -> None:
    assert is_remote_writable(path), path


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("/api/v1/config/env", ".env"),
        ("/api/v1/config/system", ".env / system"),
        ("/api/v1/config/yaml", "raw YAML ファイル"),
        ("/api/v1/prompts/save", ".j2 の直編集"),
        ("/api/v1/actors/apt29", "actor_aliases.yaml"),
        ("/api/v1/grok/session/verify", "Playwright state"),
        ("/api/v1/subscriptions/reliability", "ファイル"),
        ("/api/v1/grok-mail", "IMAP 設定 (.env)"),
        ("/api/v1/mobile-tunnel/enable", "公開経路そのものの制御"),
        ("/api/v1/mobile-tunnel/named-config", "公開経路そのものの制御"),
        ("/api/v1/host-watchdog/disable", "ホスト制御"),
        ("/api/v1/model-tiers/claudecode-update", "ホストの CLI 更新"),
        ("/api/v1/history/purge", "破壊的なデータ削除"),
        ("/api/v1/taxonomy-review/p1/approve", "ファイル"),
    ],
)
def test_file_backed_and_risky_paths_are_not_remote_writable(path: str, why: str) -> None:
    assert not is_remote_writable(path), f"{path} ({why}) が遠隔で書けてしまう"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/model-tiers/anthropic-key",
        "/api/v1/model-tiers/claudecode-token",
        "/api/v1/model-tiers/endpoint-key",
        "/api/v1/model-tiers/ollama-url",
        # webhook URL は URL の形をしているが **それ自体が資格情報**。
        "/api/v1/channels/alert/webhook",
        "/api/v1/channels/brief/webhook",
    ],
)
def test_credential_paths_are_always_blocked(path: str) -> None:
    assert is_credential_write(path), path
    # 名簿にも入っていないこと (二重に落ちる必要はないが、入っていたら設計ミス)
    assert not is_remote_writable(path), f"{path} が遠隔 write の名簿に入っている"


def test_prefix_match_does_not_leak_into_subpaths() -> None:
    """`/api/v1/channels` を許しても `/api/v1/channels/{id}/webhook` は許さない。

    ⚠ 前方一致で名簿を書くと、後から生えた下位 path を意図せず巻き込む。
    """
    assert is_remote_writable("/api/v1/channels")
    assert not is_remote_writable("/api/v1/channels/alert/webhook")
    assert is_remote_writable("/api/v1/model-tiers")
    assert not is_remote_writable("/api/v1/model-tiers/anthropic-key")
    assert is_remote_writable("/api/v1/prompts/summarizer/rubric")
    assert not is_remote_writable("/api/v1/prompts/save")


def test_unlisted_paths_are_denied() -> None:
    """fail-closed。名簿に無い write は、見落としでも「書けない」に倒れる。"""
    assert not is_remote_writable("/api/v1/some/new/endpoint")
    assert not is_remote_writable("/api/v1/")
    assert not is_remote_writable("")


def test_allowlist_has_no_wildcard_first_segment() -> None:
    """`*` だけのセグメントで始まる包括許可を防ぐ (事故で全開放しないため)。"""
    for template in (*REMOTE_WRITE_ALLOWLIST, *CREDENTIAL_WRITE_PATHS):
        assert template.startswith("/api/v1/"), template
        assert "*" not in template.split("/")[3], f"{template} が第 1 セグメントを開放している"
