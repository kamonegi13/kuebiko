"""sitemap 由来記事の published_at を lastmod で埋め直す (2026-08-16)。

sitemap watcher は長らく published に取込時刻 (``datetime.now()``) を入れており、
「後段で trafilatura が取得し直す」というコメントに反してその再取得は配線されて
いなかった。結果、何年も前の記事が「今日」として台帳・地図・ブリーフに載っていた。
watcher 側は commit 0f0876b で lastmod を使うよう是正済みだが、**既に永続化された
行は直らない**ため、sitemap を引き直して遡及修正する。

対象の絞り込み (安全側):
    - article_id の接頭辞が sitemap watcher 名の行のみ
    - **published_at ≈ created_at (既定 1 時間以内) の行のみ** — これが「取込時刻を
      公開日として入れてしまった」損傷の署名。既に正しい日付が入っている行は触らない
    - sitemap に lastmod がある URL のみ (無ければ判定不能なので放置)

⚠ lastmod は厳密には「最終更新日」で公開日ではない。取込時刻よりは実態に近い、という
   近似であることを承知の上で使う (完全な公開日が要るならページ再抽出 = 別手段)。

    uv run python scripts/backfill_sitemap_published.py          # dry-run
    uv run python scripts/backfill_sitemap_published.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect, translate_sql  # noqa: E402
from src.watchers.yaml_registry import get_registry  # noqa: E402

# published_at と created_at がこの差以内なら「取込時刻を代用した損傷行」とみなす。
DAMAGE_WINDOW = timedelta(hours=1)
# lastmod と現 published_at の差がこれ未満なら更新しない (無意味な書き換えを避ける)。
MIN_SHIFT = timedelta(hours=1)


def _as_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def _lastmod_map(name: str) -> dict[str, datetime]:
    """watcher の sitemap を引き直して url → lastmod を作る。"""
    watcher = get_registry().get(name)
    if watcher is None:
        return {}
    entries = await watcher._collect_all_entries()  # noqa: SLF001 — 取得経路を再利用する
    return {url: lastmod for (url, lastmod) in entries if lastmod is not None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="実際に更新する")
    args = parser.parse_args()

    conn = connect()
    names = sorted(get_registry())
    if not names:
        print("sitemap watcher が 1 件も見つかりません")
        return

    grand_total = 0
    for name in names:
        rows = conn.execute(
            translate_sql(
                "SELECT article_id, url, published_at, created_at FROM articles "
                "WHERE article_id LIKE ? AND url IS NOT NULL AND published_at IS NOT NULL"
            ),
            (f"{name}:%",),
        ).fetchall()
        if not rows:
            continue

        lastmods = asyncio.run(_lastmod_map(name))
        if not lastmods:
            print(f"{name:14} sitemap から lastmod を取得できず skip")
            continue

        planned: list[tuple[str, datetime, datetime]] = []
        for row in rows:
            published = _as_dt(row["published_at"])
            created = _as_dt(row["created_at"])
            lastmod = lastmods.get(str(row["url"]))
            if published is None or created is None or lastmod is None:
                continue
            # 損傷署名: published が取込時刻とほぼ同じ
            if abs(published - created) > DAMAGE_WINDOW:
                continue
            if abs(lastmod - published) < MIN_SHIFT:
                continue
            planned.append((str(row["article_id"]), published, lastmod))

        print(
            f"{name:14} 対象 {len(rows):5} 件 / sitemap lastmod {len(lastmods):5} 件 "
            f"→ 修正 {len(planned):5} 件"
        )
        # 変化量の分布 — 日付だけ出すと「同日の数時間ずれ」と「数か月のずれ」が
        # 区別できず、遡及修正の妥当性を判断できない。
        buckets = {"30日超": 0, "7-30日": 0, "1-7日": 0, "1日未満": 0}
        for _id, before, after in planned:
            days = abs((after - before).total_seconds()) / 86400
            key = (
                "30日超"
                if days > 30
                else "7-30日"
                if days > 7
                else "1-7日"
                if days > 1
                else "1日未満"
            )
            buckets[key] += 1
        print(f"    ずれ幅: {' / '.join(f'{k} {v}件' for k, v in buckets.items() if v)}")
        worst = sorted(planned, key=lambda p: abs((p[2] - p[1]).total_seconds()), reverse=True)
        for article_id, before, after in worst[:3]:
            days = abs((after - before).total_seconds()) / 86400
            print(
                f"    最大 {article_id[:30]:30} "
                f"{before:%Y-%m-%d %H:%M} → {after:%Y-%m-%d %H:%M} ({days:.0f}日)"
            )

        if args.apply and planned:
            for article_id, _before, after in planned:
                conn.execute(
                    translate_sql("UPDATE articles SET published_at=? WHERE article_id=?"),
                    (after, article_id),
                )
            conn.commit()
        grand_total += len(planned)

    verb = "修正しました" if args.apply else "修正予定 (dry-run)"
    print(f"\n合計 {grand_total} 件を{verb}")
    if not args.apply and grand_total:
        print("適用するには --apply を付けて再実行してください")


if __name__ == "__main__":
    main()
