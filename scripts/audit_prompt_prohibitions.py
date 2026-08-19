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

# 実測で tools に混入した「正規の AI プラットフォーム/モデル名」。
# ⚠ WormGPT / FraudGPT / PentestGPT は **攻撃ツールなので入れない** (悪性 LLM サービス)。
# ⚠ これは観測用リストであって関門ではない — 関門化するかは CTI 上の判断 (AI ツールの
#   悪用は実在の TTP なので、落とすと情報を失う) が要るため保留中。
_AI_PLATFORMS = (
    "claude",
    "claude code",
    "chatgpt",
    "gpt-4",
    "gpt-4o",
    "gpt4all",
    "gemini",
    "gemini cli",
    "google gemini cli",
    "ollama",
    "msty",
    "deepseek",
    "lm studio",
    "openai codex",
    "openai operator",
    "copilot",
    "github copilot",
    "midjourney",
    "stable diffusion",
    "comfyui",
    "lora",
    "perplexity",
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
    ai = _in_list(_AI_PLATFORMS)
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
            gate="— (関門化は未判断: AI ツールの悪用は実在の TTP)",
            since="2026-06-20",
            sql=(
                "SELECT count(*) n, count(*) FILTER (WHERE lower(trim(value)) IN"
                f" ({ai})) v FROM article_entities WHERE entity_type='tool'"
                " AND created_at >= '2026-06-20'"
            ),
            min_sample=100,
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
