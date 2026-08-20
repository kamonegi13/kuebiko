"""IoC LLM verifier (Phase 5T-N)。

regex で抽出した IoC 候補を LLM に文脈付きで verify させ、
誤抽出 (version 番号 / library 名 / 良性ドメイン / git commit 等) を drop する。

設計:
    - Layer 1 (ioc_extractor.regex) で大半の FP は弾く
    - Layer 2 (本 module) は context-aware な残漏れを LLM 文脈理解で除去
    - 失敗時は regex 結果をそのまま返す (graceful degradation)
    - 全 IoC を 1 つの LLM プロンプトでバッチ verify (per-article 1 call)
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.cti.ioc_extractor import ExtractedIocs
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 1 候補あたりの context 切り出し文字数 (前後合わせて)
_CONTEXT_WINDOW = 80
# 1 回の LLM verify 呼出に載せる候補数 (プロンプト肥大化を避ける chunk サイズ)。
# Q2 (2026-06-16): 旧実装はこれを超える候補を未検証で keep していたが、URL 多数の
# advisory 記事で引用 URL が verify を逃れる原因だった。現在は全候補をこのサイズで
# chunk し全件判定する (cap でなく chunk サイズ)。
_VERIFY_CHUNK_SIZE = 40
# LLM 出力 token 上限
_VERIFY_MAX_TOKENS = 2000
_VERIFY_TEMPERATURE = 0.0  # 決定的にしたいので低 temperature
# CVE 候補の LLM verify 上限 (2026-08-01)。CERT-FR 等の patch-roundup advisory は
# CVE 参照リストを数百件含み、全件 chunk verify だと 1 記事 10+ LLM 呼出 (実測 6-8 分、
# run 3520 の 1800s timeout の主因) になる。数十件を超える CVE 列挙は構造的に
# 「参照リスト」であり LLM 判定も全件 drop に収束する (実測) ため、cap 超過分は LLM を
# 呼ばず決定論的に drop する。Q2 (2026-06-16) の禁止事項は「未検証 **keep** で FP leak」
# であり、drop 方向の cap は矛盾しない。cve 以外の型 (hash/ip/domain) は IoC dump
# レポートで数百件が正当にあり得るため cap しない。env IOC_CVE_VERIFY_CAP で override 可。
_CVE_VERIFY_CAP_DEFAULT = 50


def _cve_verify_cap() -> int:
    """CVE verify cap を解決する (env override → 既定値)。"""
    raw = os.environ.get("IOC_CVE_VERIFY_CAP", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return _CVE_VERIFY_CAP_DEFAULT
        if value > 0:
            return value
    return _CVE_VERIFY_CAP_DEFAULT


# ---------- LLM 出力スキーマ ----------


_Action = Literal["keep", "drop"]


class IocDecision(BaseModel):
    """1 候補に対する LLM 判定。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(ge=0)
    action: _Action
    reason: str = Field(default="", max_length=200)


