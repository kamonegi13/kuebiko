"""claim の重複照合に埋込の確認を課す (2026-09-22)。

⚠⚠ **代償の向きが一覧の絞りと逆**。誤って繋ぐと、本来開設すべき追跡が既存の情勢へ
吸収されて**消える**。繋がなければ新規開設されるだけで後から統合もできる。よって
**精度を優先**する (一覧の絞りは取りこぼしが二重開設を招くので回収を優先した)。

実測 (Opus ラベル 422 件・同一事象 39 件、claim は先頭 50 字のみという不利な条件):

| 方式 | 同一を繋ぐ | 別事象も繋ぐ | 繋いだ中の正解率 |
|---|---|---|---|
| 現行規則 (キーの重なり) | 34/39 | 213/383 (56%) | **14%** |
| + 埋込 0.65 の確認 | 22/39 | 7/383 (2%) | **76%** |

誤った吸収が 213 → 7 件 (97% 減)。繋がなくなった 12 件は新規開設されるので失われない。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.assessment.claim_dup import CLAIM_DUP_THRESHOLD, confirm_by_embedding


def _v(x: float, y: float) -> np.ndarray:
    v = np.array([x, y], dtype=np.float32)
    out: np.ndarray = v / np.linalg.norm(v)
    return out


class TestThreshold:
    def test_threshold_favours_precision(self) -> None:
        # 0.60 は正解率 57%、0.70 は同一の回収が 51% まで落ちる
        assert CLAIM_DUP_THRESHOLD == 0.65


class TestConfirmByEmbedding:
    def test_close_claim_is_confirmed(self) -> None:
        assert confirm_by_embedding(claim_vec=_v(1, 0), situation_vec=_v(1, 0.05)) is True

    def test_distant_claim_is_rejected(self) -> None:
        assert confirm_by_embedding(claim_vec=_v(1, 0), situation_vec=_v(0, 1)) is False

    def test_missing_vector_falls_back_to_confirming(self) -> None:
        """⚠ 埋込が作れないときは **従来どおり繋ぐ** — 関門の失敗で挙動を変えない。
        (繋がない側へ倒すと、埋込の障害が静かに重複開設を量産する)"""
        assert confirm_by_embedding(claim_vec=None, situation_vec=_v(1, 0)) is True
        assert confirm_by_embedding(claim_vec=_v(1, 0), situation_vec=None) is True


class TestProductionWiring:
    """本番 (stateful) の配線 — キーで繋いだ組を埋込で確認する。"""

    def test_distant_claim_is_not_absorbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        from src.assessment import stateful

        async def _vecs(texts: list[str]) -> list[object]:
            table = {"新しい事案": _v(1, 0), "無関係の情勢": _v(0, 1)}
            return [table.get(t) for t in texts]

        monkeypatch.setattr(stateful, "_embed_texts", _vecs)

        assert asyncio.run(stateful._claim_dup_confirmed("新しい事案", "無関係の情勢")) is False

    def test_embedding_failure_keeps_the_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠ 確認できないときは繋ぐ — 関門の失敗で重複開設を量産しない。"""
        import asyncio

        from src.assessment import stateful

        async def _boom(_texts: list[str]) -> list[object]:
            raise RuntimeError("embed down")

        monkeypatch.setattr(stateful, "_embed_texts", _boom)

        assert asyncio.run(stateful._claim_dup_confirmed("a", "b")) is True
