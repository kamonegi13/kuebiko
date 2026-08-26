"""公開サイトを静的ファイルへ書き出す (Cloudflare Pages 配信用)。

読者を運用者の PC に依存させないための書き出し。**公開 API と同じ関数を呼ぶ** —
静的側に別実装を作ると、片方だけ直したときに公開面の契約 (出典必須・原文を
返さない・high のみ) が静かに破れる。

出力:
    index.json        一覧 (全件。検索・絞り込み・ページングはブラウザ側で行う)
    news/<id>.json    記事ごとの詳細
    map.json          被害国の分布
    vocabularies.json 表示ラベル (公開面が使う語彙のみ)
    meta.json         生成時刻・件数・内容ハッシュ (変化が無ければ配信しない判断に使う)

⚠ **原文と日本語訳は書き出さない**。公開 API がそもそも返さない (public_news.py の
契約) が、静的化で経路が増えるため、書き出し後に検査もする。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.encoders import jsonable_encoder  # noqa: E402

from src.ui.api.public_news import (  # noqa: E402
    FEATURED_COUNT,
    PUBLIC_CATEGORIES,
    get_public_map,
    get_public_news,
    list_public_news,
)

#: 一覧 API の 1 ページ上限 (公開 API と同じ値を使い、全件をページングで集める)
_PAGE = 60
#: 一覧に載せる要約の長さ。表示は line-clamp で 2-3 行までなので、全文は要らない
#: (全文を載せると index.json が 1.3 MB になり初回読み込みが重い)。
_TEASER_CHARS = 160
#: 一覧が実際に使うフィールド。citations (360 KB) は詳細側にあれば足りる。
_INDEX_FIELDS = ("id", "headline", "category", "published_at")
#: 書き出しに入ってはいけないキー (原文・翻訳)。万一の経路増加に対する検査。
_FORBIDDEN_KEYS = ("body", "body_ja")


def _ids(page: dict[str, Any]) -> set[str]:
    """1 ページ分の id 集合。**全件必要な絞り込みはページングして集める**。"""
    found = {str(item["id"]) for item in page["items"]}
    return found


def _all_ids(**query: Any) -> set[str]:
    found: set[str] = set()
    offset = 0
    while True:
        page = list_public_news(limit=_PAGE, offset=offset, **query)
        batch = _ids(page)
        if not batch:
            return found
        found |= batch
        offset += _PAGE


def _fetch_all_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = list_public_news(limit=_PAGE, offset=offset)
        batch = page["items"]
        if not batch:
            return items
        items.extend(batch)
        offset += _PAGE


def _assert_no_publisher_body(payload: Any, where: str) -> None:
    """出版社の本文が混ざっていないこと (§9 の再配布境界)。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _FORBIDDEN_KEYS:
                raise SystemExit(f"書き出しに原文が含まれています: {where}.{key}")
            _assert_no_publisher_body(value, f"{where}.{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            _assert_no_publisher_body(value, f"{where}[{i}]")


def _write(path: Path, payload: Any) -> int:
    """FastAPI と同じエンコーダを通す — 静的ファイルと API の出力を一致させる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


def export(out_dir: Path) -> dict[str, Any]:
    items = _fetch_all_items()
    _assert_no_publisher_body(items, "index")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    # ⚠ **絞り込みの所属は API に決めさせる**。カテゴリ絞り込みは構成記事の分類で
    # 判定される一方、一覧に出る `category` は代表値 (多数決) なので、静的側で
    # `item.category == key` と実装すると結果がずれる。国も同様。
    by_category = {key: _all_ids(category=key) for key in PUBLIC_CATEGORIES}
    countries = sorted(
        {node["iso"] for node in get_public_map().get("nodes", []) if node.get("iso")}
    )
    by_country = {iso: _all_ids(country=iso) for iso in countries}
    featured = sorted(_ids(list_public_news(limit=FEATURED_COUNT, featured=True)))

    lean = [
        {
            **{k: item.get(k) for k in _INDEX_FIELDS},
            "summary": (item.get("summary") or "")[:_TEASER_CHARS],
            "cats": [key for key, ids in by_category.items() if item["id"] in ids],
            "countries": [iso for iso, ids in by_country.items() if item["id"] in ids],
        }
        for item in items
    ]
    total = _write(
        out_dir / "index.json",
        {"items": lean, "categories": list(PUBLIC_CATEGORIES), "featured": featured},
    )

    # 検索は生成本文の全体が対象 (repo.search_event_versions と同じ範囲)。一覧に
    # 全文を載せると重いので別ファイルにし、**最初に検索したときだけ**読ませる。
    searchable: dict[str, str] = {}
    details = 0
    for item in items:
        detail = get_public_news(item["id"])
        _assert_no_publisher_body(detail, f"news/{item['id']}")
        total += _write(out_dir / "news" / f"{item['id']}.json", detail)
        details += 1
        parts = [
            detail.get("headline", ""),
            detail.get("bluf", ""),
            *detail.get("key_points", []),
            *[f.get("text", "") for f in detail.get("facts", [])],
            *[f.get("text", "") for f in detail.get("discrepancies", [])],
            *detail.get("unknowns", []),
        ]
        searchable[item["id"]] = " ".join(p for p in parts if p).lower()
    total += _write(out_dir / "search.json", searchable)

    total += _write(out_dir / "map.json", get_public_map())

    # 内容ハッシュ: 変化が無ければ配信しない判断に使う (無駄なデプロイを打たない)
    digest = hashlib.sha256()
    for path in sorted(out_dir.rglob("*.json")):
        if path.name == "meta.json":
            continue
        digest.update(path.read_bytes())
    meta = {"items": len(items), "details": details, "bytes": total, "sha256": digest.hexdigest()}
    _write(out_dir / "meta.json", meta)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/public_site", help="書き出し先")
    parser.add_argument(
        "--min-items",
        type=int,
        default=1,
        help="これ未満なら失敗させる (0 件を成功として配信しないため)",
    )
    args = parser.parse_args()
    meta = export(Path(args.out))
    # ⚠ **0 件を成功にしない**。DB に繋がらない環境で実行すると SQLite の空 DB に
    # フォールバックし、書き出しは「成功」して**空のサイトが配信される**
    # (2026-08-26 に実際に配信した)。件数で落とすのが唯一の防波堤。
    if meta["items"] < args.min_items:
        raise SystemExit(
            f"書き出しが {meta['items']} 件しかありません (下限 {args.min_items})。"
            " DB に繋がっているか確認してください"
        )
    print(
        f"書き出し: 記事 {meta['items']} 件 / 詳細 {meta['details']} 件 / "
        f"{meta['bytes'] / 1024:.0f} KB / sha256 {meta['sha256'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
