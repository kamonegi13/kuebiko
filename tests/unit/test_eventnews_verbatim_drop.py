"""逐語関門は **転記した行だけ** を落とす (版ごと捨てない)。

⚠ 2026-09-01 まで、書き直し後も丸写しの行が残ると版を書かずに捨てていた。
原文が日本語の記事は逐語率が構造的に高く (実測 38-62%)、そのため本文が
一度も作られない事象が出ていた。残りの行は自分の言葉で書かれた要約なので、
1 行のために記事全体を捨てる理由はない。**全部落ちたときだけ**版を書かない。
"""

from __future__ import annotations

from src.eventnews.models import EventNewsDraft, FactItem, GateResult
from src.eventnews.runner import _drop_transcribed_lines

_SOURCE = (
    "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
    "一時的に侵入者から閲覧可能な状態にあったことが判明しました。"
    "対象は氏名、住所、電話番号を含むデータ七万件です。"
)
_TRANSCRIBED = (
    "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
    "一時的に侵入者から閲覧可能な状態にあったことが判明したと報じられている。"
)
_PARAPHRASE = (
    "同社によれば、侵入を受けたサーバー上で取引先や利用者の登録情報が"
    "第三者の目に触れうる状況が一定期間続いていたという。"
)


def _gate(*texts: str) -> GateResult:
    facts = [FactItem(text=t, source_index=1, paragraph=1, section="what") for t in texts]
    draft = EventNewsDraft(headline="見出し", bluf="要旨", facts=facts)
    return GateResult(
        draft=draft, dropped_lines=0, repaired_ids=0, substituted_ids=0, verified=True
    )


def test_only_the_transcribed_line_is_dropped() -> None:
    # Arrange — 転記 1 行 + 自分の言葉 1 行
    gate = _gate(_TRANSCRIBED, _PARAPHRASE)

    # Act
    out = _drop_transcribed_lines(gate, {1: _SOURCE}, "ev-test")

    # Assert — 転記だけ消え、要約は残る
    assert [f.text for f in out.draft.facts] == [_PARAPHRASE]
    assert out.dropped_lines == 1


def test_clean_draft_is_untouched() -> None:
    # Arrange
    gate = _gate(_PARAPHRASE)

    # Act / Assert — 落とす行が無ければ同じものを返す
    assert _drop_transcribed_lines(gate, {1: _SOURCE}, "ev-test") is gate


def test_everything_transcribed_leaves_nothing_to_publish() -> None:
    """全行が転記なら空になる → 呼び手が版を書かない (空の本文は出せない)。"""
    # Arrange
    gate = _gate(_TRANSCRIBED)

    # Act
    out = _drop_transcribed_lines(gate, {1: _SOURCE}, "ev-test")

    # Assert
    assert out.draft.facts == []
