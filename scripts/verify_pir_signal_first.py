"""PIR signal-first 照合の A/B 検証 (read-only)。

PoC 対象 3 PIR について、旧 keyword 照合 (strong_signals) と新 signal-first 照合
(match ツリー × ArticleFacts) の 30 日マッチ件数を before/after で比較し、
差分 (keyword のみ / signal のみ) のサンプルタイトルを出力する。

DB は変更しない。match ツリーは本スクリプト内で定義 (yaml/DB への永続化は
A/B が良好であることを確認してから別途行う)。

    docker exec kuebiko python /app/scripts/verify_pir_signal_first.py
"""

from __future__ import annotations

from typing import Any

from src.pir.article_facts import ArticleFacts
from src.pir.evaluator import _load_posted_rows, _row_match_signals
from src.pir.integration import get_pir_config
from src.pir.signal_match import evaluate_match

LOOKBACK_HOURS = 24 * 30
LIMIT = 15000

# PoC 対象 3 PIR の match ツリー (docs/pir_signal_first_matching_design.md §5)。
# すべて永続化済みフィールド (intent / category / is_ransomware / victim_sector) のみ使用
# = スキーマ移行ゼロ。
MATCH_TREES: dict[str, dict[str, Any]] = {
    # 影響力工作/偽情報 = intent=influence (加害国 CN/RU の全 APT 活動を拾っていた
    # actor_nations 依存を撤去)。subversion は政変/テロ/軍事も含み広すぎるため不採用。
    # CN/RU への絞りは actor_nation property (Phase 2) で all[] 追加する。
    "pir_disinfo": {
        "property": "intent",
        "op": "eq",
        "value": "influence",
    },
    # 脆弱性記事 = category=vulnerability (多義語「脆弱性」= 軍事的脆弱性まで拾う keyword を撤去)。
    "pir_new_poc_vuln": {
        "property": "category",
        "op": "eq",
        "value": "vulnerability",
    },
    # 国家・政府レベルのランサム = ransomware AND victim_sector∈{政府/医療/電力}
    # (「ransomware」語で全ランサム記事を拾っていたのを AND で絞る)。
    "pir_state_ransomware": {
        "all": [
            {"property": "is_ransomware", "op": "is_true"},
            {
                "property": "victim_sector",
                "op": "in",
                "value": ["government", "healthcare", "energy"],
            },
        ]
    },
}


def _title(row: Any) -> str:  # noqa: ANN401
    return (row["title"] or "")[:76]


def main() -> None:
    cfg = get_pir_config()
    rows, actor_map = _load_posted_rows(lookback_hours=LOOKBACK_HOURS, limit=LIMIT)
    print(f"posted rows (30d) = {len(rows)}\n")

    for pir_id, tree in MATCH_TREES.items():
        pir = cfg.find(pir_id)
        if pir is None:
            print(f"!! {pir_id} not found in config")
            continue

        kw_ids: set[str] = set()
        sig_ids: set[str] = set()
        kw_titles: dict[str, str] = {}
        for r in rows:
            aid = str(r["article_id"])
            kw_titles[aid] = _title(r)
            actor_values = actor_map.get(r["article_id"], set())
            if _row_match_signals(r, actor_values, pir.strong_signals):
                kw_ids.add(aid)
            ok, _fired = evaluate_match(tree, ArticleFacts.from_db_row(r))
            if ok:
                sig_ids.add(aid)

        dropped = kw_ids - sig_ids  # keyword が拾い signal が落とす (= FP 候補)
        added = sig_ids - kw_ids  # signal のみが拾う (= keyword が漏らしていた真マッチ候補)
        kept = kw_ids & sig_ids

        print(f"### {pir_id}  ({pir.title})")
        print(f"    keyword(before) = {len(kw_ids)}   signal(after) = {len(sig_ids)}")
        print(f"    kept={len(kept)}  dropped(kw)={len(dropped)}  added(signal)={len(added)}")
        print(f"    match tree = {tree}")
        if dropped:
            print("    -- dropped (keyword が拾っていた誤爆候補) --")
            for aid in list(dropped)[:8]:
                print(f"       {kw_titles.get(aid, '')}")
        if added:
            print("    -- added (signal のみが拾う) --")
            for aid in list(added)[:8]:
                print(f"       {kw_titles.get(aid, '')}")
        print()


if __name__ == "__main__":
    main()
