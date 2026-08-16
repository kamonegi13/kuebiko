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

# ---------- 充足率レンズ (分析列がどれだけ埋まっているかを測る分母) ----------
#
# 上の 2 つが「どの記事を脅威として数えるか」なのに対し、こちらは「この列が本来
# 埋まるべき記事はどれか」。全記事で測ると意味を失う (地政学記事に victim_sector が
# 無いのは正常で、サイバー事案に無いのが異常) ため、指標ごとに分母を変える。
#
# ⚠ 上のレンズとは意図的に集合が違う (apt_leak / phishing / ransomware を含まない)。
# fill_rate_audit が導入時から使っている区分で、audit_triage_calibration / backfill と
# 揃えてある。統一すると過去の週次判定と地続きでなくなるため、名前を分けて併存させる。

# サイバー系全般。victim_* / iocs / ttp など攻撃事案の属性の分母。
COVERAGE_CYBER: tuple[str, ...] = (
    "apt",
    "malware",
    "incident",
    "advisory",
    "vulnerability",
    "breach",
)

# 脆弱性系。remediation / affected_vendor など「対処」を持つ記事の分母。
COVERAGE_VULN: tuple[str, ...] = ("vulnerability", "advisory")

# 侵入事案系。event_date / compromise_date の分母 — CVE 公表に「侵入日」の概念は
# 構造的に無く、advisory/vulnerability を分母に入れると埋まらない分で沈黙崩壊を見落とす。
COVERAGE_CYBER_EVENT: tuple[str, ...] = ("apt", "malware", "incident", "breach")

# 地政学・政策系。involved_countries (当事国) の分母 — サイバー事案では空が正しい。
COVERAGE_GEOPOLITICAL: tuple[str, ...] = ("geopolitical", "policy")
