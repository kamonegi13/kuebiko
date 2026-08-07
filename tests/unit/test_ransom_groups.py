"""自動ランサム辞書 (ransom_groups) の unit テスト。"""

from __future__ import annotations

from src.cti.ransom_groups import distinctive_names, find_mention, has_ransom_context


def test_distinctive_drops_short_and_generic() -> None:
    raw = ["qilin", "8base", "rhysida", "play", "nova", "lynx", "medusa", "royal", "cloak"]
    out = distinctive_names(raw)
    # distinctive (>=5 文字 かつ 非 denylist) のみ残る
    assert "qilin" in out
    assert "8base" in out
    assert "rhysida" in out
    # 4 文字 (play/nova/lynx) は最小長で除外
    assert "play" not in out and "nova" not in out and "lynx" not in out
    # 一般語 (medusa/royal/cloak) は denylist で除外
    assert "medusa" not in out and "royal" not in out and "cloak" not in out


def test_find_mention_word_boundary() -> None:
    names = distinctive_names(["qilin", "rhysida", "safepay"])
    # 語境界一致
    assert find_mention("Qilin claims breach of Acme", names) == "qilin"
    assert find_mention("被害組織は Rhysida に攻撃された", names) == "rhysida"
    # 部分一致は拾わない (英数の途中)
    assert find_mention("qilinx is unrelated", names) is None
    # 該当なし
    assert find_mention("LockBit attacked someone", names) is None


def test_find_mention_empty_inputs() -> None:
    assert find_mention("", distinctive_names(["qilin"])) is None
    assert find_mention("Qilin", distinctive_names([])) is None


def test_denylist_drops_generic_corpus_names() -> None:
    # corpus 辞書に出た一般語 (payload/genesis/aurora/underground 等) は除外される
    out = distinctive_names(["payload", "genesis", "aurora", "underground", "knight", "qilin"])
    assert out == frozenset({"qilin"})


def test_has_ransom_context() -> None:
    assert has_ransom_context("Qilin ransomware hit Acme") is True
    assert has_ransom_context("Qilin が被害組織を公開") is True
    assert has_ransom_context("身代金を要求された") is True
    # ランサム文脈語が無ければ False (一般記事でのグループ名誤爆を防ぐ二重防御)
    assert has_ransom_context("Aurora product released a new feature") is False
