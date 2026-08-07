"""記事本文のオンデマンド日本語訳 (2026-07-25 記事詳細 UI)。

英語原文のまま保存されている ``articles.body`` を、利用者が記事詳細画面で
「日本語訳」を押した時 + 毎時のバックログ翻訳ジョブで LLM 全訳する。
結果は ``articles.body_ja`` にキャッシュされ、以後は DB から返す。
呼び出し元は ``src/ui/api/article_ops.py`` / ``src/ui/services/body_translate_backlog.py``。

チャンク分割の根拠 (本番実測 2026-07-25): body は p50 ~2.8k / p90 ~9k 字。
段落境界 (空行) を跨がずに詰め、単一段落が上限を超える場合のみ強制分割する。

2026-08-06 resumable 化: 保存本文の上限が 20k → 100k 字
(``src/pipeline/body_limits.py``) に拡大されたため、一括 all-or-nothing では
長文 (最大 20 チャンク) の失敗率と再試行コストが複利で悪化する。
``translate_body_resumable`` はチャンク 1 つ訳すごとに store (body_ja_chunks) へ
確定保存し、失敗・時間切れ後は未訳チャンクだけを続きから処理する。
部分訳を ``body_ja`` として**キャッシュしない**原則は不変 (完訳時のみ確定)。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient, LLMError

_log = get_logger(__name__)

# 1 チャンクの最大文字数。26B (FAST/DIALOG 既定) の 1 呼出で全訳が
# max_tokens 内に収まり、かつ step timeout (300s) に余裕がある規模。
_CHUNK_MAX_CHARS = 5000

# 5k 字の英文チャンク → 日本語全訳 (~4-5k 字) を余裕込みで収める出力上限。
_TRANSLATE_MAX_TOKENS = 6144

BODY_TRANSLATE_SYSTEM_PROMPT = (
    "あなたは CTI (サイバー脅威インテリジェンス) アナリスト向けの英日翻訳者です。"
    "記事本文を段落構成を保ったまま日本語に全訳します。以下のルールを厳守してください:\n"
    "- 要約・省略をせず全文を訳す\n"
    "- 脅威アクター名・マルウェア名・ツール名・作戦名・組織名・製品名・サービス名などの"
    "固有名詞は翻訳せず原文表記のまま残す\n"
    "- MITRE ATT&CK ID (G/T/S 番号)・CVE 番号・IoC (IP/ドメイン/ハッシュ/URL) は"
    "そのまま維持する\n"
    "- 技術用語は定訳があれば日本語 (例: lateral movement → 横展開)、なければ原文のまま\n"
    "- 出力は翻訳文のみとし、前置き・注釈・修飾を一切付けない"
)

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")

# ひらがな/カタカナ。かな存在は日本語テキストの強いシグナル (中国語=漢字のみ /
# 韓国語=ハングル にはかなが無いため、中韓ソースは正しく「要翻訳」側に落ちる)。
_KANA_RE = re.compile(r"[ぁ-んァ-ヶ]")

# 日本語判定のかな比率しきい値。日本語文は通常 20%+ がかな。英文が引用等で
# かなを含んでも 5% には届かない。
_JAPANESE_KANA_RATIO = 0.05


def is_probably_japanese(text: str) -> bool:
    """本文が既に日本語かをかな比率で判定する (日本語原文は翻訳不要)。

    security-next 等の日本語ソース記事を「日本語に翻訳」すると LLM が逆方向
    (日→英) に翻訳して壊れるため、翻訳経路の入口で必ずこの判定を通す。
    """
    stripped = text.strip()
    if not stripped:
        return False
    kana = len(_KANA_RE.findall(stripped))
    return kana / len(stripped) >= _JAPANESE_KANA_RATIO


def split_for_translation(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """本文を段落境界で ``max_chars`` 以下のチャンクへ貪欲に分割する。

    段落 (空行区切り) は跨がずに結合する。単一段落が ``max_chars`` を超える
    場合のみ、行→固定幅の順で強制分割する (全文が失われないことを保証)。
    """
    normalized = text.strip()
    if not normalized:
        return []
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(normalized) if p.strip()]

    chunks: list[str] = []
    buf: list[str] = []
    for para in paragraphs:
        pieces = _split_oversize(para, max_chars) if len(para) > max_chars else [para]
        for piece in pieces:
            if buf and len("\n\n".join([*buf, piece])) > max_chars:
                chunks.append("\n\n".join(buf))
                buf = []
            buf.append(piece)
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _split_oversize(paragraph: str, max_chars: int) -> list[str]:
    """``max_chars`` を超える単一段落を、行境界を優先して強制分割する。"""
    out: list[str] = []
    current = ""
    for line in paragraph.splitlines():
        # 1 行だけで上限を超える場合は固定幅で切る (改行の無い長文対策)
        while len(line) > max_chars:
            if current:
                out.append(current)
                current = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chars:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


class ChunkStore(Protocol):
    """翻訳チャンク永続化の seam (storage 実装は ``TranslationChunksMixin``)。

    cti 層から storage 層への直接依存を避け、テストでは in-memory 実装を使う。
    """

    def get_body_ja_chunks(self, article_id: str, body_hash: str) -> dict[int, str]: ...

    def save_body_ja_chunk(
        self, article_id: str, seq: int, total: int, body_hash: str, text: str
    ) -> None: ...


@dataclass(frozen=True)
class TranslationProgress:
    """resumable 翻訳の結果。``text`` は全チャンク完了時のみ非 None。

    ``partial_text`` は先頭から連続して訳せている部分の連結 (進捗表示用、
    未完了でも読み始められる)。
    """

    text: str | None
    partial_text: str
    done_chunks: int
    total_chunks: int

    @property
    def is_complete(self) -> bool:
        return self.text is not None


def body_hash_for_translation(text: str) -> str:
    """分割元本文の指紋 (SHA-256 先頭 16 桁)。本文差し替えの検知に使う。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


