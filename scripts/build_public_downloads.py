"""公開サイトに配布物 (PDF) を置き、URL で直接ダウンロードできるようにする。

配布物は **リポジトリに入れない** (公開リポジトリに運用者の資料を載せない・容量も
大きい)。git 管理外の置き場 (既定 ``data/public_downloads/``) に置いた PDF を、
公開サイトの配信物の ``downloads/`` へコピーする → ``https://<公開サイト>/downloads/<名前>.pdf``。
画面からの導線は作らない (URL を渡して配る)。

⚠ 置き場に置いたものは **匿名で誰でも取得できる**。置く前に中身を確認すること。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

#: Cloudflare Pages の 1 ファイルの上限 (25 MiB)。超えると配信全体が失敗する
PAGES_FILE_LIMIT = 25 * 1024 * 1024
#: 名前は URL にそのまま出るので、ASCII の安全な文字に限る
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")


class DownloadsError(ValueError):
    """配布物に問題がある (配信を止める)。"""


def build(src: Path, out: Path) -> list[str]:
    """置き場の PDF を ``out/downloads/`` へコピーし、コピーした名前を返す。

    置き場が無ければ何もしない。名前か大きさが条件を満たさないものがあれば、
    1 つもコピーせずに止める (一部だけ配られるのを避ける)。
    """
    if not src.is_dir():
        return []
    files = sorted(p for p in src.iterdir() if p.is_file() and not p.name.startswith("."))
    for p in files:
        if not _SAFE_NAME.match(p.name):
            raise DownloadsError(f"名前は英数字と . _ - の .pdf のみ: {p.name!r}")
        size = p.stat().st_size
        if size > PAGES_FILE_LIMIT:
            raise DownloadsError(f"{p.name} が Pages の上限 25 MiB を超える ({size} bytes)")
    if not files:
        return []
    dest = out / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.copy2(p, dest / p.name)
    return [p.name for p in files]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--src", type=Path, required=True, help="配布物の置き場")
    ap.add_argument("--out", type=Path, required=True, help="公開サイトの配信物のディレクトリ")
    args = ap.parse_args()
    try:
        names = build(args.src, args.out)
    except DownloadsError as exc:
        print(f"⚠ 配布物の組み込みを中止: {exc}", file=sys.stderr)
        return 1
    for n in names:
        print(f"配布物: /downloads/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
