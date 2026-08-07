"""src.tools.discord_publisher のテスト (Step 7)。

DiscordWebhook.execute は同期 HTTP クライアント (requests) を使うため、
``unittest.mock`` で webhook クラスごとモックする方針。整形ロジックは
DiscordWebhook の作りを直接覗いて検証する。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.tools.discord_publisher import (
    DESC_CHUNK_SAFE,
    EMBED_TITLE_MAX,
    FIELD_VALUE_SAFE,
    IMPORTANCE_COLORS,
    BriefingMessage,
    DiscordPostError,
    DiscordPublisher,
    PostResult,
    Source,
    _chunk_text,
    _format_iocs,
    _format_mitre_techniques,
    _format_sources,
    _mitre_url,
)

# ---------- ヘルパ ----------

WEBHOOK_URL = "https://discord.com/api/webhooks/1/test"


def _make_message(**overrides: Any) -> BriefingMessage:
    base: dict[str, Any] = {
        "title": "Test article",
        "bluf": "新たなフィッシング攻撃が観測された",
        "importance": "medium",
        "category": "phishing",
        "summary": "詳細な要約。" * 20,
        "iocs": [],
        "mitre_techniques": [],
        "sources": [],
        "analyst_note": None,
        "metadata": {},
    }
    base.update(overrides)
    return BriefingMessage(**base)


@pytest.fixture
def mock_webhook_class() -> Iterator[tuple[Any, list[MagicMock]]]:
    """``DiscordWebhook`` をクラスごとモック。生成されたインスタンス一覧も返す。"""
    instances: list[MagicMock] = []

    def factory(*args: Any, **kwargs: Any) -> MagicMock:
        inst = MagicMock()
        inst.url = kwargs.get("url") or (args[0] if args else None)
        inst.username = kwargs.get("username")
        inst.embeds = []
        inst.add_embed.side_effect = lambda e: inst.embeds.append(e)
        inst.execute.return_value = MagicMock(status_code=204, text="")
        instances.append(inst)
        return inst

    with patch(
        "src.tools.discord_publisher.DiscordWebhook",
        side_effect=factory,
    ) as mock_class:
        yield mock_class, instances


# ---------- 整形ヘルパ単体 ----------


class TestChunkText:
    def test_short_text_unchanged(self) -> None:
        assert _chunk_text("hello", 100) == ["hello"]

    def test_splits_by_lines(self) -> None:
        text = "line1\nline2\nline3\nline4"
        chunks = _chunk_text(text, max_chars=12)
        assert len(chunks) >= 2
        # 全行の連結が元と一致 (改行込み)
        assert "\n".join(chunks) == text

    def test_each_chunk_within_limit(self) -> None:
        text = "\n".join(f"line-{i:03d}" for i in range(50))
        chunks = _chunk_text(text, max_chars=50)
        for c in chunks:
            assert len(c) <= 50


class TestMitreFormatting:
    @pytest.mark.parametrize(
        ("tid", "expected"),
        [
            ("T1566", "https://attack.mitre.org/techniques/T1566/"),
            ("T1566.001", "https://attack.mitre.org/techniques/T1566/001/"),
            ("T1059.003", "https://attack.mitre.org/techniques/T1059/003/"),
        ],
    )
    def test_valid_id_returns_url(self, tid: str, expected: str) -> None:
        assert _mitre_url(tid) == expected

    @pytest.mark.parametrize("tid", ["X1566", "T", "T-1566", "1566", "TA0001"])
    def test_invalid_id_returns_none(self, tid: str) -> None:
        assert _mitre_url(tid) is None

    def test_format_creates_markdown_links(self) -> None:
        out = _format_mitre_techniques(["T1566", "T1566.001"])
        assert "[T1566](https://attack.mitre.org/techniques/T1566/)" in out
        assert "[T1566.001](https://attack.mitre.org/techniques/T1566/001/)" in out

    def test_format_falls_back_for_invalid(self) -> None:
        out = _format_mitre_techniques(["BAD-ID"])
        assert "BAD-ID" in out
        assert "(http" not in out  # リンクなし

    def test_format_truncates_long_list(self) -> None:
        many = [f"T{i:04d}" for i in range(1, 200)]
        out = _format_mitre_techniques(many)
        assert len(out) <= FIELD_VALUE_SAFE
        assert "他" in out and "件" in out  # 切り詰めマーカー


class TestIocFormatting:
    def test_wraps_in_code_block(self) -> None:
        out = _format_iocs(["1.2.3.4", "evil.example.com"])
        assert out.startswith("```\n")
        assert out.endswith("\n```")
        assert "1.2.3.4" in out
        assert "evil.example.com" in out

    def test_truncates_long_list(self) -> None:
        many = [f"ioc-{i}.example.com" for i in range(500)]
        out = _format_iocs(many)
        assert len(out) <= FIELD_VALUE_SAFE
        assert "他" in out


class TestSourcesFormatting:
    def test_renders_markdown_link(self) -> None:
        out = _format_sources([Source(title="Bleeping Computer", url="https://bc.com/x")])
        assert "[Bleeping Computer](https://bc.com/x)" in out
        assert "(en)" in out

    def test_truncates_long_titles(self) -> None:
        long_title = "A" * 200
        out = _format_sources([Source(title=long_title, url="https://x.com/")])
        # 80 文字を超える元タイトルは切られる
        assert "A" * 81 not in out


# ---------- BriefingMessage / モデル ----------


class TestBriefingMessage:
    def test_is_frozen(self) -> None:
        msg = _make_message()
        with pytest.raises(Exception):  # noqa: B017, BLE001
            msg.title = "x"

    def test_invalid_importance_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017, BLE001
            _make_message(importance="ultra")


# ---------- DiscordPublisher: 入力検証 ----------


class TestPublisherInit:
    def test_empty_url_raises(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            DiscordPublisher(webhook_url="")

    def test_zero_rate_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="rate_limit_per_second"):
            DiscordPublisher(webhook_url=WEBHOOK_URL, rate_limit_per_second=0)

    def test_negative_rate_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="rate_limit_per_second"):
            DiscordPublisher(webhook_url=WEBHOOK_URL, rate_limit_per_second=-1)

    def test_min_interval_property(self) -> None:
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL, rate_limit_per_second=4)
        assert pub.min_interval_seconds == pytest.approx(0.25)


# ---------- 整形 (Embed の中身を検証) ----------


def _embed_attrs(embed: Any) -> dict[str, Any]:
    """DiscordEmbed (実体) の主要属性を dict で取り出す (テスト用)。"""
    return {
        "title": getattr(embed, "title", None),
        "description": getattr(embed, "description", None),
        "color": getattr(embed, "color", None),
        "fields": list(getattr(embed, "fields", []) or []),
    }


class TestEmbedShape:
    def test_high_importance_sets_red_color(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        msg = _make_message(importance="high")
        webhooks = pub._build_webhooks(msg)
        assert len(webhooks) == 1
        embed = instances[0].embeds[0]
        assert _embed_attrs(embed)["color"] == IMPORTANCE_COLORS["high"]

    def test_medium_importance_sets_yellow_color(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(importance="medium"))
        assert _embed_attrs(instances[0].embeds[0])["color"] == IMPORTANCE_COLORS["medium"]

    def test_low_importance_sets_gray_color(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(importance="low"))
        assert _embed_attrs(instances[0].embeds[0])["color"] == IMPORTANCE_COLORS["low"]

    def test_bluf_in_bold_at_top_of_description(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(bluf="重大なゼロデイが公開された"))
        desc = _embed_attrs(instances[0].embeds[0])["description"]
        assert desc.startswith("**重大なゼロデイが公開された**")

    def test_iocs_become_code_block_field(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(iocs=["1.2.3.4", "phish.example.com"]),
        )
        fields = _embed_attrs(instances[0].embeds[0])["fields"]
        ioc_field = next(f for f in fields if "IOC" in f.get("name", ""))
        assert ioc_field["value"].startswith("```")
        assert "1.2.3.4" in ioc_field["value"]

    def test_mitre_field_contains_links(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(mitre_techniques=["T1566.001"]))
        fields = _embed_attrs(instances[0].embeds[0])["fields"]
        mitre = next(f for f in fields if "MITRE" in f.get("name", ""))
        assert "https://attack.mitre.org/techniques/T1566/001/" in mitre["value"]

    def test_long_summary_splits_into_multiple_webhooks(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        # description にそのまま乗る形で長文を作る
        long_summary = "\n".join(["パラグラフ" * 50] * 30)  # 各 600+ 文字 × 30 行
        webhooks = pub._build_webhooks(_make_message(summary=long_summary))
        assert len(webhooks) >= 2
        # 続き embed のタイトルに "(続き ...)" が付く
        second_embed = instances[1].embeds[0]
        assert "続き" in _embed_attrs(second_embed)["title"]
        # 各 description は安全マージン以下
        for inst in instances:
            for embed in inst.embeds:
                desc = _embed_attrs(embed)["description"]
                assert len(desc) <= DESC_CHUNK_SAFE

    def test_title_truncated_to_discord_limit(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(title="A" * 500))
        title = _embed_attrs(instances[0].embeds[0])["title"]
        assert len(title) <= EMBED_TITLE_MAX


# ---------- 投稿実行 (mock 経由) ----------


class TestPostExecution:
    @pytest.mark.asyncio
    async def test_post_calls_execute_once_for_short_message(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        await pub.post(_make_message())
        assert len(instances) == 1
        assert instances[0].execute.call_count == 1

    @pytest.mark.asyncio
    async def test_post_attaches_file_when_attachments_provided(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """attachments が指定された場合、最初の webhook に add_file が呼ばれる。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        attachments = [("stix-bundle.json", b'{"type":"bundle"}')]
        await pub.post(_make_message(), attachments=attachments)
        assert len(instances) == 1
        instances[0].add_file.assert_called_once_with(
            file=b'{"type":"bundle"}',
            filename="stix-bundle.json",
        )

    @pytest.mark.asyncio
    async def test_post_no_attach_when_attachments_none(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """attachments=None / 空 では add_file は呼ばれない (後方互換)。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        await pub.post(_make_message())
        assert instances[0].add_file.call_count == 0
        await pub.post(_make_message(), attachments=[])
        # 2 件目 webhook も add_file 未呼び出し
        assert instances[-1].add_file.call_count == 0

    @pytest.mark.asyncio
    async def test_post_attaches_only_to_first_webhook_when_split(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """summary が長くて複数 webhook に分割される場合、添付は先頭にだけ付く。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        long_summary = "\n".join(["パラグラフ" * 50] * 30)
        await pub.post(
            _make_message(summary=long_summary),
            attachments=[("stix.json", b"{}")],
        )
        assert len(instances) >= 2
        instances[0].add_file.assert_called_once()
        for inst in instances[1:]:
            assert inst.add_file.call_count == 0

    @pytest.mark.asyncio
    async def test_post_raises_on_4xx_response(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class

        # 次に作成される webhook の execute は 401 を返すよう仕込む
        def factory_with_error(*args: Any, **kwargs: Any) -> MagicMock:
            inst = MagicMock()
            inst.embeds = []
            inst.add_embed.side_effect = lambda e: inst.embeds.append(e)
            inst.execute.return_value = MagicMock(status_code=401, text="Unauthorized")
            instances.append(inst)
            return inst

        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory_with_error,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            with pytest.raises(DiscordPostError, match="401"):
                await pub.post(_make_message())

    @pytest.mark.asyncio
    async def test_post_batch_aggregates_failures(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, _instances = mock_webhook_class
        # 2 件目だけ 500 になるよう、回数で切り替え
        call_count = {"n": 0}

        def factory(*args: Any, **kwargs: Any) -> MagicMock:
            call_count["n"] += 1
            inst = MagicMock()
            inst.embeds = []
            inst.add_embed.side_effect = lambda e: inst.embeds.append(e)
            status = 500 if call_count["n"] == 2 else 204
            inst.execute.return_value = MagicMock(status_code=status, text="")
            return inst

        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            result = await pub.post_batch(
                [
                    _make_message(title="ok-1"),
                    _make_message(title="bad-2"),
                    _make_message(title="ok-3"),
                ],
            )

        assert isinstance(result, PostResult)
        assert result.total == 3
        assert result.succeeded == 2
        assert result.failed == 1
        assert any("bad-2" in f for f in result.failures)


# ---------- レート制限 ----------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_consecutive_posts_respect_min_interval(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, _instances = mock_webhook_class
        pub = DiscordPublisher(
            webhook_url=WEBHOOK_URL,
            rate_limit_per_second=10,  # min interval 0.1s
        )
        msg = _make_message()
        start = time.monotonic()
        await pub.post(msg)
        await pub.post(msg)
        await pub.post(msg)
        elapsed = time.monotonic() - start
        # 3 件で最低 (3-1) * 0.1 = 0.2 秒。テストの計測誤差を含めて 0.18 秒以上。
        assert elapsed >= 0.18

    @pytest.mark.asyncio
    async def test_first_post_has_no_delay(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, _instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL, rate_limit_per_second=2)
        start = time.monotonic()
        await pub.post(_make_message())
        elapsed = time.monotonic() - start
        # 初回は遅延なし (mock execute は即座に返るので 0.1 秒未満)
        assert elapsed < 0.1


# ---------- Rich embed (T1+T2: content / author / footer / category emoji) ----------


class TestRichEmbed:
    def test_content_preview_includes_importance_emoji_and_category_flag(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                importance="high",
                category="china_apt",
                title="PurpleHaze SentinelOne 顧客標的",
            ),
        )
        wh = instances[0]
        wh.set_content.assert_called_once()
        content = wh.set_content.call_args.args[0]
        # Phase 5D: 色ベース重要度 (🔴 = high)、[HIGH] 文字タグは廃止
        assert "🔴" in content
        assert "🇨🇳" in content  # china_apt の国旗
        assert "中国関連" in content
        assert "PurpleHaze" in content  # title が出る (BLUF 重複回避)

    def test_content_preview_unknown_category_no_flag(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """Inoreader 経路の自由文字列カテゴリは国旗もタグも出さず色重要度のみ。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                importance="high",
                category="apt",  # Inoreader 経路の LLM 出力例
                title="BlueNoroff Fake Zoom Calls",
            ),
        )
        content = instances[0].set_content.call_args.args[0]
        # Phase 5D: 色重要度のみ、[HIGH] タグは廃止
        assert "🔴" in content
        assert "🛡️" not in content
        assert "BlueNoroff" in content

    def test_japan_targeted_uses_dedicated_label(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                importance="high",
                category="japan_targeted",
                title="某省庁への偵察活動",
            ),
        )
        content = instances[0].set_content.call_args.args[0]
        assert "🇯🇵" in content
        assert "日本標的" in content

    def test_medium_importance_uses_yellow_color(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """Phase 5D: medium = 🟡 (色ベース、文字タグなし)。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                importance="medium",
                category="noted_events",
                title="weekly notes",
            ),
        )
        content = instances[0].set_content.call_args.args[0]
        assert "🟡" in content
        assert "[MEDIUM" not in content

    def test_author_set_with_category_emoji(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="north_korea_apt",
                metadata={"grok_chat_url": "https://grok.com/chat/abc"},
            ),
        )
        embed = instances[0].embeds[0]
        author = getattr(embed, "author", {}) or {}
        assert "🇰🇵" in author.get("name", "")
        assert author.get("url") == "https://grok.com/chat/abc"

    def test_author_skipped_for_unknown_category(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """Inoreader 経路の自由文字列カテゴリでは author を出さない。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(category="apt"))
        embed = instances[0].embeds[0]
        author = getattr(embed, "author", {}) or {}
        # 設定されていれば name に "🛡️ apt" のような文字列が入るはずだが、
        # スキップしているので空または name キー欠落
        assert not author.get("name")

    def test_footer_includes_section_and_id(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="china_apt",
                metadata={
                    "grok_section": "中国関連 (MSS / PLA直轄系)",
                    "grok_email_uid": "uid-123",
                },
            ),
        )
        embed = instances[0].embeds[0]
        footer = getattr(embed, "footer", {}) or {}
        text = footer.get("text", "")
        assert "中国関連" in text
        assert "uid-123" in text

    def test_timestamp_set_when_received_at_provided(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                metadata={"received_at": "2026-05-02T06:35:00+00:00"},
            ),
        )
        embed = instances[0].embeds[0]
        ts = getattr(embed, "timestamp", None)
        # discord-webhook は ISO 8601 文字列で保持する
        assert ts is not None and "2026-05-02" in str(ts)

    def test_actor_and_confidence_extracted_from_note(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="noted_events",
                analyst_note="アクター: APT34（Iranian） / 信頼性: high=2, medium=1",
            ),
        )
        fields = _embed_attrs(instances[0].embeds[0])["fields"]
        actor_field = next(f for f in fields if "アクター" in f.get("name", ""))
        confidence_field = next(f for f in fields if "信頼性" in f.get("name", ""))
        assert "APT34" in actor_field["value"]
        assert "high=2" in confidence_field["value"]

    def test_field_names_have_emoji_prefix(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                iocs=["CVE-2025-21756"],
                mitre_techniques=["T1566.001"],
                sources=[Source(title="@x", url="https://x.com/x", language="ja")],
            ),
        )
        fields = _embed_attrs(instances[0].embeds[0])["fields"]
        names = [f.get("name", "") for f in fields]
        assert any(n.startswith("🛡️") for n in names)
        assert any(n.startswith("🔍") for n in names)
        assert any(n.startswith("📎") for n in names)


# ---------- BLUF と本文の重複回避 (Phase 2.6a 改善) ----------


class TestDescriptionBlufDeduplication:
    def test_bluf_substring_of_summary_collapses(self) -> None:
        """BLUF が summary に substring 含まれる場合、冒頭の重複は削除される。"""
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        bluf = "直近24時間において新規大規模APTキャンペーンは確認されませんでした。"
        summary = bluf + " 継続中の中国系活動などが背景として言及される程度。"
        # title 30 字以上が前提 (短縮された BLUF)
        msg = _make_message(bluf=bluf, summary=summary, title=f"🔴 {bluf}")
        desc = pub._compose_description(msg)
        # title 本文は description 内で重複表示されない
        assert bluf.strip() not in desc
        assert "継続中の中国系活動" in desc

    def test_bluf_distinct_from_summary_keeps_both(self) -> None:
        """BLUF と summary が別物 (Inoreader 経路) なら両方出す。"""
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        bluf = "BLUF: phishing campaign observed."
        summary = "詳細な本文。BLUF とは違う表現で日本語要約が続く。"
        msg = _make_message(bluf=bluf, summary=summary)
        desc = pub._compose_description(msg)
        assert f"**{bluf}**" in desc
        assert summary in desc

    def test_summary_without_bluf_renders(self) -> None:
        """BLUF が空なら summary だけを表示。"""
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        msg = _make_message(bluf="", summary="本文のみ。")
        desc = pub._compose_description(msg)
        assert "本文のみ。" in desc
        assert "****" not in desc  # 空 BLUF が太字で出ない


# ---------- Phase 2.8a: 表示磨き込み ----------


class TestDisplay28aImprovements:
    def test_observed_field_no_longer_emitted(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """⏰ 観測 field は削除済み (footer + embed.timestamp に集約)。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                metadata={"received_at": "2026-05-02T06:35:00+00:00"},
            ),
        )
        fields = _embed_attrs(instances[0].embeds[0])["fields"]
        assert not any("観測" in f.get("name", "") for f in fields)

    def test_author_includes_incident_count(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="china_apt",
                metadata={"incident_count": 3},
            ),
        )
        embed = instances[0].embeds[0]
        author = getattr(embed, "author", {}) or {}
        assert "3 incidents" in author.get("name", "")

    def test_author_omits_incident_count_when_zero(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="china_apt",
                metadata={"incident_count": 0},
            ),
        )
        embed = instances[0].embeds[0]
        author = getattr(embed, "author", {}) or {}
        assert "incidents" not in author.get("name", "")

    def test_footer_contains_observation_window(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="china_apt",
                metadata={"received_at": "2026-05-02T06:30:00+09:00"},
            ),
        )
        embed = instances[0].embeds[0]
        footer = getattr(embed, "footer", {}) or {}
        text = footer.get("text", "")
        assert "📅" in text
        assert "JST" in text
        # 24h 前の 5/1 06:30 が含まれる
        assert "05/01" in text and "05/02" in text

    def test_description_has_cta_link(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(
            _make_message(
                category="china_apt",
                metadata={"grok_chat_url": "https://grok.com/chat/abc"},
            ),
        )
        desc = _embed_attrs(instances[0].embeds[0])["description"]
        assert "Grok レポートで詳細" in desc
        assert "https://grok.com/chat/abc" in desc

    def test_description_no_cta_when_url_missing(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """Inoreader 経路 (chat_url なし) では CTA を出さない。"""
        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        pub._build_webhooks(_make_message(category="apt"))
        desc = _embed_attrs(instances[0].embeds[0])["description"]
        assert "Grok レポートで詳細" not in desc


# ---------- Phase 2.8a2: paragraph formatting + label bolding ----------


class TestParagraphFormatting:
    def test_attribute_lines_become_bold_labels(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = (
            "発生した政府・軍事目的の活動：新規キャンペーンの報告なし。Volt Typhoon 等。\n"
            "新規/更新されたIOC：特記事項なし。\n"
            "信頼できる出典：FBI 公式投稿、台湾国防部、CSIS 等。\n"
            "信頼性メモ：継続監視情報中心。\n"
        )
        out = _format_paragraphs(text)
        assert "**発生した政府・軍事目的の活動**: 新規キャンペーン" in out
        assert "**新規/更新されたIOC**: 特記事項なし。" in out
        assert "**信頼できる出典**: FBI 公式投稿" in out
        assert "**信頼性メモ**: 継続監視情報中心。" in out
        # 段落間に空行が入る (Discord で段落として描画される)
        assert "\n\n" in out

    def test_lines_without_label_kept_as_is(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = "通常の文章。\n別の段落。\n"
        out = _format_paragraphs(text)
        assert "通常の文章。" in out
        assert "別の段落。" in out
        # 太字化されない
        assert "**" not in out

    def test_list_prefix_lines_not_bolded(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = "- 事象1：x\n- 事象2：y\n"
        out = _format_paragraphs(text)
        # `- 事象1：x` が `**- 事象1**: x` のような誤変換をしないこと
        assert "**事象1**" not in out
        assert "- 事象1" in out

    def test_url_lines_not_treated_as_labels(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = "リンク: https://example.com/path\n"
        out = _format_paragraphs(text)
        # url のスキーマっぽいコロンを誤検出しないこと (label が "リンク" で
        # 値が "https://..." だが、label は短く OK 範囲なので変換される)
        # ただし URL 自体は破壊されない
        assert "https://example.com/path" in out


class TestAnalystNoteRendering:
    def test_field_only_note_is_omitted(self) -> None:
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        msg = _make_message(
            analyst_note="アクター: APT34 / 信頼性: high=2",
        )
        desc = pub._compose_description(msg)
        assert "アナリスト所見" not in desc

    def test_substantive_note_uses_bold_label(self) -> None:
        """field と異なる実質的な所見は太字ラベルで表示する。"""
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        note = "セキュリティベンダーへの攻撃は二次的な侵入経路として注意が必要"
        msg = _make_message(analyst_note=note)
        desc = pub._compose_description(msg)
        # markdown のイタリック (_..._) ではなく太字 (**...**) を採用
        assert "_アナリスト所見_" not in desc
        assert "**アナリスト所見**" in desc
        assert note in desc

    def test_confidence_only_note_is_omitted(self) -> None:
        from src.tools.discord_publisher import DiscordPublisher

        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        msg = _make_message(analyst_note="信頼性: high=1")
        desc = pub._compose_description(msg)
        assert "アナリスト所見" not in desc
        assert "信頼性: high=1" not in desc  # field 側で出るため description には不要


# ---------- R1+R2: BLUF 冒頭重複削除 + 事象N 見出し化 ----------


class TestRedundancyAndIncidentHeading:
    def test_title_prefix_removed_from_summary_line(self) -> None:
        from src.tools.discord_publisher import _trim_overlap_with_title

        title = "🔴 発生した政府・軍事目的の活動・キャンペーン：新規キャンペーンの報告なし。"
        summary = (
            "発生した政府・軍事目的の活動・キャンペーン：新規キャンペーンの報告なし。"
            "Volt Typhoon、Salt Typhoon が再言及。\n"
            "新規/更新されたIOC：特記事項なし。"
        )
        out = _trim_overlap_with_title(summary, title)
        # 1 行目から title 部分が削除され、残りが残る
        assert out.startswith("Volt Typhoon")
        assert "新規/更新されたIOC：特記事項なし。" in out
        # title 本文は description 内に出ない
        assert "発生した政府・軍事目的の活動・キャンペーン" not in out

    def test_title_with_ellipsis_suffix_handled(self) -> None:
        """title 末尾の "…" を無視して比較できる。"""
        from src.tools.discord_publisher import _trim_overlap_with_title

        title_body = "直近24時間において新規大規模APTキャンペーンは確認されませんでした。"
        title = f"🔴 {title_body}…"
        summary = f"{title_body} 具体的には Volt Typhoon の事前配置活動。"
        out = _trim_overlap_with_title(summary, title)
        assert "具体的には Volt Typhoon" in out
        assert out.startswith("具体的には") or out == ""

    def test_h3_incident_body_overlap_with_title_removed(self) -> None:
        """### 事象 N 見出し直後の本文行が title prefix と重複する場合も削除。"""
        from src.tools.discord_publisher import _trim_overlap_with_title

        # 30 字以上の title (実運用に近い長さ)
        title = "🔴 北朝鮮系ハッカー（Lazarusなど）が2026年に暗号資産ハック総損失の76%を占める。"
        summary = (
            "事象 1\n"
            "北朝鮮系ハッカー（Lazarusなど）が2026年に暗号資産ハック総損失の76%を占める。"
            "2026年累計で$6B超の過去実績も指摘。"
        )
        out = _trim_overlap_with_title(summary, title)
        # 事象見出し行は残る
        assert "事象 1" in out
        # title 本文と重複する prefix は削除されている
        assert "2026年累計で$6B超" in out
        # title prefix 部分が事象 1 の直後にそのまま再掲されていないこと
        body_lines = [line for line in out.split("\n") if line.strip()]
        # 事象 1 の直後の行は title prefix で始まらない
        if len(body_lines) >= 2:
            assert not body_lines[1].startswith("北朝鮮系ハッカー（Lazarusなど）")

    def test_short_title_prefix_match_no_action(self) -> None:
        """30 文字未満の title で **prefix 一致のみ** は誤削除を避けるため何もしない。"""
        from src.tools.discord_publisher import _trim_overlap_with_title

        title = "🔴 短いタイトル"
        summary = "短いタイトルとそれ以外の文章"  # prefix 一致だが続きあり
        out = _trim_overlap_with_title(summary, title)
        assert out == summary

    def test_short_title_exact_match_removed(self) -> None:
        """30 文字未満の title でも **完全一致行** は削除する (重複表示防止)。"""
        from src.tools.discord_publisher import _trim_overlap_with_title

        title = "🔴 中国関連 (MSS / PLA直轄系)"
        summary = "中国関連 (MSS / PLA直轄系)\n他の本文"
        out = _trim_overlap_with_title(summary, title)
        assert "他の本文" in out
        assert "中国関連 (MSS / PLA直轄系)" not in out

    def test_multi_embed_mode_produces_one_webhook_with_multiple_embeds(
        self,
        mock_webhook_class: tuple[Any, list[MagicMock]],
    ) -> None:
        """incidents が非空のときは 1 webhook に各 incident + 集約 embed が並ぶ。"""
        from src.tools.discord_publisher import BriefingIncident

        _, instances = mock_webhook_class
        pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
        msg = _make_message(
            title="🔴 2 件の事象 (PurpleHaze, TheWizards)",
            bluf="2 件の事象 (PurpleHaze, TheWizards)",
            importance="high",
            category="china_apt",
            summary="",
            sources=[Source(title="@all", url="https://x.com/")],
            incidents=[
                BriefingIncident(
                    heading="事象 1",
                    body="PurpleHaze が SentinelOne を標的にした攻撃。",
                    related_actor="PurpleHaze",
                    confidence="high",
                    sources=[Source(title="@TheHackersNews", url="https://x.com/TheHackersNews")],
                ),
                BriefingIncident(
                    heading="事象 2",
                    body="TheWizards の IPv6 SLAAC AitM 攻撃。",
                    related_actor="TheWizards",
                    confidence="medium",
                ),
            ],
        )
        webhooks = pub._build_webhooks(msg)
        assert len(webhooks) == 1
        # incidents 2 件 = 2 embeds (集約 embed は廃止、最後の事象に集約メタ情報を統合)
        embeds = instances[0].embeds
        assert len(embeds) == 2
        # 1 つ目の embed が 事象 1
        assert "事象 1" in str(_embed_attrs(embeds[0])["title"])
        assert "PurpleHaze" in str(_embed_attrs(embeds[0])["description"])
        # 2 つ目が 事象 2 (最後 = 観測期間 / CTA / footer 付き)
        assert "事象 2" in str(_embed_attrs(embeds[1])["title"])
        assert "TheWizards" in str(_embed_attrs(embeds[1])["description"])

    def test_incident_head_line_preserved(self) -> None:
        """``事象N：`` を含む行は構造保持のため触らない (誤削除防止)。"""
        from src.tools.discord_publisher import _trim_overlap_with_title

        # 仮に title が事象 1 内容と偶然一致しても、事象見出し行は保護される
        title = "🔴 中国系ハッカーがアジア諸国政府を標的としたキャンペーンが進行中"
        summary = "事象1：中国系ハッカーがアジア諸国政府を標的としたキャンペーンが進行中\n出典: X"
        out = _trim_overlap_with_title(summary, title)
        # 事象1 行は完全に残る (構造保持優先)
        assert "事象1" in out
        assert "中国系ハッカー" in out
        assert "出典: X" in out

    def test_no_match_keeps_summary_intact(self) -> None:
        from src.tools.discord_publisher import _trim_overlap_with_title

        title = "🔴 前提となる別の説明文がある程度長く30文字以上書いてあります"
        summary = "実際の本文 1 行目で title とは違う\n本文 2 行目"
        out = _trim_overlap_with_title(summary, title)
        assert out == summary

    def test_incident_head_becomes_h3(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = (
            "事象1: 中国系ハッカーが標的攻撃を実施。\n"
            "出典: @TheHackersNews\n"
            "信頼性: 高\n"
            "事象2: TeamPCP がサプライチェーン攻撃。\n"
            "出典: @threatintel\n"
            "信頼性: 高\n"
        )
        out = _format_paragraphs(text)
        # 事象 N が h3 見出しに
        assert "### 事象 1" in out
        assert "### 事象 2" in out
        # 値部分は別行に分離
        assert "中国系ハッカー" in out
        # 属性ラベルも太字化されている
        assert "**出典**: @TheHackersNews" in out
        assert "**信頼性**: 高" in out

    def test_incident_head_without_body_is_just_heading(self) -> None:
        from src.tools.discord_publisher import _format_paragraphs

        text = "事象 1：\n本文の続き"
        out = _format_paragraphs(text)
        assert "### 事象 1" in out
        # 別行 (太字化される or そのまま)
        assert "本文の続き" in out


# ---------- Retry behavior (Phase 5P: 429 / 5xx 自動リトライ) ----------


class _RetryFactory:
    """指定したシーケンスで status_code / text / headers を返す mock factory."""

    def __init__(
        self,
        responses: list[dict[str, Any]],
        instances: list[MagicMock],
    ) -> None:
        self._responses = responses
        self._instances = instances
        self._call_index = 0

    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        inst = MagicMock()
        inst.embeds = []
        inst.add_embed.side_effect = lambda e: inst.embeds.append(e)

        def execute_side_effect() -> MagicMock:
            idx = self._call_index
            self._call_index += 1
            if idx >= len(self._responses):
                raise AssertionError(
                    f"execute called {idx + 1} times but only "
                    f"{len(self._responses)} responses scripted"
                )
            spec = self._responses[idx]
            resp = MagicMock()
            resp.status_code = spec.get("status_code", 204)
            resp.text = spec.get("text", "")
            resp.headers = spec.get("headers", {})
            return resp

        inst.execute.side_effect = execute_side_effect
        self._instances.append(inst)
        return inst


@pytest.fixture
def patched_sleep() -> Iterator[MagicMock]:
    """``asyncio.sleep`` をモックして実待機をスキップ。呼び出し履歴を観察可能。"""
    with patch(
        "src.tools.discord_publisher.asyncio.sleep",
        new=MagicMock(return_value=None),
    ) as mock_sleep:
        # awaitable にする
        async def _async_noop(*args: Any, **kwargs: Any) -> None:
            mock_sleep(*args, **kwargs)

        with patch("src.tools.discord_publisher.asyncio.sleep", side_effect=_async_noop):
            yield mock_sleep


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_429_with_retry_after_header_seconds(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        factory = _RetryFactory(
            [
                {"status_code": 429, "headers": {"Retry-After": "2"}, "text": "{}"},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        assert instances[0].execute.call_count == 2
        sleep_calls = [c.args[0] for c in patched_sleep.call_args_list if c.args]
        # 429 retry で 2 秒の wait が含まれていること (Retry-After header)
        assert any(2.0 <= s <= 2.5 for s in sleep_calls)

    @pytest.mark.asyncio
    async def test_retries_on_429_with_body_retry_after_float(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        body = '{"message": "rate limited", "retry_after": 1.5, "global": false, "code": 40062}'
        factory = _RetryFactory(
            [
                {"status_code": 429, "headers": {}, "text": body},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        assert instances[0].execute.call_count == 2
        sleep_calls = [c.args[0] for c in patched_sleep.call_args_list if c.args]
        assert any(1.5 <= s <= 2.0 for s in sleep_calls)

    @pytest.mark.asyncio
    async def test_retries_on_5xx_with_exponential_backoff_1_2_4(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        factory = _RetryFactory(
            [
                {"status_code": 500, "text": "ISE"},
                {"status_code": 502, "text": "BadGW"},
                {"status_code": 503, "text": "Unavail"},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        assert instances[0].execute.call_count == 4
        sleep_calls = [c.args[0] for c in patched_sleep.call_args_list if c.args]
        # 1, 2, 4 秒の backoff が順に観測されること (rate-limit の min_interval は別)
        # 厳密一致ではなく最低 3 件 (1 / 2 / 4 付近) を確認
        backoffs = sorted([s for s in sleep_calls if s >= 0.9])
        assert len(backoffs) >= 3
        # 1, 2, 4 のいずれもおおむね含まれる
        assert any(0.9 <= s <= 1.3 for s in backoffs)
        assert any(1.9 <= s <= 2.3 for s in backoffs)
        assert any(3.9 <= s <= 4.3 for s in backoffs)

    @pytest.mark.asyncio
    async def test_does_not_retry_on_4xx_other_than_429(self) -> None:
        instances: list[MagicMock] = []
        factory = _RetryFactory(
            [
                {"status_code": 401, "text": "Unauthorized"},
                # 2 個目以降は呼ばれてはいけない
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            with pytest.raises(DiscordPostError, match="401"):
                await pub.post(_make_message())

        assert instances[0].execute.call_count == 1

    @pytest.mark.asyncio
    async def test_max_3_retries_then_raises_with_attempt_count(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        # 4 回連続 500 → 1 回 + 3 リトライ = 4 回試行で全失敗
        factory = _RetryFactory(
            [{"status_code": 500, "text": "ISE"}] * 4,
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            with pytest.raises(DiscordPostError) as exc_info:
                await pub.post(_make_message())

        assert instances[0].execute.call_count == 4
        # メッセージにリトライ回数が含まれること
        assert "4 attempts" in str(exc_info.value) or "4" in str(exc_info.value)
        assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_logs_discord_post_retry_event(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        factory = _RetryFactory(
            [
                {"status_code": 429, "headers": {"Retry-After": "1"}, "text": "{}"},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with (
            patch(
                "src.tools.discord_publisher.DiscordWebhook",
                side_effect=factory,
            ),
            patch("src.tools.discord_publisher._log") as mock_log,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        # discord_post_retry イベントが発火されたこと
        retry_log_calls = [
            call
            for call in mock_log.warning.call_args_list
            if call.args and call.args[0] == "discord_post_retry"
        ]
        assert len(retry_log_calls) >= 1
        # attempt と wait が記録されていること
        kwargs = retry_log_calls[0].kwargs
        assert "attempt" in kwargs
        assert "wait_seconds" in kwargs
        assert "status" in kwargs

    @pytest.mark.asyncio
    async def test_global_429_treated_same_as_per_route_429(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        instances: list[MagicMock] = []
        body = '{"message": "global rate limit", "retry_after": 1.0, "global": true, "code": 40012}'
        factory = _RetryFactory(
            [
                {"status_code": 429, "headers": {}, "text": body},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        assert instances[0].execute.call_count == 2

    @pytest.mark.asyncio
    async def test_429_caps_wait_time_at_max(
        self,
        patched_sleep: MagicMock,
    ) -> None:
        """Discord が retry_after に巨大値 (例: 60s) を返してきても上限でクランプ。"""
        instances: list[MagicMock] = []
        body = '{"message": "rate limited", "retry_after": 60.0, "global": false, "code": 40062}'
        factory = _RetryFactory(
            [
                {"status_code": 429, "headers": {}, "text": body},
                {"status_code": 204, "text": ""},
            ],
            instances,
        )
        with patch(
            "src.tools.discord_publisher.DiscordWebhook",
            side_effect=factory,
        ):
            pub = DiscordPublisher(webhook_url=WEBHOOK_URL)
            await pub.post(_make_message())

        sleep_calls = [c.args[0] for c in patched_sleep.call_args_list if c.args]
        # 60s wait はそのまま受け入れず、上限 (例: 10s) でクランプ
        # ただし保守的に「30 秒未満ならOK」とする (ユーザ定義の上限と一致)
        large_waits = [s for s in sleep_calls if s > 0.5]
        for w in large_waits:
            assert w <= 30.0, f"retry wait {w}s exceeds safety cap"
