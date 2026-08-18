#!/usr/bin/env python3
"""legacy summarizer.j2 と rubric 合成結果の期待差分 (移行専用の一時的契約)。

summarizer プロンプトの層分け (WP1: ``src/prompts/`` + ``config/prompts/summarizer_rubric.yaml``
seed) は、契約散文 (JSON 強制の重複指示・schema が既に強制する「必須」文言等) を
``prompts/briefing/summarizer.j2`` から削り、判定基準 (rubric) だけを残す設計になっている。
本モジュールは「legacy と合成結果の差分は *この表に載っているものだけ* であるべき」という
移行時の契約をデータとして固定し、``scripts/verify_summarizer_rubric.py`` と
``tests/unit/test_summarizer_legacy_equivalence.py`` の両方から参照される (単一の真実)。

差分は 2 種類ある (混ぜないこと):
  §1 **移行時の契約散文** — 層分けで削った重複指示。legacy と合成で *指示は同じ*。
  §2 **移行後の意図的な rubric 変更** — legacy は rollback 用に凍結する方針なので、
     合成側だけに入れた変更はここへ追記する。判定基準を編集するたび表が伸びるのは
     設計どおりで、伸び続けるようなら「legacy 凍結の価値 < 維持コスト」の合図
     (= 下記の撤去時期が来ている)。

**これは恒久的な設計ではない**。CLAUDE.md §10 の観察期間 (1 か月) を経て
``empty_fields`` / ``fill_rate_audit`` に劣化が無いことを確認したら、
``prompts/briefing/summarizer.j2`` 本体と合わせて次を **まとめて撤去** する:
  - 本モジュール (``scripts/summarizer_diff_contract.py``)
  - ``scripts/verify_summarizer_rubric.py``
  - ``tests/unit/test_summarizer_legacy_equivalence.py``
  - ``tests/fixtures/summarizer_composed_expected.txt``
撤去時は ``pyproject.toml`` の mypy override からも本モジュール名を外すこと。

行番号は ``prompts/briefing/summarizer.j2`` (403 行、対象コミット 05cfc42) の **1 始まり**。
"""

from __future__ import annotations

from pathlib import Path

# legacy テンプレートの正本パス (repo root からの相対パス)。
LEGACY_PATH = Path("prompts/briefing/summarizer.j2")

# 移行後に意図して入れた rubric 変更の理由 (契約散文の削除とは種類が違う。下記 §2)。
_ARTICLE_TYPE_SUPPRESSED = (
    "article_type を suppressed 化 (2026-08-18)。summarizer 出力は本番 134 件 / "
    "gold set 86 件とも 100% breaking に枯死し、値は judgment 分類器が model_copy で "
    "上書きしていた。schema 既定値も breaking なので挙動は不変"
)
_ROUTING_FLAGS_SUPPRESSED = (
    "routing_flags を suppressed 化 (2026-08-18)。gold set 258 件で返却ゼロ。"
    "dedup_key=compute_dedup_key / primary_actor_id・confidence=judgment 分類器 / "
    "japan_targeted・is_breaking_critical=regex + victim_country が実際の供給元"
)
_PMESII_SUPPRESSED = (
    "pmesii_axes を suppressed 化 (2026-08-18)。gold set 258 件で返却ゼロ。"
    "本番 14 日 3,511 件のうち category/feed 既定マッピング + judgment の I-infra で"
    "説明できない軸を持つ記事は 2 件 (0.06%) のみ"
)

