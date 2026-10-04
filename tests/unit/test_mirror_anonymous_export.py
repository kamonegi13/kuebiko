"""写しの匿名公開化 (2026-10-04) に伴う関門のテスト。

書き出す対象が常時匿名公開になったことで、本文・個人メモ・レビューキュー等を
**無条件**に除くことが要件になった。ここでは:
    - 本文 (body/body_ja) の再帰的な除去 (事象ニュースの入れ子な構成記事も含む)
    - 除外エンドポイントが書き出されていないことを `_final_gate` が検出する
    - channels / runtime-flags のスタブが期待する形であること
を確認する。credential 判定自体 (_assert_no_credentials) は
test_mirror_credential_gate.py が別に持つ。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from export_mirror import (  # noqa: E402
    _CHANNELS_STUB,
    _EXCLUDED_ENDPOINTS,
    _FORBIDDEN_BODY_KEYS,
    _RUNTIME_FLAGS_STUB,
    _assert_no_forbidden_keys,
    _final_gate,
    _safe_name,
    _strip_forbidden,
    _write,
)


class TestStripForbidden:
    """本文等の再帰的な除去 (_strip_forbidden) の振る舞い。"""

    def test_removes_body_from_flat_article(self) -> None:
        # Arrange
        article = {"article_id": "a1", "title": "見出し", "body": "本文テキスト", "body_ja": "和訳"}

        # Act
        stripped = _strip_forbidden(article)

        # Assert
        assert "body" not in stripped
        assert "body_ja" not in stripped
        assert stripped["title"] == "見出し"

    def test_removes_body_from_nested_eventnews_members(self) -> None:
        # Arrange — eventnews detail は構成記事を members に入れ子で持つ
        detail = {
            "id": "ev-1",
            "members": [
                {"article_id": "a1", "title": "T1", "body": "本文1", "summary": "要約1"},
                {"article_id": "a2", "title": "T2", "body_ja": "本文2日本語", "summary": "要約2"},
            ],
        }

        # Act
        stripped = _strip_forbidden(detail)

        # Assert
        for member in stripped["members"]:
            assert "body" not in member
            assert "body_ja" not in member
        assert stripped["members"][0]["summary"] == "要約1"

    def test_removes_body_arbitrarily_deep(self) -> None:
        # Arrange — 入れ子の深さに関わらず落ちることを確認する
        deeply_nested = {"a": {"b": {"c": [{"d": {"body": "deep"}}]}}}

        # Act
        stripped = _strip_forbidden(deeply_nested)

        # Assert
        assert "body" not in stripped["a"]["b"]["c"][0]["d"]

    def test_leaves_non_forbidden_keys_untouched(self) -> None:
        # Arrange
        payload = {"title": "記事", "summary": "要約", "importance": "high"}

        # Act
        stripped = _strip_forbidden(payload)

        # Assert — 新しいオブジェクトだが内容は不変 (immutable な変換であることの確認)
        assert stripped == payload
        assert stripped is not payload


class TestAssertNoForbiddenKeys:
    """`_assert_no_forbidden_keys` は取り残しを検出して書き出しを止める。"""

    @pytest.mark.parametrize("key", list(_FORBIDDEN_BODY_KEYS))
    def test_raises_when_forbidden_key_present(self, key: str) -> None:
        with pytest.raises(SystemExit):
            _assert_no_forbidden_keys({key: "漏れた値"}, "t")

    def test_raises_when_forbidden_key_nested_in_list(self) -> None:
        with pytest.raises(SystemExit):
            _assert_no_forbidden_keys([{"ok": 1}, {"body": "漏れた本文"}], "t")

    def test_allows_payload_without_forbidden_keys(self) -> None:
        # 例外が出なければ成功
        _assert_no_forbidden_keys({"title": "T", "summary": "S"}, "t")


class TestWriteStripsBeforeGating:
    """`_write` は書き込み前に本文を落とすので、本文を含む払い出しでも失敗しない。"""

    def test_write_strips_body_and_succeeds(self, tmp_path: Path) -> None:
        # Arrange
        payload = {"article_id": "a1", "title": "T", "body": "本文", "body_ja": "和訳"}

        # Act
        _write(tmp_path / "articles" / "a1.json", payload)

        # Assert
        written = json.loads((tmp_path / "articles" / "a1.json").read_bytes())
        assert "body" not in written
        assert "body_ja" not in written
        assert written["title"] == "T"


class TestFinalGate:
    """`_final_gate` — 全ファイルを最後に見直す fail-closed な関門。"""

    def test_passes_on_clean_output_directory(self, tmp_path: Path) -> None:
        # Arrange
        _write(tmp_path / "articles.json", {"articles": [{"title": "T"}]})

        # Act / Assert — 例外が出なければ成功
        _final_gate(tmp_path)

    def test_fails_when_excluded_endpoint_file_exists(self, tmp_path: Path) -> None:
        # Arrange — _write を迂回して、除外対象のファイルを直接置く
        # (本来 main() はこのファイルを書かないが、関門自体の検出力を確かめる)
        excluded_ep = _EXCLUDED_ENDPOINTS[0]
        path = tmp_path / "api" / f"{_safe_name(excluded_ep)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"leaked": true}')

        # Act / Assert
        with pytest.raises(SystemExit):
            _final_gate(tmp_path)

    def test_fails_when_body_key_slips_through(self, tmp_path: Path) -> None:
        # Arrange — _write を迂回して本文つきファイルを直接置く (取り残しの模擬)
        path = tmp_path / "articles" / "leaked.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"body": "漏れた本文"}).encode())

        # Act / Assert
        with pytest.raises(SystemExit):
            _final_gate(tmp_path)

    def test_fails_when_credential_shaped_value_slips_through(self, tmp_path: Path) -> None:
        # Arrange
        path = tmp_path / "api" / "leaked.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({"note": "sk-ant-api03-AbCdEfGhIjKlMnOp"}).encode())

        # Act / Assert
        with pytest.raises(SystemExit):
            _final_gate(tmp_path)


class TestExcludedEndpointsNeverWritten:
    """個人メモ・購読ソース・Grok・レビューキューは SCREEN_ENDPOINTS に無いこと。"""

    def test_excluded_endpoints_not_in_screen_or_reference_list(self) -> None:
        # Arrange
        from export_mirror import REFERENCE_ENDPOINTS, SCREEN_ENDPOINTS

        # Act
        all_endpoints = set(SCREEN_ENDPOINTS) | set(REFERENCE_ENDPOINTS)

        # Assert
        for excluded in _EXCLUDED_ENDPOINTS:
            assert excluded not in all_endpoints


class TestStubs:
    """channels / runtime-flags は稼働中の値を取らず、同形の空スタブで書く。"""

    def test_runtime_flags_stub_is_all_anonymous(self) -> None:
        assert _RUNTIME_FLAGS_STUB == {
            "read_only": False,
            "authenticated": False,
            "auth_available": False,
            "remote_write": False,
        }

    def test_channels_stub_has_expected_shape_and_no_webhook_data(self) -> None:
        # Arrange / Act
        stub: dict[str, Any] = _CHANNELS_STUB

        # Assert — ChannelsResponse (frontend/src/api/channels.ts) と同形のキーを持つが、
        # webhook_env_key のような運用情報は一切含まれない
        assert set(stub.keys()) == {
            "channels",
            "builtin_ids",
            "rule_refs",
            "webhook_set",
            "webhook_masked",
        }
        assert stub["channels"] == []
        _assert_no_forbidden_keys(stub, "channels-stub")
