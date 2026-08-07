"""概観集計の純ロジック (変化=delta + 内訳) の unit test。

DB 非依存の純関数 (_movers / _region_counts) を検証する。集計 SQL は live smoke で確認。
"""

from __future__ import annotations

from src.ui.services.overview import (
    _build_attention,
    _is_roundup,
    _movers,
    _region_counts,
)


def test_movers_computes_delta_and_sorts_by_current() -> None:
    cur = {"a": 10, "b": 5, "c": 3}
    prior = {"a": 6, "b": 8, "d": 2}
    out = _movers(cur, prior, top=4)
    by = {m["key"]: m for m in out}
    assert by["a"]["current"] == 10 and by["a"]["delta"] == 4  # 上昇
    assert by["b"]["delta"] == -3  # 下降
    assert by["c"]["prior"] == 0 and by["c"]["delta"] == 3  # 新規
    assert by["d"]["current"] == 0 and by["d"]["delta"] == -2  # 消滅 (前期のみ)
    # current 降順
    assert [m["key"] for m in out][:2] == ["a", "b"]


def test_movers_share_normalizes_volume_growth() -> None:
    # 発見1: 総量が倍増しても share が同じなら share_delta_pp は 0 (カバレッジ汚染除去)。
    cur = {"a": 20, "b": 20}  # total 40, a=50%
    prior = {"a": 10, "b": 10}  # total 20, a=50%
    out = _movers(cur, prior, top=2)
    by = {m["key"]: m for m in out}
    assert by["a"]["delta"] == 10  # 生 delta は +10 (誤誘導しうる)
    assert by["a"]["share_pct"] == 50.0
    assert by["a"]["share_delta_pp"] == 0.0  # 比重は不変 → 真のシフトなし


def test_movers_share_detects_real_shift() -> None:
    # 比重が実際に動いた場合は share_delta_pp が符号を持つ。
    cur = {"a": 30, "b": 10}  # a=75%
    prior = {"a": 10, "b": 10}  # a=50%
    out = _movers(cur, prior, top=2)
    by = {m["key"]: m for m in out}
    assert by["a"]["share_delta_pp"] == 25.0
    assert by["b"]["share_delta_pp"] == -25.0


def test_movers_share_zero_total_is_safe() -> None:
    out = _movers({}, {}, top=3)
    assert out == []
    out2 = _movers({"a": 0}, {}, top=1)
    assert out2[0]["share_pct"] == 0.0  # ゼロ除算しない


def test_is_roundup_detects_digest_titles() -> None:
    assert _is_roundup("今週の気になるセキュリティニュース - Issue #279")
    assert _is_roundup("Weekly Threat Roundup")
    assert _is_roundup("Security news digest")
    # 通常のアドバイザリ title は roundup でない
    assert not _is_roundup("Cisco Catalyst SD-WAN Manager のゼロデイ脆弱性")
    assert not _is_roundup("LockBit ransomware targets healthcare")


def test_build_attention_priority_and_cap() -> None:
    # 新規KEV > 新規アクター > 敵対spike > 日本増。 ≤5 件。
    notable = [
        {"tag": "new", "canonical": "NewActor", "nation": "中国"},
        {"tag": "spike", "canonical": "APT28", "nation": "ロシア"},
        {"tag": "spike", "canonical": "Unknown", "nation": ""},  # 国籍なしは spike 採用外
    ]
    out = _build_attention(["CVE-2026-0001", "CVE-2026-0002"], notable, jp_delta=5)
    kinds = [i["kind"] for i in out]
    assert kinds[0] == "kev" and kinds[1] == "kev"  # KEV が先頭
    assert "new_actor" in kinds
    assert "spike" in kinds  # 国籍ありの spike のみ
    assert kinds.count("spike") == 1  # 国籍なしは除外
    assert "japan" in kinds
    assert len(out) <= 5


def test_build_attention_no_japan_when_flat() -> None:
    out = _build_attention([], [], jp_delta=0)
    assert out == []  # 何もなければ空 (空状態を優雅に)


def test_movers_top_limits() -> None:
    cur = {k: i for i, k in enumerate("abcdefgh")}
    out = _movers(cur, {}, top=3)
    assert len(out) == 3
    assert out[0]["current"] >= out[1]["current"] >= out[2]["current"]


def test_movers_label_of_applied() -> None:
    out = _movers({"x": 3}, {}, label_of=lambda k: f"L-{k}")
    assert out[0]["label"] == "L-x"
    # label_of なしは key そのまま
    assert _movers({"y": 1}, {})[0]["label"] == "y"


def test_region_counts_aggregates_countries() -> None:
    counts = {"JP": 5, "CN": 3, "GB": 2, "ZZ": 9}  # ZZ は未知 → 無視
    out = _region_counts(counts)
    assert out["east_asia"] == 8  # JP + CN
    assert out["europe"] == 2
    assert "ZZ" not in out
    assert set(out) == {"east_asia", "europe"}
