"""切替 (EVENTNEWS_PAIR_ML=1) 直後に、ML が実際に群化へ効いたかを確かめる。

⚠ 「フラグを足した」と「効いている」は別 (2026-08-29 に Spotlight で 1 度外した)。
   ジョブが succeeded でも、モデルが読めなければ decide() は空を返し決定論のまま動く。

使い方:
    docker exec kuebiko python /app/scripts/check_pair_ml_cutover.py
"""

import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app")

from src.eventnews import pair_model, pair_shadow
from src.storage.db_backend import connect


def main() -> None:
    live = pair_shadow.is_live()
    model = pair_model.load_model()
    print(f"EVENTNEWS_PAIR_ML     : {'1 (live)' if live else '0 (決定論)'}")
    print(f"学習済みモデルの読込   : {'成功' if model is not None else '失敗 → 決定論で動く'}")
    if not live or model is None:
        print("\n⚠ ML は群化に効いていない。")
        return

    since = datetime.now(UTC) - timedelta(hours=3)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ml_joined, rule_joined FROM event_pair_shadow WHERE observed_at > ?",
            (since.isoformat(),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    scored = [r for r in rows if r["ml_joined"] is not None]
    if not scored:
        print("\n直近 3 時間に観測ペアなし (候補が全部新規事象だと発生しない)。")
        return
    ml = sum(1 for r in scored if r["ml_joined"])
    rule = sum(1 for r in scored if r["rule_joined"])
    diff = sum(1 for r in scored if bool(r["ml_joined"]) != bool(r["rule_joined"]))
    print(f"\n直近 3 時間 {len(scored)} 組: ML が繋ぐ {ml} / 規則なら {rule} / 食い違い {diff}")
    print("食い違いの中身は scripts/show_pair_shadow.py で読む。")


if __name__ == "__main__":
    main()
