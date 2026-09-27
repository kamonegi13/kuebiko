#!/bin/bash
# STIX 2.1 の公式 JSON スキーマ (OASIS cti-stix2-json-schemas) を取得する (テストの準拠確認用)。
# stix2-validator の wheel にはスキーマが入っていないため (2026-09-27)。data/ は gitignore。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d data/stix2-json-schemas ]; then
  git clone --depth 1 -q https://github.com/oasis-open/cti-stix2-json-schemas.git data/stix2-json-schemas
fi
mkdir -p data/stix2-validator-root
ln -sfn ../stix2-json-schemas data/stix2-validator-root/schemas-2.1
echo "ok: data/stix2-json-schemas ($(git -C data/stix2-json-schemas log -1 --format=%h))"
