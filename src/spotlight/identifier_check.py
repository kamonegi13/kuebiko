"""spotlight の識別子関門 — 事象ニュース関門の narrative 延長 (2026-09-06)。

背景: narrative 特化 SFT (kuebiko-sft:26b) の spotlight 実測で、固有名詞の低頻度破損
("Nanjing Xinjiuwei"→"Xinjirazu"、".hl.cn"→".hl.conn") を確認した。事象ニュースは
識別子関門 (``src/eventnews/identifier_gate.py``) を持つが spotlight には無かった。

方式は事象ニュース関門と同じ**対称照合**: 生成文・参照文の両側に同じ抽出器
(``extract_identifiers``) をかけ、生成側にだけ現れる識別子を「出典不支持」とする。
判定経路が 1 本なので抽出器の癖は両側で相殺される (2026-08-23 の抜本改修の原則)。
参照側は **LLM に渡したプロンプト実文字列** — モデルに見せたものだけが引用可能。

適用範囲の限界 (意図的):
- 単独大文字の固有名詞 ("Xinjirazu" 型) は対象外。固有名詞は文法が開いており、
  一般英単語との区別がつかない (identifier_match の設計判断を踏襲)。
- 短い一般略語 (EDR/C2/IoC 等、正規化後 4 字未満の proper_noun) は誤検出源に
  なるため拾わない。
残余は構造ログ (``spotlight_identifier_unverified``) が計数し、将来の RLVR 報酬
設計の測定にもなる。
"""

from __future__ import annotations

import os

from src.tools.identifier_match import Identifier, contains_identifier, extract_identifiers

# rollback flag: 0 で関門を無効化し従来挙動 (検査なし) に戻す。
GATE_FLAG_ENV = "SPOTLIGHT_IDENTIFIER_GATE"

# 書き直しを強制する kind = **厳密文法のもののみ** (2026-09-07 較正)。
# proper_noun は SaaS/SMTP/SBOM/YARA/SIEM 等の一般技術語を大量に誤検知する —
# 教師データ実測で 313 件中 271 件 (87%) を誤棄却、本番でも YARA/SIEM/NATO14 で
# 誤発火した。proper_noun は観測 (soft watch) に降格し、強制しない。
STRICT_KINDS: frozenset[str] = frozenset(
    {"cve", "ip", "domain", "hash", "version", "cvss", "actor_id"}
)

# proper_noun のみ最小長で足切り (EDR/C2/IoC 等の一般略語の誤検出抑制)。
_PROPER_NOUN_MIN_LEN = 4


def gate_enabled() -> bool:
    """関門の有効判定 (既定 ON)。``SPOTLIGHT_IDENTIFIER_GATE=0`` で無効。"""
    return os.environ.get(GATE_FLAG_ENV, "1").strip() not in ("0", "false", "False")


def find_unsupported_identifiers(
    generated: str, reference: str, *, kinds: frozenset[str] = STRICT_KINDS
) -> tuple[Identifier, ...]:
    """生成文の識別子のうち、参照文 (プロンプト) に存在しないものを返す。

    ``kinds``: 対象 kind の絞り込み。既定は厳密文法のみ (proper_noun を含めると
    一般技術語の誤検知が支配的になる — 観測用途でのみ広げること)。
    """
    if not generated:
        return ()
    out: list[Identifier] = []
    for ident in extract_identifiers(generated):
        if ident.kind not in kinds:
            continue
        if ident.kind == "proper_noun" and len(ident.normalized) < _PROPER_NOUN_MIN_LEN:
            continue
        if not contains_identifier(reference, ident):
            out.append(ident)
    return tuple(out)


# 観測用 (強制しない): proper_noun を含む全 kind。誤検知率の測定と RLVR 報酬設計の材料。
ALL_KINDS: frozenset[str] = STRICT_KINDS | {"proper_noun"}


def render_identifier_feedback(unsupported: tuple[Identifier, ...]) -> str:
    """書き直し指示 (具体値の列挙)。

    事象ニュースの実測 (2026-08-27): 散文の一般指示は無効だが、**具体的な値の指摘**
    による書き直しは効く (「構造は効く」側の介入)。
    """
    values = "\n".join(f"- {i.raw}" for i in unsupported)
    return (
        "\n\n【識別子検査 — 書き直し指示】\n"
        "上で生成した文章に、出典記事群に存在しない識別子・名称が含まれていた:\n"
        f"{values}\n"
        "これらは転記誤りか出典外の値である。出典に実在する正しい値に直すか、"
        "該当する言及を削除して、全体を書き直すこと。出典に無い識別子を新たに書かないこと。"
    )
