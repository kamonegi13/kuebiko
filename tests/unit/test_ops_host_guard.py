"""公開面に運用画面のホスト名を出さない関門 (2026-11-20、11-19 監査 P0-1)。

アドバンスドの書き出し 30 ファイルに、日次ブリーフの Discord 文面の末尾リンク
(運用画面のホスト) がそのまま載っていた。実ドメインはリポジトリに書かず、実行時に
tunnel の hostname ファイル等から集めた**実ホストとの完全一致だけ**で止める
(``ops.`` 始まりや quick tunnel の形で止めると、IOC を書いた CTI 記事で止まる)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.ops_host_guard import (
    OpsHostUnavailableError,
    collect_ops_hosts,
    find_ops_host_refs,
)

HOST = "ops.kuebiko.example"


class TestFindOpsHostRefs:
    def test_finds_host_in_url(self) -> None:
        text = f"詳しくは https://{HOST}/app/daily-brief を参照"
        assert find_ops_host_refs(text, (HOST,)) == (HOST,)

    def test_case_insensitive(self) -> None:
        assert find_ops_host_refs("https://OPS.Kuebiko.Example/app", (HOST,)) == (HOST,)

    def test_public_domain_is_not_ops_host(self) -> None:
        text = "https://kuebiko.example/app/news と /app/daily-brief"
        assert find_ops_host_refs(text, (HOST,)) == ()

    def test_longer_label_is_not_a_match(self) -> None:
        assert find_ops_host_refs("https://xops.kuebiko.example/", (HOST,)) == ()
        assert find_ops_host_refs("https://ops.kuebiko.example.evil/", (HOST,)) == ()

    def test_attacker_quick_tunnel_in_ioc_is_allowed(self) -> None:
        # CTI 記事が攻撃者の quick tunnel を IOC として書くのは普通 — 止めない
        text = "C2 は random-words.trycloudflare.com と ops.attacker.example"
        assert find_ops_host_refs(text, (HOST,)) == ()

    def test_no_hosts_finds_nothing(self) -> None:
        assert find_ops_host_refs(f"https://{HOST}/", ()) == ()


class TestCollectOpsHosts:
    def test_reads_hostname_and_quick_tunnel_url(self, tmp_path: Path) -> None:
        (tmp_path / "hostname").write_text(f"https://{HOST}/\n", encoding="utf-8")
        (tmp_path / "url").write_text("https://abc-def.trycloudflare.com\n", encoding="utf-8")
        hosts = collect_ops_hosts(
            hostname_file=tmp_path / "hostname", url_file=tmp_path / "url", env_host=None
        )
        assert hosts == (HOST, "abc-def.trycloudflare.com")

    def test_env_host_is_added_and_deduplicated(self, tmp_path: Path) -> None:
        (tmp_path / "hostname").write_text(HOST, encoding="utf-8")
        hosts = collect_ops_hosts(
            hostname_file=tmp_path / "hostname",
            url_file=tmp_path / "missing",
            env_host=HOST.upper(),
        )
        assert hosts == (HOST,)

    def test_missing_files_give_empty(self, tmp_path: Path) -> None:
        hosts = collect_ops_hosts(
            hostname_file=tmp_path / "a", url_file=tmp_path / "b", env_host=None
        )
        assert hosts == ()

    def test_unreadable_existing_file_fails_closed(self, tmp_path: Path) -> None:
        # 在るのに読めない = 関門が黙って空になるので止める
        (tmp_path / "hostname").mkdir()
        with pytest.raises(OpsHostUnavailableError):
            collect_ops_hosts(
                hostname_file=tmp_path / "hostname", url_file=tmp_path / "b", env_host=None
            )
