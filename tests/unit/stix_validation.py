"""STIX 2.1 の準拠をテストで確かめる補助 (OASIS stix2-validator、2026-09-27)。

⚠ stix2-validator の wheel には JSON スキーマ本体 (OASIS cti-stix2-json-schemas) が入っていない。
``scripts/fetch_stix_schemas.sh`` が data/stix2-json-schemas に取得し、ここで検証器の
既定の場所 (``<validator>/schemas-2.1/schemas``) に見えるようにする。無ければテストを飛ばす。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "data" / "stix2-validator-root"


def assert_valid_stix(bundle: dict[str, Any]) -> None:
    """strict + 参照の実在 (enforce_refs) で検証し、エラーを全部並べて落とす。"""
    if not (_ROOT / "schemas-2.1" / "schemas").is_dir():
        pytest.skip("STIX の JSON スキーマが無い (scripts/fetch_stix_schemas.sh で取得)")
    import stix2validator.validator as validator
    from stix2validator import ValidationOptions, validate_string

    # 検証器は ``dirname(__file__)/schemas-2.1/schemas`` を既定の場所として読む
    validator.__file__ = str(_ROOT / "validator.py")
    result = validate_string(
        json.dumps(bundle),
        # 302 (外部参照の URL に中身のハッシュを付ける SHOULD) だけ外す — 取得先の中身の
        # ハッシュは実務上付けられず、MITRE ATT&CK 自身の STIX も付けていない
        ValidationOptions(version="2.1", strict=True, enforce_refs=True, disabled=["302"]),
    )
    messages = [str(getattr(e, "message", e)) for e in (result.errors or [])]
    messages += ["W: " + str(getattr(w, "message", w)) for w in (result.warnings or [])]
    assert result.is_valid and not messages, "\n".join(messages)


def assert_extensions_match_schema(bundle: dict[str, Any]) -> None:
    """kuebiko 拡張の値が docs/stix/kuebiko-extension.schema.json に合うか。

    スキーマは additionalProperties=false — 書き出しに属性を足したらスキーマも更新する。
    """
    import jsonschema

    from src.cti.stix.core import EXTENSION_ID

    schema_path = Path(__file__).resolve().parents[2] / "docs/stix/kuebiko-extension.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for obj in bundle["objects"]:
        ext = (obj.get("extensions") or {}).get(EXTENSION_ID)
        if ext is not None:
            jsonschema.validate(ext, schema)
