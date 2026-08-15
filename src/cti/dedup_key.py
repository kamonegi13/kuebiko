"""同事象クラスタリング用 dedup_key の生成 (Phase 5L-4 + 5L-7)。

``compute_dedup_key``:
    - LLM が prompts/briefing/summarizer.j2 で出力する ``dedup_key`` を一級信号
    - LLM 不在時は CVE-ID 抽出 → article_id ハッシュにフォールバック

旧 Grok markdown 経路の ``compute_grok_section_dedup_key`` は Grok JSONL 化
(独自の dedup_key 生成を持つ) に伴い 2026-06-13 に撤去。
"""

from __future__ import annotations

import re
import unicodedata

# CVE-ID パターン (e.g. CVE-2026-12345 or CVE-2026-1)
_CVE_PATTERN = re.compile(r"\bCVE-(\d{4})-(\d{4,7})\b", re.IGNORECASE)

# dedup_key 内に埋め込まれた CVE 識別子 (例: "cve-2026-0300-palo-alto-rce")
_CVE_IN_KEY_PATTERN = re.compile(r"cve-(\d{4})-(\d{4,7})", re.IGNORECASE)


def extract_cve_id(*, dedup_key: str | None = None, title: str | None = None) -> str | None:
    """Phase 5T-V-2: dedup_key / title から正規化 CVE-ID を抽出。

    返り値は ``cve-YYYY-NNNN`` (lowercase) 固定形式。LLM が生成する
    dedup_key のバリエーション (``cve-2026-0300``, ``cve-2026-0300-palo-alto-rce``)
    を 1 つに揃え、48h 以内の同 CVE 重複投稿を強制 skip する判定に使う。

    Args:
        dedup_key: 既存 dedup_key (LLM 提供 or 機械生成)、ある場合は最優先
        title: BriefingMessage.title (dedup_key に CVE が無い場合の fallback)

    Returns:
        ``cve-YYYY-NNNN`` 形式の文字列、抽出失敗時は None。
    """
    if dedup_key:
        m = _CVE_IN_KEY_PATTERN.search(dedup_key)
        if m:
            return f"cve-{m.group(1)}-{m.group(2)}".lower()
    if title:
        m2 = _CVE_PATTERN.search(title)
        if m2:
            return f"cve-{m2.group(1)}-{m2.group(2)}".lower()
    return None


def compute_dedup_key(
    *,
    llm_key: str,
    title: str,
    body_first_line: str = "",
    article_id_fallback: str = "",
) -> str:
    """LLM 提供 key を最優先、無ければ CVE-ID 抽出 / 一意フォールバック。

    Args:
        llm_key: ``LLMRoutingFlags.dedup_key`` (空文字なら自動生成)
        title: BriefingMessage.title (CVE 抽出用、ASCII slug 用途)
        body_first_line: 抽出本文の先頭 200 文字程度 (CVE-ID 抽出用、任意)
        article_id_fallback: LLM/CVE 抽出が空のとき、article_id ベースの一意 key を
            出すための値 (typically Article.id)。重複クラスタリング自体は LLM /
            CVE が無ければ機能しないが、誤って異なる記事を同 key にしないための保険。

    Returns:
        slug 化された dedup_key (空文字を返すことはない)。
    """
    # 1. LLM 由来 key (既に slug 化済) を最優先
    if llm_key:
        return llm_key.strip()
    # 2. CVE-ID をタイトル + 本文先頭から抽出 (機械的判定で確実)
    full_text = f"{title}\n{body_first_line or ''}"
    cve_match = _CVE_PATTERN.search(full_text)
    if cve_match:
        return f"cve-{cve_match.group(1)}-{cve_match.group(2)}".lower()
    # 3. フォールバック: LLM/CVE が無いときは article_id をベースに一意 key を返す
    #    (誤クラスタリング防止のため title slug 単独は使わない)
    if article_id_fallback:
        # article_id の hash 末尾を suffix にして衝突回避
        import hashlib

        h = hashlib.sha1(article_id_fallback.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        title_slug = _title_to_slug(title, max_length=40) or "article"
        return f"{title_slug}-{h}"
    # 4. 最終手段: タイトル先頭の slug 化 (本来は呼ばれない)
    return _title_to_slug(title, max_length=80)


def _title_to_slug(title: str, *, max_length: int) -> str:
    """タイトルを ASCII 安全な slug に変換する (フォールバック用)。

    日本語の場合は最初の 16 文字 (NFKC 正規化) を返し、別事象との衝突を避ける。
    """
    if not title:
        return ""
    norm = unicodedata.normalize("NFKC", title.strip())
    # ASCII 主体なら lowercase + 空白 → ハイフン
    if norm.isascii():
        slug = re.sub(r"[^a-z0-9]+", "-", norm.lower())
        slug = slug.strip("-")
        return slug[:max_length]
    # 日本語などの非 ASCII: 文字をそのまま使い、空白は除く
    no_space = re.sub(r"\s+", "", norm)
    return no_space[:max_length]
