"""プロンプトの「禁止事項」が実データで守られているかを測る (2026-08-19)。

⭐⭐ **指示は「してほしいこと」は伝えるが「してはいけないこと」を止める力が弱い**
(2026-08-19 に 5 例で確認)。止められるものは決定論の関門を置く。**塞げないものは
測り続ける対象として可視化する** — その「測り続ける」をここに集約する。

各行は 1 つの禁止事項で、``since`` (指示が入った日) 以降の出力だけを母数にする。
禁止が入る前の違反を数えても「指示が効いたか」は分からない。判定は 3 値:

    OK          : 母数十分で違反 0
    VIOLATED    : 違反あり (関門が要る / 関門が漏れている)
    INCONCLUSIVE: 母数不足 (「様子見」を口約束でなく明示的な状態にする)

使い方 (production の PG を読む):
    docker exec -w /app kuebiko python -m scripts.audit_prompt_prohibitions

⚠ ここで測れるのは **機械判定できる禁止だけ**。「藁人形の対立仮説を書かない」
「アンカリング禁止」のような判断を要する禁止は対象外で、目録には載せない。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.cti.ioc_source_filter import source_hosts  # noqa: E402
from src.cti.victim_org_filter import PROTECTED_CATEGORIES, VENDOR_DENYLIST  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

STATUS_OK = "OK"
STATUS_VIOLATED = "VIOLATED"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
_EXIT = {STATUS_OK: 0, STATUS_VIOLATED: 1, STATUS_INCONCLUSIVE: 2}

# status_synthesis の文章セクション (件数・軸コードの制限が掛かる範囲)。
# axes_evidence の events 数は対象外と判定基準が明記しているので混ぜない。
_PROSE = "weight_section||' '||chain_section||' '||cog_section||' '||spillover_section"

# 生成語彙。カテゴリ語だけを弾く (「Lumma Stealer」のような固有名詞を巻き込まない)。
_GENERIC_MALWARE = (
    "ransomware",
    "malware",
    "backdoor",
    "trojan",
    "stealer",
    "infostealer",
    "rat",
    "loader",
    "webshell",
    "spyware",
    "worm",
    "botnet",
    "wiper",
    "dropper",
    "rootkit",
    "keylogger",
    "downloader",
    "adware",
)

# 正規 AI 製品名の**観測用** regex。関門 (malware_aliases.yaml の ai_platform_drop、
# 2026-08-19 導入) より**意図的に広く**取る — 関門と同じ判定で測ると「関門のバグ」しか
# 見えず、関門が知らない新変種 (新製品・新表記) を誰も検知しない。ここに引っかかったら
# 人が見て、本物なら関門の yaml に 1 行足す。
# ⚠ WormGPT / FraudGPT / PentestGPT は **攻撃ツールなので入れない** (悪性 LLM サービス)。
_AI_PLATFORM_REGEX = (
    r"^(claude|chatgpt|gpt-?[0-9]|gpt4all|gemini|google gemini|openai($| )|copilot"
    r"|github copilot|deepseek|perplexity|ollama$|msty$|lm studio|midjourney"
    r"|stable diffusion|comfyui|lora$|mythos|mistral|anthropic|hugging ?face|grok($| ))"
)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


@dataclass(frozen=True)
class Prohibition:
    """1 つの禁止事項と、その遵守を測る SQL。"""

    key: str
    source: str  # 指示の出所
    forbids: str  # 何を禁じているか
    gate: str  # 決定論の関門 (無ければ "—")
    since: str  # 指示 (または関門) が入った日。git log -S で特定した
    sql: str  # (n, violations) を返す
    min_sample: int


def _checks() -> list[Prohibition]:
    vendors = _in_list(tuple(sorted(VENDOR_DENYLIST)))
    protected = _in_list(tuple(sorted(PROTECTED_CATEGORIES)))
    hosts = tuple(sorted(source_hosts()))
    host_list = _in_list(hosts) if hosts else "''"
    generic = _in_list(_GENERIC_MALWARE)
    ai_regex = _AI_PLATFORM_REGEX.replace("'", "''")
    return [
        Prohibition(
            key="synthesis.axis_code",
            source="status_synthesis.j2 §インライン軸コードの禁止",
            forbids="文章セクションに (M) (I-cyber) 等の軸コードを書かない",
            gate="—",
            since="2026-08-15",
            sql=(
                f"SELECT count(*) n, count(*) FILTER (WHERE {_PROSE} ~ "
                r"'\((M|P|E|S|I|I-cyber|I-infra|P/M|E/I-infra)\)') v"
                " FROM status_synthesis WHERE generated_at >= '2026-08-15'"
            ),
            min_sample=20,
        ),
        Prohibition(
            key="synthesis.absolute_count",
            source="status_synthesis.j2 §件数記述の制限",
            forbids="文章セクションで「N 件」等の絶対数を書かない (相対表現は可)",
            gate="—",
            since="2026-08-15",
            sql=(
                f"SELECT count(*) n, count(*) FILTER (WHERE {_PROSE} ~ "
                r"'[0-9][0-9,]*\s*件') v"
                " FROM status_synthesis WHERE generated_at >= '2026-08-15'"
            ),
            min_sample=20,
        ),
        Prohibition(
            key="synthesis.symbol_code",
            source="status_synthesis.j2 §A1 / B2 のような記号は本文に書かない",
            forbids="文章セクションに A1 / B2 のような記号を書かない",
            gate="—",
            since="2026-08-15",
            sql=(
                f"SELECT count(*) n, count(*) FILTER (WHERE {_PROSE} ~ '\\m[A-Z][0-9]\\M') v"
                " FROM status_synthesis WHERE generated_at >= '2026-08-15'"
            ),
            min_sample=20,
        ),
        Prohibition(
            key="synthesis.latex",
            source="status_synthesis.j2 §LaTeX / 数式表記は全面禁止",
            forbids="LaTeX / 数式表記を出力しない",
            gate="—",
            since="2026-08-15",
            sql=(
                f"SELECT count(*) n, count(*) FILTER (WHERE {_PROSE} ~ "
                r"'\\frac|\\times|\$\$|\\\(') v"
                " FROM status_synthesis WHERE generated_at >= '2026-08-15'"
            ),
            min_sample=20,
        ),
        Prohibition(
            key="malware_family.generic_word",
            source="判定基準 malware_families",
            forbids="カテゴリ語 (ransomware / infostealer 等) を family 名にしない",
            gate="malware_aliases.yaml の drop (2026-06-20)",
            since="2026-06-20",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE lower(trim(value)) IN"
                f" ({generic})) v FROM article_entities WHERE entity_type='malware_family'"
                " AND created_at >= '2026-06-20'"
            ),
            min_sample=200,
        ),
        Prohibition(
            key="victim_org.vendor",
            source="判定基準 victim_orgs ⚠含めない①",
            forbids="ベンダ / ソフトメーカーを被害組織にしない",
            gate="victim_org_filter (2026-08-19)",
            since="2026-08-19",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE lower(trim(ae.value)) IN"
                f" ({vendors}) AND (a.category IS NULL OR a.category NOT IN ({protected}))) v"
                " FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id"
                " WHERE ae.entity_type='victim_org' AND ae.created_at >= '2026-08-19'"
            ),
            min_sample=100,
        ),
        Prohibition(
            key="ioc.source_reference",
            source="判定基準 iocs",
            forbids="出典系の URL / ドメインを IOC にしない",
            gate="ioc_source_filter (2026-08-19)",
            since="2026-08-19",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE"
                " lower(regexp_replace(coalesce(substring(value from '://([^/]+)'), value),"
                f" '^www\\.', '')) IN ({host_list})) v"
                " FROM article_entities WHERE entity_type IN ('ioc_domain','ioc_url')"
                " AND created_at >= '2026-08-19'"
            ),
            min_sample=50,
        ),
        Prohibition(
            key="tool.ai_platform",
            source="判定基準 tools ⚠絶対に含めない②",
            forbids="AI モデル / プラットフォーム名を攻撃ツールにしない",
            gate="ai_platform_drop (2026-08-19)",
            since="2026-08-19",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE lower(trim(value)) ~"
                f" '{ai_regex}') v FROM article_entities WHERE entity_type='tool'"
                " AND created_at >= '2026-08-19'"
            ),
            min_sample=100,
        ),
        Prohibition(
            key="victim_org.non_cyber_category",
            source="判定基準 victim_orgs (research/policy/geopolitical では原則 [])",
            forbids="非サイバーカテゴリの記事に victim_org を付けない",
            # ⚠ 関門を意図的に置いていない (2026-08-19 判断): サンプル 25 件目視の中身は
            # 制裁対象・監査対象大学・ドローン攻撃先などで、entity 情報としては実在の価値が
            # ある。地図・被害国 KPI は消費側が CYBER_ATTACK_EVENTS で絞っており守られて
            # いる。この行の VIOLATED は「rubric の指示と LLM の実挙動の乖離」の観測で、
            # 対処は 関門化 or rubric 側の緩和 のどちらか (利用者判断待ち)。
            gate="— (消費側 filter で地図は保護済み。関門化 or rubric 緩和は判断待ち)",
            since="2026-08-19",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE EXISTS ("
                "SELECT 1 FROM article_entities ae WHERE ae.article_id=a.article_id"
                " AND ae.entity_type='victim_org')) v"
                " FROM articles a WHERE a.category IN ('geopolitical','policy','research')"
                " AND a.created_at >= '2026-08-19'"
            ),
            min_sample=100,
        ),
        Prohibition(
            key="victim_sector.non_cyber_category",
            source="判定基準 victim_sector (research/policy/geopolitical は原則 null)",
            forbids="非サイバーカテゴリの記事に victim_sector を付けない",
            gate="— (victim_org.non_cyber_category と同じ判断待ち)",
            since="2026-08-19",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE victim_sector_canonical IS NOT NULL"
                " AND victim_sector_canonical NOT IN ('', 'uncategorized')) v"
                " FROM articles WHERE category IN ('geopolitical','policy','research')"
                " AND created_at >= '2026-08-19'"
            ),
            min_sample=100,
        ),
        Prohibition(
            key="involved_country.cyber_category",
            source="判定基準 involved_countries (サイバー事案では [])",
            forbids="サイバー事案の記事に involved_country を付けない (victim_country を使う)",
            gate="—",
            # 2026-08-18 = required 化 + rubric v4 で involved_countries が実供給され始めた日
            since="2026-08-18",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE EXISTS ("
                "SELECT 1 FROM article_entities ae WHERE ae.article_id=a.article_id"
                " AND ae.entity_type='involved_country')) v"
                " FROM articles a WHERE a.category IN"
                " ('breach','incident','apt','malware','vulnerability','advisory','apt_leak')"
                " AND a.created_at >= '2026-08-18'"
            ),
            min_sample=200,
        ),
        Prohibition(
            key="synthesis.alternatives_empty",
            source="status_synthesis.j2 §tradecraft (対立仮説を 1-2 個)",
            forbids="tradecraft.alternatives を空配列にしない",
            gate="—",
            since="2026-08-15",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE tradecraft IS NULL OR"
                " jsonb_array_length(coalesce(tradecraft->'alternatives','[]'::jsonb))=0) v"
                " FROM status_synthesis WHERE generated_at >= '2026-08-15'"
            ),
            min_sample=10,
        ),
        Prohibition(
            key="grounded.japanese_only",
            source="ground_ach.j2 / ground_incremental.j2 (すべて日本語で書く)",
            forbids="assumptions / missing / indicators に英語断片を書かない",
            gate="—",
            since="2026-08-15",
            sql=(
                "WITH elems AS ("
                "SELECT jsonb_array_elements_text(assumptions::jsonb) txt"
                " FROM situation_revisions WHERE assumptions<>'[]' AND created_at >= '2026-08-15'"
                " UNION ALL SELECT jsonb_array_elements_text(indicators::jsonb)"
                " FROM situation_revisions WHERE indicators<>'[]' AND created_at >= '2026-08-15'"
                " UNION ALL SELECT jsonb_array_elements_text(missing::jsonb)"
                " FROM situation_revisions WHERE missing<>'[]' AND created_at >= '2026-08-15') "
                "SELECT count(*) n, count(*) FILTER"
                " (WHERE length(txt)>12 AND txt !~ '[぀-ヿ一-鿿]') v FROM elems"
            ),
            min_sample=100,
        ),
        Prohibition(
            key="summary.extreme_length",
            source="判定基準 summary (250-500 字)",
            forbids="summary が 1000 字を超えない (JSON 流入等の破損検知)",
            # 250-500 字の遵守そのものは測らない (全期間 25.8% が範囲外 = 指示が実態と
            # 乖離しており、そのまま載せると常時 VIOLATED の狼少年になる)。1000 字超は
            # 過去に「LLM が閉じ損ねた JSON の自由記述流入」(599d81a) で実際に起きた
            # 破局の型なので、その再発だけを検知する。
            gate="599d81a (JSON 尾部の遮断) が主因を止めた後の再発検知",
            since="2026-08-18",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE char_length(summary) > 1000) v"
                " FROM articles WHERE summary IS NOT NULL AND summary <> ''"
                " AND created_at >= '2026-08-18'"
            ),
            min_sample=200,
        ),
    ]


def _verdict(n: int, v: int, min_sample: int) -> str:
    if v > 0:
        return STATUS_VIOLATED
    return STATUS_OK if n >= min_sample else STATUS_INCONCLUSIVE


def main() -> int:
    repo = RunHistoryRepository()
    rows: list[tuple[Prohibition, int, int, str]] = []
    with repo._connect() as con:  # noqa: SLF001 — 監査 script
        for check in _checks():
            r = con.execute(check.sql).fetchone()
            n, v = (int(r["n"]), int(r["v"])) if r is not None else (0, 0)
            rows.append((check, n, v, _verdict(n, v, check.min_sample)))

    print("\n=== プロンプト禁止事項の遵守状況 (機械判定できるものだけ) ===\n")
    print(f"{'禁止':38} {'関門':34} {'母数':>6} {'違反':>5}  判定")
    print("-" * 100)
    for check, n, v, verdict in rows:
        print(f"{check.forbids[:37]:38} {check.gate[:33]:34} {n:>6} {v:>5}  {verdict}")
    print("\n出所:")
    for check, _n, _v, _s in rows:
        print(f"  {check.key:28} {check.source} (since {check.since})")

    if any(s == STATUS_VIOLATED for *_x, s in rows):
        status = STATUS_VIOLATED
    elif any(s == STATUS_INCONCLUSIVE for *_x, s in rows):
        status = STATUS_INCONCLUSIVE
    else:
        status = STATUS_OK
    print(f"\n総合: {status}")
    return _EXIT[status]


if __name__ == "__main__":
    sys.exit(main())