async def _translate_chunk(llm: LLMClient, chunk: str, index: int, total: int) -> str:
    """チャンク 1 つを翻訳する。空応答は ``LLMError``。"""
    resp = await llm.generate(
        prompt=f"次の記事本文を日本語に翻訳してください:\n\n{chunk}",
        system=BODY_TRANSLATE_SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=_TRANSLATE_MAX_TOKENS,
        think=False,  # 翻訳は単純タスク。thinking ブロックで本文圧迫されるのを防ぐ
    )
    out = resp.text.strip()
    if not out:
        raise LLMError(f"翻訳結果が空です (chunk {index + 1}/{total})")
    return out


async def translate_body(llm: LLMClient, text: str) -> str:
    """本文全体を日本語に翻訳して返す (in-memory 一括版)。

    チャンクごとに 1 LLM 呼出。いずれかが失敗 (例外 / 空応答) したら
    ``LLMError`` を送出し、部分訳は返さない (キャッシュ側の不整合防止)。
    永続 store を使う resumable 版は ``translate_body_resumable``。
    """
    chunks = split_for_translation(text)
    if not chunks:
        raise LLMError("翻訳対象の本文が空です")
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        parts.append(await _translate_chunk(llm, chunk, i, len(chunks)))
    _log.info(
        "body_translated",
        chunks=len(chunks),
        input_chars=len(text),
        output_chars=sum(len(p) for p in parts),
        model=llm.model,
    )
    return "\n\n".join(parts)


async def translate_body_resumable(
    llm: LLMClient,
    text: str,
    *,
    article_id: str,
    store: ChunkStore,
    deadline_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TranslationProgress:
    """チャンク単位で確定保存しながら翻訳する (失敗・時間切れから再開可能)。

    - 各チャンクの訳は成功した時点で ``store`` に保存する。途中で LLM が失敗
      (``LLMError`` 送出) しても進捗は残り、次回呼び出しが続きから処理する。
    - ``deadline_seconds`` を超えたら**チャンク境界で**中断し、未完了の
      ``TranslationProgress`` を返す (例外ではない)。毎時ジョブの時間予算と
      HTTP リクエストの応答上限の両方がこの 1 つの仕組みで守られる。
    - 全チャンク完了で連結訳文を返す。``articles.body_ja`` への確定保存と
      チャンク行の削除は**呼び出し側の責務** (store は途中状態のみ持つ)。
    """
    chunks = split_for_translation(text)
    if not chunks:
        raise LLMError("翻訳対象の本文が空です")
    body_hash = body_hash_for_translation(text)
    parts: dict[int, str] = dict(store.get_body_ja_chunks(article_id, body_hash))
    resumed_from = len(parts)
    started = clock()
    for i, chunk in enumerate(chunks):
        if i in parts:
            continue
        if deadline_seconds is not None and clock() - started >= deadline_seconds:
            break
        out = await _translate_chunk(llm, chunk, i, len(chunks))
        store.save_body_ja_chunk(article_id, i, len(chunks), body_hash, out)
        parts[i] = out

    prefix: list[str] = []
    for i in range(len(chunks)):
        if i not in parts:
            break
        prefix.append(parts[i])
    partial_text = "\n\n".join(prefix)

    if len(parts) < len(chunks):
        return TranslationProgress(
            text=None,
            partial_text=partial_text,
            done_chunks=len(parts),
            total_chunks=len(chunks),
        )
    _log.info(
        "body_translated",
        chunks=len(chunks),
        resumed_from=resumed_from,
        input_chars=len(text),
        output_chars=len(partial_text),
        model=llm.model,
    )
    return TranslationProgress(
        text=partial_text,
        partial_text=partial_text,
        done_chunks=len(chunks),
        total_chunks=len(chunks),
    )
