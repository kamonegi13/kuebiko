"""body_ja_chunks (本文翻訳のチャンク単位キャッシュ) の repo mixin。

長文本文の resumable 翻訳 (2026-08-06) 用。翻訳器 (src/cti/body_translator.py の
``translate_body_resumable``) はチャンクを 1 つ訳すごとにここへ確定保存し、
LLM 失敗・時間予算切れの後は未訳チャンクだけを続きから処理する。
全チャンク完了で呼び出し側が articles.body_ja へ連結保存し、本表の行を削除する
(= 平常時は空。行が残る = 翻訳が途中)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.storage.repo_base import RunHistoryRepositoryBase


class TranslationChunksMixin(RunHistoryRepositoryBase):
    """翻訳チャンクキャッシュの read/write。"""

    def get_body_ja_chunks(self, article_id: str, body_hash: str) -> dict[int, str]:
        """保存済みチャンク (seq → 訳文) を返す。

        ``body_hash`` が保存時と不一致の行が 1 つでもあれば、本文が差し替わった
        (reprocess 等) とみなして残骸を全削除し、空 dict を返す (古い部分訳の
        混入防止)。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, body_hash, text FROM body_ja_chunks WHERE article_id=?",
                (article_id,),
            ).fetchall()
        if rows and any(str(r["body_hash"]) != body_hash for r in rows):
            self.clear_body_ja_chunks(article_id)
            return {}
        return {int(r["seq"]): str(r["text"]) for r in rows}

    def save_body_ja_chunk(
        self, article_id: str, seq: int, total: int, body_hash: str, text: str
    ) -> None:
        """訳し終えたチャンク 1 つを確定保存する (既存 seq は無視 = 冪等)。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO body_ja_chunks"
                " (article_id, seq, total, body_hash, text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (article_id, seq, total, body_hash, text, datetime.now(UTC).isoformat()),
            )

    def clear_body_ja_chunks(self, article_id: str) -> int:
        """1 記事のチャンク行を全削除する (完訳確定時 / 本文差し替え時)。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM body_ja_chunks WHERE article_id=?", (article_id,))
            return int(cur.rowcount or 0)
