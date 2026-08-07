"""「サイバー」カテゴリ集合の SSoT (監査 2026-07-16 4f)。

同じ「cyber」という語で意味の異なる 2 つの分母が別モジュールに二重定義され、
サーフェス間で件数が黙って食い違っていた (situation の cyber_target > 地図の count)。
**2 つのレンズは意図的に別物** — 統一はせず、命名して 1 箇所に定義し drift だけを封じる:

- CYBER_THREAT_SCOPE (広義・脅威状況レンズ): 「その国が受けている脅威」の集計。
  ransomware / vulnerability を含む (脆弱性勧告もその国のリスク面)。国家情勢ボードが使う。
- CYBER_ATTACK_EVENTS (狭義・攻撃イベントレンズ): 「実際に発生した攻撃・被害」のみ。
  vulnerability (勧告) と ransomware (is_ransomware フラグで別扱い) を含めない。地図が使う。

利用側は必ず本モジュールを import する (複製辞書を作らない — CLAUDE.md §7 規約)。
"""

from __future__ import annotations

# 広義: 脅威状況レンズ (国家情勢ボード「標的として」面)
CYBER_THREAT_SCOPE: tuple[str, ...] = (
    "breach",
    "incident",
    "apt",
    "apt_leak",
    "malware",
    "phishing",
    "ransomware",
    "vulnerability",
)

# 狭義: 攻撃イベントレンズ (脅威マップの placed events。ransomware は is_ransomware
# フラグ側で扱い、vulnerability は「被害」でないため含めない)
CYBER_ATTACK_EVENTS: frozenset[str] = frozenset(
    {"breach", "incident", "apt", "apt_leak", "malware", "phishing"},
)