# legacy 行番号 → 削除理由。これ以外の削除は検証 fail。
EXPECTED_DELETIONS: dict[int, str] = {
    # §1 移行時の契約散文 (層分けで削った重複指示)
    23: "title_ja は schema required + min_length=1 で文法強制済 (契約散文)",
    403: (
        "JSON 強制は 4 クライアント実装すべてが schema/tool_use/自動付加で担保済 "
        "(Ollama=format / Anthropic=tool_use / openai互換・claudecode=末尾に schema 自動付加)"
    ),
    # §2 移行後の意図的な rubric 変更: article_type の判定基準 (L230-241) と
    # 出力例 3 件のキー (L348/370/392)。legacy 側は rollback 用に据え置くため、
    # 「合成側にだけ入れた変更」はここに追記して未分類にしない。
    **dict.fromkeys(range(230, 242), _ARTICLE_TYPE_SUPPRESSED),
    348: _ARTICLE_TYPE_SUPPRESSED,
    370: _ARTICLE_TYPE_SUPPRESSED,
    392: _ARTICLE_TYPE_SUPPRESSED,
    # routing_flags の判定基準 (L242-279) と出力例 3 件の object (L349-355 / 371-377 /
    # 393-399)。例では最終キーになるため直前行の末尾カンマが消える = 下の REWRITES 側。
    # ⚠ 空行 (L252/261/316) は除く — 分類は行番号でなく **行内容** で行うため、空文字を
    # 削除許容リストに入れると文書中どこの空行削除でも「期待どおり」と誤分類される。
    **dict.fromkeys([n for n in range(242, 280) if n not in (252, 261)], _ROUTING_FLAGS_SUPPRESSED),
    **dict.fromkeys(range(349, 356), _ROUTING_FLAGS_SUPPRESSED),
    **dict.fromkeys(range(371, 378), _ROUTING_FLAGS_SUPPRESSED),
    **dict.fromkeys(range(393, 400), _ROUTING_FLAGS_SUPPRESSED),
    # pmesii_axes の判定基準 (L281-331)。出力例には元から含まれていない。
    **dict.fromkeys([n for n in range(281, 332) if n != 316], _PMESII_SUPPRESSED),
}

# legacy 行番号 → (legacy 原文, 合成後の期待文字列)。
# L15 は章見出しの置換、それ以外の 18 行は「型注釈括弧の削除」= 見出し簡素化。
EXPECTED_REWRITES: dict[int, tuple[str, str]] = {
    15: (
        "# 出力スキーマ (JSON のみ、前置き・解説不要)",
        "# フィールド別の判定基準",
    ),
    17: (
        "- ``title_ja`` (string, **必須・絶対に省略禁止**): **記事タイトルの日本語訳 (60 "
        "字以内)**。",
        "- ``title_ja``: **記事タイトルの日本語訳 (60 字以内)**。",
    ),
    24: (
        '- ``importance`` ("high" | "medium" | "low") — **category により判定軸が異なる**:',
        "- ``importance``: **category により判定軸が異なる**:",
    ),
    55: (
        "- ``category`` (string): 以下から 1 つ選択:",
        "- ``category``: 以下から 1 つ選択:",
    ),
    107: (
        "- ``victim_sector`` (string | null, **被害事案では必須**): 被害組織のセクター。",
        "- ``victim_sector``: 被害組織のセクター。",
    ),
    117: (
        "- ``victim_country`` (string | null, **被害事案では必須**): 被害組織の国 (raw text or ISO "
        "code)。",
        "- ``victim_country``: 被害組織の国 (raw text or ISO code)。",
    ),
    128: (
        "- ``victim_city`` (string | null): 被害組織の **所在都市 / 地域** "
        "が記事に明示されていれば",
        "- ``victim_city``: 被害組織の **所在都市 / 地域** が記事に明示されていれば",
    ),
    134: (
        "- ``involved_countries`` (array of string): **geopolitical / policy "
        "事象の「当事国（主体国）」**。",
        "- ``involved_countries``: **geopolitical / policy 事象の「当事国（主体国）」**。",
    ),
    143: (
        "- ``victim_orgs`` (array of string): 地図プロット用の "
        "**実際に攻撃を受け侵害された組織名**",
        "- ``victim_orgs``: 地図プロット用の **実際に攻撃を受け侵害された組織名**",
    ),
    174: (
        "- ``summary`` (string): **2〜3 段落の日本語要約。全体で 250〜500 字に収める**。",
        "- ``summary``: **2〜3 段落の日本語要約。全体で 250〜500 字に収める**。",
    ),
    176: (
        "- ``iocs`` (array of string, Phase Diamond): 本文に明示されている IP / ドメイン / "
        "ハッシュ /",
        "- ``iocs``: 本文に明示されている IP / ドメイン / ハッシュ /",
    ),
    191: (
        "- ``mitre_techniques`` (array of string, Phase Diamond): MITRE ATT&CK Technique ID。",
        "- ``mitre_techniques``: MITRE ATT&CK Technique ID。",
    ),
    206: (
        "- ``malware_families`` (array of string, Phase Diamond): 本文に明示されている",
        "- ``malware_families``: 本文に明示されている",
    ),
    210: (
        "- ``tools`` (array of string, Phase Diamond): 本文に明示されている **サイバー攻撃ツール** "
        "の名前。",
        "- ``tools``: 本文に明示されている **サイバー攻撃ツール** の名前。",
    ),
    228: (
        "- ``analyst_note`` (string | null): 日本の CTI 担当者の視点での",
        "- ``analyst_note``: 日本の CTI 担当者の視点での",
    ),
    332: (
        "- ``remediation`` (string|null): **本文に明示された対処** "
        "(修正版/パッチ/回避策/緩和策/推奨対応)",
        "- ``remediation``: **本文に明示された対処** (修正版/パッチ/回避策/緩和策/推奨対応)",
    ),
    # §2 由来: routing_flags を落とした結果、出力例で最終キーになった行の末尾カンマが消える。
    347: (
        '  "remediation": "ScreenConnect を 23.9.8 以降へ更新し、'
        '掲載 IOC (C2 ドメイン/IP) を遮断する。",',
        '  "remediation": "ScreenConnect を 23.9.8 以降へ更新し、'
        '掲載 IOC (C2 ドメイン/IP) を遮断する。"',
    ),
    369: (
        '  "analyst_note": "国内外の Apache 利用者は速やかにパッチ適用を推奨。",',
        '  "analyst_note": "国内外の Apache 利用者は速やかにパッチ適用を推奨。"',
    ),
    391: (
        '  "analyst_note": "",',
        '  "analyst_note": ""',
    ),
}

