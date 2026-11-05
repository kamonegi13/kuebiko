"""n20 契約 T3 (審査役の自己一致 + n19 の基線) と T4 (翻訳の教師の監査) の道具 (2026-11-04)。

根拠: data/eval/n20_contract_2026_11_02.md §1・§3・§4、
data/eval/n20_s26_design_2026_10_29.md §2.3・§4.1。
DB は SELECT のみ (docker exec psql)、LLM の生成はしない。Opus は adjudication_cache 経由。
"""
