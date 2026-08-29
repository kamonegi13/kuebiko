"""全 PIR の Spotlight を有効にする (2026-08-29)。

背景: Spotlight は 5 PIR だけで運用していた。窓を「直近 7 日を毎日作り直す」
(rolling7) に変えたことで、材料の薄い PIR でも週次相当の量を毎日の鮮度で扱える
ようになったため、全 PIR へ広げる。

⚠ 材料が 5 件を下回る PIR は generator が生成しない (埋め合わせを防ぐ関門)。
   有効にしても記事が無ければ出ない — それが正しい振る舞い。

    uv run python scripts/enable_all_pir_spotlight.py [--apply]

⚠ **実行時の SSoT は DB** (config_store)。`src.pir.loader.save_pir_config` は
   seed の yaml を書くだけで実行時には効かない (2026-08-29 に実際に踏んだ。
   同じ罠が rubric でも起きている)。書き込みは persist_pir_config を通すこと。
"""

from __future__ import annotations

import argparse
import sys

from src.pir.integration import load_current_pir_config, persist_pir_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に保存する (既定は下見のみ)")
    args = ap.parse_args()

    cfg = load_current_pir_config()
    changed: list[str] = []
    for p in cfg.priorities:
        if not p.enabled or p.spotlight.enabled:
            continue
        # ⚠ 元を書き換えず作り直す (frozen model)。
        p.spotlight.enabled = True
        if not p.spotlight.title:
            p.spotlight.title = p.title
        changed.append(p.id)

    print(f"有効にする PIR: {len(changed)} 件")
    for pid in changed:
        print(f"  - {pid}")
    if not changed:
        print("変更なし")
        return 0
    if not args.apply:
        print("\n下見のみ。実行するには --apply を付ける。")
        return 0
    persist_pir_config(cfg)
    # 読み側は in-process にキャッシュを持つ。落とさないと「保存したのに効かない」。
    from src.ui.api.pir import _invalidate_pir_caches

    _invalidate_pir_caches()
    print("保存した (DB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