class IocVerifyOutput(BaseModel):
    """LLM verifier の出力全体。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    decisions: list[IocDecision] = Field(default_factory=list)


# ---------- public API ----------


async def verify_iocs_with_llm(
    extracted: ExtractedIocs,
    article_body: str,
    *,
    llm: LLMClient,
    article_id: str = "",
) -> ExtractedIocs:
    """各 IoC 候補に文脈を付けて LLM に keep/drop 判定させ、誤抽出を drop。

    Args:
        extracted: Layer 1 (regex) で抽出した IoC
        article_body: 文脈抽出用の元テキスト
        llm: LLM クライアント
        article_id: log 用 (optional)

    Returns:
        Layer 2 verify 後の IoC (drop 判定が出たものは除外)。LLM 失敗時は
        extracted をそのまま返す。
    """
    # 巨大 CVE 参照リスト (patch-roundup advisory) の verify 暴走防止。
    # 超過分は LLM を呼ばず drop する (先頭 = 文書内出現順で本題の CVE を優先)。
    cap = _cve_verify_cap()
    if len(extracted.cves) > cap:
        _log.info(
            "ioc_cve_verify_capped",
            article_id=article_id,
            total_cves=len(extracted.cves),
            kept=cap,
        )
        extracted = extracted.model_copy(update={"cves": extracted.cves[:cap]})

    candidates = _flatten_with_context(extracted, article_body)
    if not candidates:
        return extracted

    # Q2 (2026-06-16): 全候補を _VERIFY_CHUNK_SIZE 件ずつ chunk して **全件** LLM 判定する。
    # 旧実装は cap 超過分を未検証 keep し、URL 多数の advisory 記事で引用 URL が verify を
    # 逃れていた。chunk 化でプロンプト肥大を避けつつ取りこぼしを無くす (ローカル Ollama・
    # 金銭コスト 0、増分は >chunk の稀な記事で数 LLM 呼出のみ)。
    surviving: list[dict[str, str]] = []
    for start in range(0, len(candidates), _VERIFY_CHUNK_SIZE):
        batch = candidates[start : start + _VERIFY_CHUNK_SIZE]
        drop_ids = await _verify_batch(batch, llm=llm, article_id=article_id)
        surviving.extend(t for i, t in enumerate(batch) if i not in drop_ids)

    return _reconstruct_extracted(extracted, surviving)


async def _verify_batch(
    targets: list[dict[str, str]],
    *,
    llm: LLMClient,
    article_id: str,
) -> set[int]:
    """1 batch を LLM 判定し drop すべき batch-local index 集合を返す。

    LLM 失敗時は空集合 (= その batch は全 keep) で graceful degradation。
    """
    prompt = _build_prompt(targets)
    try:
        output = await llm.generate_structured(
            prompt,
            schema=IocVerifyOutput,
            temperature=_VERIFY_TEMPERATURE,
            max_tokens=_VERIFY_MAX_TOKENS,
            think=False,
        )
        decisions = list(getattr(output, "decisions", []))
    except Exception as e:  # noqa: BLE001
        _log.warning("ioc_verifier_failed", article_id=article_id, error=str(e))
        return set()

    drop_ids: set[int] = set()
    for d in decisions:
        if d.action == "drop" and 0 <= d.id < len(targets):
            drop_ids.add(d.id)
            t = targets[d.id]
            _log.info(
                "ioc_dropped_by_llm",
                article_id=article_id,
                ioc_type=t["type"],
                value=t["value"],
                reason=d.reason[:120],
            )
    return drop_ids


# ---------- internal helpers ----------


def _flatten_with_context(extracted: ExtractedIocs, body: str) -> list[dict[str, str]]:
    """ExtractedIocs を per-IoC の (type, value, context) の dict list に展開。"""
    out: list[dict[str, str]] = []
    for type_name, values in (
        ("ipv4", extracted.ipv4),
        ("ipv6", extracted.ipv6),
        ("domain", extracted.domains),
        ("url", extracted.urls),
        ("md5", extracted.md5),
        ("sha1", extracted.sha1),
        ("sha256", extracted.sha256),
        ("email", extracted.emails),
        ("cve", extracted.cves),
        ("mitre_tech", extracted.mitre_techniques),
        ("mitre_group", extracted.mitre_groups),
    ):
        for v in values:
            ctx = _find_context(body, v, _CONTEXT_WINDOW)
            out.append({"type": type_name, "value": v, "context": ctx})
    return out


def _find_context(body: str, needle: str, window: int) -> str:
    """body 中の最初の needle 出現位置の前後 window 文字を返す。"""
    if not body or not needle:
        return ""
    try:
        idx = body.find(needle)
    except (TypeError, ValueError):
        return ""
    if idx < 0:
        return ""
    s = max(0, idx - window)
    e = min(len(body), idx + len(needle) + window)
    return body[s:e].replace("\n", " ").strip()


# ---------- プロンプトの層分け (python_block kind、2026-08-21) ----------
#
# .j2 と違い本関数は Python コードが構造を所有する動的プロンプト (候補リストが実行時に
# 決まるため Jinja ループで表現しない設計判断)。編集可能な指示散文は 3 block に分離し
# DB (config_store) 所有にする (src/prompts/registry.py の PromptSpec、kind="python_block")。
# 見出し行・候補リストの動的生成・JSON 出力形式行は **code 所有のまま** (出力スキーマ
# IocVerifyOutput と結合しているため編集不可が正しい)。
#
# slot id (skeleton が無いため固定表で持つ — prompt_store.python_block_slot_ids が参照)。
SLOT_IDS: tuple[str, ...] = ("intro", "criteria", "rules")

_PROMPT_ID = "ioc_llm_verifier"

# 保存前検証・preview 用のサンプル候補 (各 type を 1 件ずつ含む非空ダミー)。実データを
# 使わず kuebiko.example 系のみ (§4: ログ・応答に本物の記事本文を持ち込まない)。
SAMPLE_TARGETS: list[dict[str, str]] = [
    {
        "type": "ipv4",
        "value": "198.51.100.23",
        "context": "C2 サーバへの接続が観測された 198.51.100.23 (render 検証用)。",
    },
    {
        "type": "ipv6",
        "value": "2001:db8::1",
        "context": "IPv6 経由の C2 通信 2001:db8::1 (render 検証用)。",
    },
    {
        "type": "domain",
        "value": "evil.kuebiko.example",
        "context": "マルウェアの配布元 evil.kuebiko.example (render 検証用)。",
    },
    {
        "type": "url",
        "value": "https://evil.kuebiko.example/payload.exe",
        "context": "初期アクセスの配布 URL (render 検証用)。",
    },
    {
        "type": "md5",
        "value": "abcd1234abcd1234abcd1234abcd1234",
        "context": "検体の MD5 ハッシュ (render 検証用)。",
    },
    {
        "type": "sha1",
        "value": "abcd1234abcd1234abcd1234abcd1234abcd1234",
        "context": "検体の SHA1 ハッシュ (render 検証用)。",
    },
    {
        "type": "sha256",
        "value": "abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234",
        "context": "検体の SHA256 ハッシュ (render 検証用)。",
    },
    {
        "type": "email",
        "value": "attacker@kuebiko.example",
        "context": "フィッシングの送信元アドレス (render 検証用)。",
    },
    {
        "type": "cve",
        "value": "CVE-2026-00001",
        "context": "悪用された脆弱性 (render 検証用)。",
    },
    {
        "type": "mitre_tech",
        "value": "T1566",
        "context": "観測された ATT&CK technique (render 検証用)。",
    },
    {
        "type": "mitre_group",
        "value": "G0001",
        "context": "帰属候補の ATT&CK group (render 検証用)。",
    },
]


def compose_from_blocks(blocks: Mapping[str, str], targets: list[dict[str, str]]) -> str:
    """blocks (DB 所有の指示散文) + targets (code 所有の動的候補リスト) からプロンプトを合成する。

    レイアウトは ``_build_prompt_legacy`` と同一。blocks が legacy 由来の verbatim テキスト
    なら結果は byte 一致する (golden 不変量、tests/unit で固定)。``blocks`` に
    ``SLOT_IDS`` の欠落があれば ``KeyError`` (呼び出し側の保存前検証 / fail-safe で拾う)。
    """
    lines = [
        blocks["intro"],
        "",
        "## 判定基準",
        blocks["criteria"],
        "",
        "## 候補リスト",
    ]
    for i, t in enumerate(targets):
        lines.append(
            f"{i}. type={t['type']} value={t['value']!r}\n   context: {t['context'][:200]!r}",
        )
    lines.extend(
        [
            "",
            "## 出力 (JSON のみ)",
            '{ "decisions": ['
            ' {"id": <候補番号>, "action": "keep"|"drop", "reason": "<短い理由>"},'
            " ... ] }",
            "",
            "ルール:",
            blocks["rules"],
        ],
    )
    return "\n".join(lines)


def _composed_prompt(targets: list[dict[str, str]]) -> str | None:
    """DB 編集層があればそれで合成する。flag OFF / 編集層不在 / 合成失敗は None (= legacy へ)。"""
    from src.prompts.prompt_store import build_python_block_source
    from src.prompts.registry import get_spec

    spec = get_spec(_PROMPT_ID)
    if spec is None:
        return None
    return build_python_block_source(spec, targets)


def _build_prompt(targets: list[dict[str, str]]) -> str:
    """LLM verifier 用プロンプトを組み立てる (DB 編集層 → legacy の順で fail-safe に解決)。

    graceful degradation (本 module の契約) を下層の例外処理に依存させない — 合成経路が
    どこで raise しても legacy (純 Python・I/O なし) に落ちる構造保証。per-article の
    hot path で prompt 組み立てを新しい crash 点にしないための関門。
    """
    try:
        composed = _composed_prompt(targets)
    except Exception as e:  # noqa: BLE001 — 合成失敗は契約上 legacy へ
        _log.warning("ioc_prompt_compose_failed", error=str(e))
        composed = None
    return composed if composed is not None else _build_prompt_legacy(targets)


def _build_prompt_legacy(targets: list[dict[str, str]]) -> str:
    """rollback 用の frozen 実装 (``IOC_LLM_VERIFIER_COMPOSER=0`` の実体)。

    ⚠ 編集禁止: DB 編集層 (blocks) の内容と独立させるため、この関数は他のどこからも
    import されない自己完結コピーとして維持する (block 方式の legacy .j2 と同じ役割)。
    """
    lines = [
        "あなたは CTI (Cyber Threat Intelligence) 分析の専門家です。",
        "以下は regex で抽出された IoC 候補と各々の周辺文脈です。",
        "**真の Indicator of Compromise (攻撃に関連する識別子) かどうか** を判定してください。",
        "",
        "## 判定基準",
        "- KEEP: 攻撃で実際に観測 / 使用された識別子 (C2 IP / malware hash / 攻撃 domain など)",
        "- DROP: 以下のような誤抽出",
        "  - ソフトウェアバージョン番号 (例: 6.2.10.0)",
        "  - ライブラリ / フレームワーク名 (例: Socket.IO, Node.js)",
        "  - Git commit hash (40-hex、github.com 等の文脈)",
        "  - ATT&CK 解説記事内の technique 参照 (実 incident でなく解説目的)",
        "  - 記事末尾の連絡先 email / 公式機関 contact",
        "  - 良性サービス (Cloudflare/Google DNS、AWS など) の悪用文脈なし言及",
        "  - 記事の引用 / 出典 URL",
        "  - RFC documentation IP (192.0.2.x / 198.51.100.x / 203.0.113.x)",
        "",
        "## 候補リスト",
    ]
    for i, t in enumerate(targets):
        lines.append(
            f"{i}. type={t['type']} value={t['value']!r}\n   context: {t['context'][:200]!r}",
        )
    lines.extend(
        [
            "",
            "## 出力 (JSON のみ)",
            '{ "decisions": ['
            ' {"id": <候補番号>, "action": "keep"|"drop", "reason": "<短い理由>"},'
            " ... ] }",
            "",
            "ルール:",
            "- 全候補 (上の番号 0〜N-1) について必ず判定を 1 件ずつ出力",
            "- 判定迷う場合は keep (false negative よりも false positive を許容)",
            "- reason は 50 字以内の短い日本語",
        ],
    )
    return "\n".join(lines)


def _reconstruct_extracted(
    original: ExtractedIocs,
    surviving: list[dict[str, str]],
) -> ExtractedIocs:
    """surviving の (type, value) から ExtractedIocs を再構築。順序は original を維持。"""
    surviving_set: dict[str, set[str]] = {}
    for s in surviving:
        surviving_set.setdefault(s["type"], set()).add(s["value"])

    def _keep(type_name: str, values: tuple[str, ...]) -> tuple[str, ...]:
        keep_set = surviving_set.get(type_name, set())
        return tuple(v for v in values if v in keep_set)

    # noqa: B008
    return ExtractedIocs(
        cves=_keep("cve", original.cves),
        mitre_techniques=_keep("mitre_tech", original.mitre_techniques),
        mitre_groups=_keep("mitre_group", original.mitre_groups),
        ipv4=_keep("ipv4", original.ipv4),
        ipv6=_keep("ipv6", original.ipv6),
        domains=_keep("domain", original.domains),
        urls=_keep("url", original.urls),
        md5=_keep("md5", original.md5),
        sha1=_keep("sha1", original.sha1),
        sha256=_keep("sha256", original.sha256),
        emails=_keep("email", original.emails),
    )


# 後方互換: 既存 test / コードが import するための named export
__all__ = [
    "SAMPLE_TARGETS",
    "SLOT_IDS",
    "IocDecision",
    "IocVerifyOutput",
    "compose_from_blocks",
    "verify_iocs_with_llm",
]


# silenced unused import warning (re used in module-level constants below in tests)
_ = re