# 空白のみの差分の許容上限 (出力例見出し前の空行正規化 + legacy 末尾空行の削除)。
# 3 → 5 (2026-08-18): routing_flags / pmesii_axes の判定基準に含まれていた空行 3 行が、
# セクションごと消えたことで空白差分として数えられるようになったため。
# ⚠ 空行を削除許容リスト側に入れて数を減らすのは不可 (行内容で分類するため、
#   文書中どこの空行削除でも「期待どおり」になってしまう)。
MAX_WHITESPACE_DIFFS = 5

# EXPECTED_REWRITES の自己検証。契約表そのものの改変ミス (行の消し忘れ等) を早期に検出する。
# 19 → 18: article_type の見出しは「置換」ではなく「削除」へ移った (2026-08-18)。
# 18 → 19: routing_flags 除去で出力例 3 件の末尾カンマが消えた (同日)。
# なお routing_flags / pmesii_axes の見出し置換 (旧 242 / 281) は削除側へ移動している。
_EXPECTED_REWRITE_COUNT = 19
assert len(EXPECTED_REWRITES) == _EXPECTED_REWRITE_COUNT, (
    f"EXPECTED_REWRITES は {_EXPECTED_REWRITE_COUNT} 行のはずだが {len(EXPECTED_REWRITES)} 行"
)
# 2 (§1 契約散文) + 15 (article_type) + 57 (routing_flags) + 50 (pmesii_axes) = 124。
# 空行 3 行は含めない (上記の注意書き)。
_EXPECTED_DELETION_COUNT = 124
assert len(EXPECTED_DELETIONS) == _EXPECTED_DELETION_COUNT, (
    f"EXPECTED_DELETIONS は {_EXPECTED_DELETION_COUNT} 行のはずだが {len(EXPECTED_DELETIONS)} 行"
)
