"""被害国の過剰付与を抑える関門 (2026-10-10、2026-10-11 キーワード関門追加)。

本番 140 件の監査 (data/eval/victim_country_audit_v1.jsonl) で、被害組織の無い
非インシデント記事 (advisory/press/research/opinion) に victim_country が誤って
付く割合が高いことが判明した: advisory 13 誤/3 正、press 12/1、research+opinion
8/5 (breaking は 74 正/11 誤で問題なし)。典型例は CISA KEV advisory が US、
ドイツ BSI の警告が DE になる — 発行元の国を被害国と混同している。

このルールをシミュレーションすると誤り 27 件を修正し正しい値 3 件を NULL 化する
(誤った国名を出すより欠落させる方が許容できるという判断)。

rollback: 環境変数 ``VICTIM_COUNTRY_GATE`` を ``"0"`` にすると常に無効化 (False)。

## 案 A: 侵害・攻撃の語彙関門 (``VICTIM_COUNTRY_GATE_KEYWORD``、2026-10-11)

被害組織名 (victim_orgs) があっても通過してしまう行に、なお誤りが多いことが
C1 holdout 再測定 (data/eval/C1_gold.jsonl、`data/mlx/measure_C1.py` b1_kept) で
判明した (境界を除く誤り率 81.4%、35/43)。誤りのほぼ全てが、victim_orgs 欄に
被害組織でない実体 (省庁・軍の部隊名、規制/訴訟/決算の当事者企業、発行元自身等)
が混入したもの。設計 (``data/eval/c1_x_design.md`` §2.2 案 A) は、本文+要約に
「侵害・攻撃の事実」を示す語彙が 1 つも無いときは victim_org があっても
victim_country を落とす、という後付けの決定論的関門 (LLM 呼び出しを追加しない)。

調整用標本 (C1_gold の b1_kept 43 件、境界除く) では誤り 35 件中 28 件 (80.0%) を
修正し、正しい値 8 件は 1 件も失わない (0/8) — ただしこれは関門の設計そのものに
使った標本の値であり、別の無作為標本での再測定が要る (`data/eval/p3_report.md`)。

### 新標本での測り直しと 2 点の仕上げ (2026-10-11)

別の無作為標本 (data/eval/P3_gold.jsonl、main 80 件 + jp 40 件) で測り直したところ、
main 層は合格線を満たしたが、jp 層 (victim_country=JP) で正しい日本の被害国を 1 件
誤って落とした (大阪府の個人情報書類の「誤送付」— 攻撃・侵害の語彙を使わない事故由来の
漏えい)。これを受けて 2 点を追加した:

1. **``victim_country_iso`` が日本 (JP) の行には語の関門 (案 A 部分) を当てない**
   (``japan_relevance.is_japan_targeted_row`` の SSoT で判定)。日本関連は見逃しの代償が
   大きい工程であり回収率を優先する (docs/accuracy_targets.md §2 の方針)。org 自体が
   無い行を落とす既存の分岐 (非インシデント種別で victim_org 皆無) はこの例外の対象外
   (JP でも org が無ければ従来どおり落とす)。
2. 事故由来の情報漏えいを示す語 (誤送付/誤交付/誤って送付/誤公開/誤って公開/誤掲載/
   誤表示/紛失/誤破棄、英語 misdirected/mistakenly sent/lost/exposed/accidentally) を
   語彙に追加 — 語を足すのは「落とさない」方向にしか働かない。

既定は ``"1"`` (on)。``"0"`` で旧挙動 (victim_org があれば保持、語の関門を掛けない) に戻る。

rollback: 環境変数 ``VICTIM_COUNTRY_GATE_KEYWORD`` を ``"0"`` にすると
この追加条件は無効化され、旧挙動 (victim_org があれば保持) のままになる。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from src.cti.japan_relevance import is_japan_targeted_row

#: 被害組織が実在するインシデントではない記事種別 (src.pipeline.summary.ArticleType より)。
#: tutorial は監査対象には無かったが、定義上非インシデントのため含める。
NON_INCIDENT_ARTICLE_TYPES: frozenset[str] = frozenset(
    {"advisory", "press", "research", "opinion", "tutorial"}
)

#: 関門の有効/無効を切り替える環境変数名 (既定 on、"0" で無効化)。
GATE_ENV = "VICTIM_COUNTRY_GATE"

#: 案 A (侵害・攻撃の語彙関門) の有効/無効を切り替える環境変数名。
#: 既定 on (2026-10-11、新標本での測り直し後)。"0" のときだけ無効化する。
KEYWORD_GATE_ENV = "VICTIM_COUNTRY_GATE_KEYWORD"

#: 英語の「侵害・攻撃の事実」語彙 (data/eval/c1_x_design.md §2.2 案 A の一覧 +
#: 2026-10-11 事故由来の漏えいを示す語を追加、data/eval/p3_report.md §4)。
#: 大小文字を区別しない語境界マッチ。
_INCIDENT_EVIDENCE_PATTERN_EN: re.Pattern[str] = re.compile(
    r"\b("
    r"breach(?:ed|es)?"
    r"|compromis(?:e|ed|es|ing)"
    r"|hack(?:ed|s)?"
    r"|ransomware"
    r"|exfiltrat\w*"
    r"|stolen"
    r"|leak(?:ed|s)?"
    r"|victim\w*"
    r"|intrusion"
    r"|unauthoriz(?:ed)?\s+access"
    r"|infected"
    r"|misdirected"
    r"|mistakenly\s+sent"
    r"|lost"
    r"|exposed"
    r"|accidentally"
    r")\b",
    re.IGNORECASE,
)

#: 日本語の「侵害・攻撃の事実」語彙 (部分一致、data/eval/c1_x_design.md §2.2 案 A の一覧 +
#: 2026-10-11 事故由来の漏えい — 誤送付・紛失等 — を示す語を追加、data/eval/p3_report.md §4)。
INCIDENT_EVIDENCE_WORDS_JA: tuple[str, ...] = (
    "攻撃",
    "侵害",
    "漏えい",
    "漏洩",
    "ランサムウェア",
    "流出",
    "不正アクセス",
    "被害",
    "盗まれ",
    "侵入",
    "感染",
    "窃取",
    "誤送付",
    "誤交付",
    "誤って送付",
    "誤公開",
    "誤って公開",
    "誤掲載",
    "誤表示",
    "紛失",
    "誤破棄",
)


def _gate_enabled() -> bool:
    """``VICTIM_COUNTRY_GATE`` (既定 on)。"0" のときだけ無効化する。"""
    return os.environ.get(GATE_ENV, "1").strip() != "0"


def _keyword_gate_enabled() -> bool:
    """``VICTIM_COUNTRY_GATE_KEYWORD`` (既定 on、2026-10-11)。"0" のときだけ無効化する
    (旧挙動 = victim_org があれば保持、語の関門を掛けない)。"""
    return os.environ.get(KEYWORD_GATE_ENV, "1").strip() != "0"


def _has_incident_evidence(body_text: str) -> bool:
    """本文+要約に「侵害・攻撃の事実」を示す語彙が 1 つでもあるか。"""
    if not body_text:
        return False
    if _INCIDENT_EVIDENCE_PATTERN_EN.search(body_text):
        return True
    return any(word in body_text for word in INCIDENT_EVIDENCE_WORDS_JA)


def should_drop_victim_country(
    article_type: str,
    victim_orgs: Sequence[str],
    body_text: str = "",
    victim_country_iso: str | None = None,
) -> bool:
    """被害国を落とすべきか。

    非インシデント種別 (``NON_INCIDENT_ARTICLE_TYPES``) で、かつ被害組織名が
    1 件も (空白除去後に) 無いときに True を返す。この分岐は ``victim_country_iso``
    に関わらず常に適用する (JP でも org が無ければ従来どおり落とす)。

    ``VICTIM_COUNTRY_GATE_KEYWORD`` が有効なとき (既定 on) は、被害組織名があっても
    ``body_text`` (本文+要約) に侵害・攻撃の語彙 (``_has_incident_evidence``) が
    1 つも無ければ、なお True (落とす) を返す (案 A)。**ただし ``victim_country_iso``
    が日本 (JP) の行には、この語の関門を当てない** (2026-10-11、P3 新標本測り直し:
    正しい日本の被害国を 1 件誤って落としたため。日本関連は見逃しの代償が大きい工程で
    回収率を優先する — docs/accuracy_targets.md §2。判定は
    ``japan_relevance.is_japan_targeted_row`` の SSoT を使う、複製辞書は作らない)。
    """
    if not _gate_enabled():
        return False
    if article_type not in NON_INCIDENT_ARTICLE_TYPES:
        return False
    has_org = any(str(org).strip() for org in victim_orgs)
    if not has_org:
        return True
    if not _keyword_gate_enabled():
        return False
    if is_japan_targeted_row(victim_country_iso, None):
        return False
    return not _has_incident_evidence(body_text)
