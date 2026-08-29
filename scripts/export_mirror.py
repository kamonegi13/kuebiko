"""運用画面 (Tier1) の読み取り面を静的ファイルへ書き出す。

目的は **Mac に到達できないときの継続**。ライブの代わりではなく、
「その時点の写し」を別経路で読めるようにする (2026-08-29 利用者提案)。

⚠ **書き出すのは Tier2 が生成した情報だけ**。操作のための状態は書き出さない:
    - ジョブ計画 / 実行履歴 — 「いま」を知る情報。古い時刻は誤読しか生まない
    - レビューキュー — 古い状態で承認判断はできない
    - 設定 / プロンプト — DB が SSoT。古い写しは編集判断を誤らせる
    - 分析チャット / 翻訳 — LLM 依存 (外へ出せない)
    - モデルティア / 鍵 — 秘密

⭐ **稼働中の API を HTTP で叩いて写す**。同じ関数を import する方式だと、
書き出し側の環境が違ったときに静かに別物を出す (2026-08-26 に公開サイトで、
DATABASE_URL の無いホストで実行して空の SQLite にフォールバックし、
0 件のサイトを「成功」として配信した)。HTTP なら **利用者と同じ経路**を通るので、
環境差がそもそも生まれない。アプリが落ちていれば失敗する = 静かに壊れない。

出力:
    meta.json            生成時刻・件数 (画面が「○○時点の写し」を出すのに使う)
    articles.json        記事一覧 (直近 N 日)
    articles/<id>.json   記事の詳細
    eventnews.json       事象ニュース一覧
    eventnews/<id>.json  事象ニュースの詳細
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

#: 記事の窓。Pages の 1 デプロイ 20,000 ファイル制限に対する余裕を保つ
#: (30 日で約 8,300 件。90 日にすると 28,000 件で超える)。
DEFAULT_DAYS = 30
#: 一覧 API の 1 ページ上限 (API 側の制約と同じ値)
_PAGE = 200
#: これを下回ったら書き出しを失敗させる。**空を配信しないための最後の関門**。
#: 環境を揃えるだけでは再発する (公開サイトで実証済み) ので件数でも守る。
#: 取りすぎの歯止め。Pages は 1 デプロイ 20,000 ファイルまで。
MAX_ARTICLES = 9000
MAX_EVENTS = 4000
DEFAULT_MIN_ARTICLES = 100
DEFAULT_MIN_EVENTS = 50

#: 画面が起動時・描画時に必ず引く小さな参照データ。
#:
#: ⭐ これを写さないと、写しは記事があっても**表示できない**。語彙 (value→日本語ラベル)
#: は起動関門になっていて、解決するまで本体を描かないため、取得が失敗し続けると
#: 読み込み中のまま固まる (2026-08-29 の実障害)。ラベルが無いと生の enum が出るので、
#: 「関門を外して先へ進む」は解にならない。
#:
#: いずれも数十 KB 以下で、絞り込み条件を持たない全体一覧。
REFERENCE_ENDPOINTS = (
    "/api/v1/vocabularies",
    "/api/v1/runtime-flags",
    "/api/v1/channels",
    "/api/v1/feed-options",
    "/api/v1/actor-options",
    "/api/v1/affected-vendors",
    "/api/v1/pir/options",
)


def _get(client: httpx.Client, path: str, **params: Any) -> Any:
    r = client.get(path, params=params or None)
    r.raise_for_status()
    return r.json()


def _write(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(body)
    return len(body)


def _safe_name(article_id: str) -> str:
    """id をファイル名にする。URL をそのまま id にしている経路があるため、
    ディレクトリ区切りや長さの問題を避けてハッシュに落とす。"""
    return hashlib.sha256(article_id.encode()).hexdigest()[:32]


def _fetch_articles(client: httpx.Client, days: int, cap: int) -> list[dict[str, Any]]:
    """記事一覧を窓のぶん全件集める (offset で辿る)。

    ``cap`` は取りすぎの歯止め。Pages の 1 デプロイ 20,000 ファイル制限があるので、
    窓を広げすぎたときに黙って上限へ突っ込まないようにする。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < cap:
        page = _get(
            client,
            "/api/v1/articles",
            limit=_PAGE,
            offset=offset,
            since_hours=days * 24,
            include_summary=True,
        )
        batch = page.get("articles", [])
        if not batch:
            break
        for a in batch:
            aid = str(a.get("article_id") or "")
            if aid and aid not in seen:
                seen.add(aid)
                out.append(a)
        offset += _PAGE
    return out[:cap]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--out", required=True, help="書き出し先ディレクトリ")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-articles", type=int, default=MAX_ARTICLES)
    ap.add_argument("--max-events", type=int, default=MAX_EVENTS)
    ap.add_argument("--min-articles", type=int, default=DEFAULT_MIN_ARTICLES)
    ap.add_argument("--min-events", type=int, default=DEFAULT_MIN_EVENTS)
    ap.add_argument(
        "--with-bodies",
        action="store_true",
        help="記事本文も書き出す (既定は出さない — 45MB あり、"
        "エッジに置く量と機微が増える。出典 URL から原記事に当たれる)",
    )
    args = ap.parse_args()

    out = Path(args.out)
    generated_at = datetime.now(UTC)
    total_bytes = 0

    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        # --- 画面が引く参照データ ---
        # 記事より先に取る。ここが欠けると画面が描けないので、
        # 記事だけ揃った「読めない写し」を作らない。
        for ep in REFERENCE_ENDPOINTS:
            payload = _get(client, ep)
            total_bytes += _write(out / "api" / f"{_safe_name(ep)}.json", payload)

        # --- 記事 ---
        articles = _fetch_articles(client, args.days, args.max_articles)
        if len(articles) < args.min_articles:
            print(
                f"記事が {len(articles)} 件しかない (下限 {args.min_articles})。"
                " 空の写しを配らないため中止する。",
                file=sys.stderr,
            )
            return 1
        total_bytes += _write(out / "articles.json", {"articles": articles, "count": len(articles)})

        for a in articles:
            aid = str(a.get("article_id") or "")
            if not aid:
                continue
            enc = urllib.parse.quote(aid, safe="")
            try:
                detail = _get(client, f"/api/v1/articles/{enc}")
            except httpx.HTTPStatusError:
                continue  # 1 件の欠落で写し全体を止めない
            if not args.with_bodies:
                art = detail.get("article")
                if isinstance(art, dict):
                    # 本文は写さない (既定)。**キーごと消す**のではなく空にする —
                    # 画面が「取得できなかった」と「写していない」を区別できるように、
                    # meta.json の with_bodies と合わせて読ませる。
                    art["body"] = ""
                    art["body_ja"] = ""
            total_bytes += _write(out / "articles" / f"{_safe_name(aid)}.json", detail)

        # --- 事象ニュース (こちらも offset で全件辿る) ---
        events: list[dict[str, Any]] = []
        seen_ev: set[str] = set()
        ev_offset = 0
        while len(events) < args.max_events:
            batch = _get(client, "/api/v1/eventnews", limit=_PAGE, offset=ev_offset).get(
                "items", []
            )
            if not batch:
                break
            for e in batch:
                eid = str(e.get("id") or "")
                if eid and eid not in seen_ev:
                    seen_ev.add(eid)
                    events.append(e)
            ev_offset += _PAGE
        events = events[: args.max_events]
        if len(events) < args.min_events:
            print(
                f"事象ニュースが {len(events)} 件しかない (下限 {args.min_events})。中止する。",
                file=sys.stderr,
            )
            return 1
        total_bytes += _write(out / "eventnews.json", {"items": events})

        for e in events:
            eid = str(e.get("id") or "")
            if not eid:
                continue
            try:
                detail = _get(client, f"/api/v1/eventnews/{urllib.parse.quote(eid, safe='')}")
            except httpx.HTTPStatusError:
                continue
            total_bytes += _write(out / "eventnews" / f"{_safe_name(eid)}.json", detail)

    # --- 写しであることの宣言 ---
    # 画面はこれを読んで「○○時点の写し」を常時出す。無いとライブと見分けが付かない。
    meta = {
        "generated_at": generated_at.isoformat(),
        "kind": "ops-mirror",
        "window_days": args.days,
        "with_bodies": bool(args.with_bodies),
        "counts": {"articles": len(articles), "eventnews": len(events)},
    }
    _write(out / "meta.json", meta)

    print(
        f"書き出し完了: 記事 {len(articles)} 件 / 事象 {len(events)} 件 "
        f"/ {total_bytes / 1024 / 1024:.1f} MB / 本文 "
        f"{'あり' if args.with_bodies else 'なし'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
