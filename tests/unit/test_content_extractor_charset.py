"""ヘッダに charset が無い Shift_JIS 応答の文字化け対策 (2026-09-05)。

httpx はヘッダ不在時に UTF-8 を仮定するため、meta タグでのみ charset を宣言する
サイト (@IT / ITmedia) の本文が U+FFFD へ**不可逆に**化けて保存されていた
(実測 28,378 件中 241 件・ほぼ 2 feed に集中)。

3 段で固定する:
1. 判定器そのもの (Shift_JIS バイト列を正しく復号できるか)
2. **配線** — 内部構築されるクライアントに判定器が渡っているか。
   テストの多くはクライアントを注入するため、ここを検査しないと修正が外れても気付けない
3. 貫通 — charset 無しヘッダ + Shift_JIS 本文で extract して化けないこと
"""

from __future__ import annotations

import httpx
import pytest

from src.tools.content_extractor import ContentExtractor, _detect_charset

# 200 字超 (min_content_length) の日本語本文。
_JA_PARAGRAPH = (
    "ニチレイグループがサイバー攻撃を受け、冷凍食品の出荷や冷蔵倉庫の業務が停止した。"
    "被害はグループのシステム遮断に伴うもので、外食チェーンへの食材配送にも遅延が生じた。"
    "同社は復旧までの間、手作業による出荷対応を継続すると説明している。"
    "セキュリティ企業の分析によれば、初期侵入の経路は現時点で公表されていない。"
    "復旧の目処や影響範囲の詳細は、追って発表される見通しである。"
)


def _sjis_article() -> bytes:
    html = (
        "<html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">'
        "<title>ニチレイへのサイバー攻撃</title></head><body><article>"
        + "".join(f"<p>{_JA_PARAGRAPH}</p>" for _ in range(3))
        + "</article></body></html>"
    )
    return html.encode("shift_jis")


def test_detect_charset_decodes_shift_jis_without_mojibake() -> None:
    raw = _sjis_article()

    encoding = _detect_charset(raw)
    decoded = raw.decode(encoding)

    assert "�" not in decoded
    assert "ニチレイ" in decoded


def test_detect_charset_falls_back_to_utf8_on_garbage() -> None:
    # 判定不能でも取得自体を落とさない (挙動保存の fallback)
    assert isinstance(_detect_charset(b"\x00\x01\x02"), str)


@pytest.mark.asyncio
async def test_internal_client_is_wired_with_detector() -> None:
    """クライアント注入型のテストでは検出できない配線を直接検査する。"""
    extractor = ContentExtractor()
    try:
        assert extractor._client._default_encoding is _detect_charset  # noqa: SLF001
    finally:
        await extractor._client.aclose()  # noqa: SLF001


@pytest.mark.asyncio
async def test_extracts_shift_jis_body_without_replacement_chars() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        # ヘッダは charset 無し (実サイト @IT と同じ形)
        return httpx.Response(200, content=_sjis_article(), headers={"content-type": "text/html"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "TestUA/1.0"},
        default_encoding=_detect_charset,
    )
    extractor = ContentExtractor(min_content_length=200, user_agent="TestUA/1.0", client=client)
    result = await extractor.extract("https://example.com/sjis-article")

    assert result.text is not None
    assert "�" not in result.text
    assert "ニチレイ" in result.text
