"""ユーザ定義 LLM 接続先レジストリ (2026-07-24、外部プロバイダ一般化)。

OpenAI / Gemini / LM Studio / リモート Ollama 等の OpenAI 互換 API を
「接続先 (endpoint)」として登録し、``<接続先名>:<モデル>`` で参照可能にする。

- **定義 (name / base_url) は DB (config_store "llm_endpoints")** — UI 編集・版履歴。
- **API キーは .env (``LLM_ENDPOINT_KEY_<NAME>``)** — §4「認証情報は .env」
  (DB は版履歴 + pg_dump に残るため秘密を置かない。ANTHROPIC_API_KEY と同じ扱い)。
- anthropic / claudecode は専用アダプタの組み込み接続先 (このレジストリの対象外)。
- 登録済み接続先だけが UI に現れる (未登録ベンダーの設定欄は並ばない)。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

_log = get_logger(__name__)

LLM_ENDPOINTS_CONFIG_KEY = "llm_endpoints"

# 組み込み prefix・Ollama と衝突させないための予約名
RESERVED_ENDPOINT_NAMES = frozenset({"anthropic", "claudecode", "ollama", "local", "builtin"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")

# UI の「+ 接続先を追加」プリセット (テンプレでしかない — 登録するまで UI に現れない)
ENDPOINT_PRESETS: tuple[dict[str, str], ...] = (
    {"name": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {
        "name": "gemini",
        "label": "Google Gemini (OpenAI 互換)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    {
        "name": "lmstudio",
        "label": "LM Studio (ローカル)",
        "base_url": "http://host.docker.internal:1234/v1",
    },
    {
        "name": "ollama-remote",
        "label": "Ollama (リモート/クラウド)",
        "base_url": "http://<host>:11434/v1",
    },
    {"name": "custom", "label": "カスタム (OpenAI 互換)", "base_url": "https://"},
)


class LlmEndpoint(BaseModel):
    """1 つの OpenAI 互換接続先。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    base_url: str

    @property
    def key_env(self) -> str:
        """API キーの .env キー名 (レジストリ駆動 allowlist に載る)。"""
        return endpoint_key_env(self.name)


def endpoint_key_env(name: str) -> str:
    return "LLM_ENDPOINT_KEY_" + name.upper().replace("-", "_")


def validate_llm_endpoints(raw: object) -> list[str]:
    """保存前検証。エラー文字列 list (空 = OK)。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("endpoints"), list):
        return ["llm_endpoints は {endpoints: [...]} である必要があります"]
    errs: list[str] = []
    seen: set[str] = set()
    for i, e in enumerate(raw["endpoints"]):
        if not isinstance(e, dict):
            errs.append(f"#{i}: object である必要があります")
            continue
        name = str(e.get("name") or "").strip()
        base_url = str(e.get("base_url") or "").strip()
        if not _NAME_RE.match(name):
            errs.append(f"#{i}: 接続先名は英小文字始まり 2-20 字 (a-z0-9-) です: {name!r}")
        elif name in RESERVED_ENDPOINT_NAMES:
            errs.append(f"#{i}: '{name}' は予約名です")
        elif name in seen:
            errs.append(f"#{i}: 接続先名 '{name}' が重複しています")
        seen.add(name)
        if not (base_url.startswith(("http://", "https://")) and len(base_url) > 10):
            errs.append(f"#{i} ({name}): base_url が不正です (http(s)://... を指定)")
    return errs


# process 内キャッシュ (model_tiers と同パターン)。UI 保存後は invalidate。
_CACHE: dict[str, list[LlmEndpoint]] = {}


def invalidate_llm_endpoints_cache() -> None:
    _CACHE.clear()


def load_llm_endpoints(*, db_path: Path | None = None) -> list[LlmEndpoint]:
    """DB から接続先一覧を読む。未投入・障害時は空 (接続先なし = 従来挙動)。"""
    cache_key = str(db_path)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    result: list[LlmEndpoint] = []
    try:
        from src.storage.config_store import get_config

        raw: Any = get_config(LLM_ENDPOINTS_CONFIG_KEY, db_path=db_path)
        if isinstance(raw, dict) and isinstance(raw.get("endpoints"), list):
            for e in raw["endpoints"]:
                if isinstance(e, dict) and e.get("name") and e.get("base_url"):
                    result.append(
                        LlmEndpoint(
                            name=str(e["name"]).strip(),
                            base_url=str(e["base_url"]).strip().rstrip("/"),
                        )
                    )
    except Exception as e:  # noqa: BLE001 — DB 障害で LLM 構築を壊さない (接続先なし扱い)
        _log.warning("llm_endpoints_load_failed", error=str(e)[:200])
    _CACHE[cache_key] = result
    return result


def resolve_endpoint(name: str, *, db_path: Path | None = None) -> LlmEndpoint | None:
    for e in load_llm_endpoints(db_path=db_path):
        if e.name == name:
            return e
    return None


def endpoint_api_key(name: str, *, env_path: Path | str = ".env") -> str:
    """接続先の API キーを解決する (os.environ → .env の順。無ければ空 = キー不要接続先)。

    pydantic-settings は既知フィールドしか読まないため、動的キーは dotenv を直接読む。
    """
    key_name = endpoint_key_env(name)
    value = os.environ.get(key_name, "")
    if value:
        return value.strip()
    try:
        from dotenv import dotenv_values

        p = Path(env_path)
        if p.exists():
            return str(dotenv_values(p).get(key_name) or "").strip()
    except Exception as e:  # noqa: BLE001 — .env 読取失敗はキー無し扱い
        _log.warning("llm_endpoint_key_read_failed", endpoint=name, error=str(e)[:120])
    return ""
