"""Unit tests for src.grok.jsonl_parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.grok.jsonl_parser import (
    TweetEngagement,
    TweetRecord,
    detect_grok_task_from_records,
    filter_records,
    is_jsonl_output,
    parse_jsonl,
)


def _make_jsonl_line(
    tweet_id: str = "1",
    theme: str = "B",
    text: str = "test tweet",
    posted_at: str | None = None,
    like: int = 10,
    retweet: int = 5,
) -> str:
    """テスト用の 1 JSONL line を作る。"""
    if posted_at is None:
        posted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return (
        f'{{"tweet_id":"{tweet_id}","url":"https://x.com/test/status/{tweet_id}",'
        f'"author_handle":"@test","author_name":"Test","posted_at":"{posted_at}",'
        f'"lang":"en","text":"{text}","is_retweet":false,"retweeted_tweet_id":null,'
        f'"is_quote":false,"quoted_tweet_id":null,"quoted_text":null,'
        f'"reply_to_tweet_id":null,"media_urls":[],"external_urls":[],'
        f'"engagement":{{"like":{like},"retweet":{retweet},"quote":0,"reply":0}},'
        f'"matched_theme":"{theme}"}}'
    )


class TestIsJsonlOutput:
    def test_detects_jsonl_starting_with_brace(self) -> None:
        body = _make_jsonl_line()
        assert is_jsonl_output(body) is True

    def test_detects_jsonl_wrapped_in_code_block(self) -> None:
        body = f"```json\n{_make_jsonl_line()}\n```"
        assert is_jsonl_output(body) is True

    def test_rejects_markdown_narrative(self) -> None:
        body = "# 全体サマリー\n\n中国系 APT の動向..."
        assert is_jsonl_output(body) is False

    def test_rejects_empty(self) -> None:
        assert is_jsonl_output("") is False
        assert is_jsonl_output("   \n  ") is False

    def test_detects_jsonl_with_grok_ui_prefix(self) -> None:
        """Grok web UI が code block 前に挿入する 'JSON' / 'コピー' label 対応。"""
        body = "JSON\nコピー\n" + _make_jsonl_line(theme="J2")
        assert is_jsonl_output(body) is True

    def test_detects_jsonl_with_english_copy_button(self) -> None:
        """英語 UI で 'Copy' button text が入る場合の対応。"""
        body = "json\nCopy\n" + _make_jsonl_line(theme="B")
        assert is_jsonl_output(body) is True


class TestParseJsonl:
    def test_parses_single_record(self) -> None:
        body = _make_jsonl_line(tweet_id="100", theme="A")
        result = parse_jsonl(body)
        assert result.parsed_count == 1
        assert result.records[0].tweet_id == "100"
        assert result.records[0].matched_theme == "A"

    def test_parses_multiple_records(self) -> None:
        body = "\n".join(
            [
                _make_jsonl_line(tweet_id="1", theme="A"),
                _make_jsonl_line(tweet_id="2", theme="B"),
                _make_jsonl_line(tweet_id="3", theme="F"),
            ],
        )
        result = parse_jsonl(body)
        assert result.parsed_count == 3
        themes = [r.matched_theme for r in result.records]
        assert themes == ["A", "B", "F"]

    def test_strips_code_block_wrap(self) -> None:
        body = f"```json\n{_make_jsonl_line(tweet_id='42')}\n```"
        result = parse_jsonl(body)
        assert result.parsed_count == 1
        assert result.records[0].tweet_id == "42"

    def test_skips_invalid_json(self) -> None:
        body = "\n".join(
            [
                _make_jsonl_line(tweet_id="1"),
                "this is not json",
                _make_jsonl_line(tweet_id="2"),
            ],
        )
        result = parse_jsonl(body)
        assert result.parsed_count == 2
        assert len(result.skipped_lines) == 1

    def test_skips_record_missing_required_field(self) -> None:
        # tweet_id 欠落 → ValidationError
        body = (
            '{"url":"https://x.com/test/status/1","author_handle":"@test",'
            '"posted_at":"2026-05-25T00:00:00Z","text":"x","matched_theme":"B"}'
        )
        result = parse_jsonl(body)
        assert result.parsed_count == 0
        assert len(result.skipped_lines) == 1


class TestFilterRecords:
    def test_drops_record_older_than_24h(self) -> None:
        now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
        old_ts = (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        fresh_ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

        result = parse_jsonl(
            "\n".join(
                [
                    _make_jsonl_line(tweet_id="old", posted_at=old_ts),
                    _make_jsonl_line(tweet_id="fresh", posted_at=fresh_ts),
                ],
            ),
        )
        filtered = filter_records(result.records, now=now)
        assert len(filtered) == 1
        assert filtered[0].tweet_id == "fresh"

    def test_engagement_floor_drops_low_for_non_theme_b(self) -> None:
        # theme F は >= 3 必須、like+RT=2 は drop
        body = _make_jsonl_line(tweet_id="1", theme="F", like=2, retweet=0)
        result = parse_jsonl(body)
        filtered = filter_records(
            result.records,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert len(filtered) == 0

    def test_engagement_floor_theme_b_accepts_score_1(self) -> None:
        # theme B は >= 1 で OK
        body = _make_jsonl_line(tweet_id="1", theme="B", like=1, retweet=0)
        result = parse_jsonl(body)
        filtered = filter_records(
            result.records,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert len(filtered) == 1

    def test_engagement_floor_theme_b_drops_zero(self) -> None:
        # theme B でも 0/0 は drop
        body = _make_jsonl_line(tweet_id="1", theme="B", like=0, retweet=0)
        result = parse_jsonl(body)
        filtered = filter_records(
            result.records,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        assert len(filtered) == 0


class TestDetectGrokTask:
    def test_detects_slot1_when_a_to_f_dominates(self) -> None:
        records = [
            TweetRecord(
                tweet_id=str(i),
                url=f"https://x.com/test/status/{i}",
                author_handle="@x",
                posted_at="2026-05-25T00:00:00Z",
                text="x",
                matched_theme=t,
            )
            for i, t in enumerate(["A", "B", "C"])
        ]
        assert detect_grok_task_from_records(records) == "slot1"

    def test_detects_slot2_when_j1_to_j6_dominates(self) -> None:
        records = [
            TweetRecord(
                tweet_id=str(i),
                url=f"https://x.com/test/status/{i}",
                author_handle="@x",
                posted_at="2026-05-25T00:00:00Z",
                text="x",
                matched_theme=t,
            )
            for i, t in enumerate(["J1", "J2", "J3"])
        ]
        assert detect_grok_task_from_records(records) == "slot2"

    def test_unknown_when_empty(self) -> None:
        assert detect_grok_task_from_records([]) == "unknown"


class TestTweetRecord:
    def test_posted_at_dt_parses_z_suffix(self) -> None:
        r = TweetRecord(
            tweet_id="1",
            url="https://x.com/test/status/1",
            author_handle="@x",
            posted_at="2026-05-25T03:42:00Z",
            text="x",
            matched_theme="A",
        )
        dt = r.posted_at_dt
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 5

    def test_posted_at_dt_returns_none_on_invalid(self) -> None:
        r = TweetRecord(
            tweet_id="1",
            url="https://x.com/test/status/1",
            author_handle="@x",
            posted_at="invalid date",
            text="x",
            matched_theme="A",
        )
        assert r.posted_at_dt is None


class TestTweetEngagement:
    def test_signal_score_sums_like_and_retweet(self) -> None:
        e = TweetEngagement(like=10, retweet=5, quote=3, reply=2)
        assert e.signal_score == 15

    def test_signal_score_zero_when_empty(self) -> None:
        assert TweetEngagement().signal_score == 0


class TestHourlyAdditions:
    """hourly 運用対応 (2026-08-15): no_events ハートビート + account_class バイパス。"""

    def test_heartbeat_line_recognized_not_counted_as_failure(self) -> None:
        # 事象ゼロの窓で「静穏」と「タスク死」を区別するための 1 行 (追加ルール①)
        body = '{"status":"no_events","window_minutes":90}'
        result = parse_jsonl(body)
        assert result.records == []
        assert result.heartbeat_count == 1
        assert result.skipped_lines == []

    def test_heartbeat_mixed_with_records(self) -> None:
        body = _make_jsonl_line() + "\n" + '{"status":"no_events","window_minutes":90}'
        result = parse_jsonl(body)
        assert len(result.records) == 1
        assert result.heartbeat_count == 1

    def test_account_class_parsed_and_defaults_empty(self) -> None:
        line = _make_jsonl_line().replace(
            '"matched_theme"', '"account_class":"vendor_official","matched_theme"'
        )
        rec = parse_jsonl(line).records[0]
        assert rec.account_class == "vendor_official"
        assert parse_jsonl(_make_jsonl_line()).records[0].account_class == ""

    def _record(self, *, account_class: str, like: int = 0) -> TweetRecord:
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return TweetRecord(
            tweet_id="42",
            url="https://x.com/t/status/42",
            author_handle="@t",
            posted_at=now_iso,
            text="fresh signal",
            engagement=TweetEngagement(like=like, retweet=0),
            matched_theme="F",
            account_class=account_class,
        )

    def test_trusted_account_bypasses_engagement_floor(self) -> None:
        # hourly では投稿直後で拡散が未蓄積 → 信頼種別は engagement 0 でも通す (追加ルール③)
        for cls in ("vendor_official", "gov_official", "analyst_known", "affected_party"):
            out = filter_records([self._record(account_class=cls)])
            assert len(out) == 1, f"{cls} は engagement floor をバイパスするべき"

    def test_untrusted_account_still_subject_to_floor(self) -> None:
        for cls in ("aggregator", "other", ""):
            out = filter_records([self._record(account_class=cls)])
            assert out == [], f"{cls} は従来どおり engagement floor で落ちるべき"

    def test_trusted_bypass_does_not_skip_age_window(self) -> None:
        # バイパスは engagement のみ — 24h 窓は信頼種別でも適用される
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
        rec = TweetRecord(
            tweet_id="43",
            url="https://x.com/t/status/43",
            author_handle="@t",
            posted_at=old,
            text="stale",
            engagement=TweetEngagement(like=0, retweet=0),
            matched_theme="F",
            account_class="vendor_official",
        )
        assert filter_records([rec]) == []

    def test_heartbeat_only_body_detected_for_quiet_classification(self) -> None:
        # orchestrator が「静穏 (row 不生成)」と「本当の失敗 (extract_failed)」を
        # body 再パースで判別する経路の回帰固定
        quiet = parse_jsonl('{"status":"no_events","window_minutes":90}')
        assert quiet.heartbeat_count > 0 and quiet.records == []
        failed = parse_jsonl("")  # 完全な空出力 = ハートビート無し → 障害扱いのまま
        assert failed.heartbeat_count == 0
