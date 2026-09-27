"""STIX 2.1 書き出し (2026-09-27 再設計)。設計は docs/stix_export.md。

- ``core``: ID・時刻・作成者・TLP・kuebiko 拡張 (extension-definition)・確度の写像
- ``objects``: SDO / SRO の組み立て (intrusion-set / threat-actor / malware / ...)
- ``article``: 記事 1 件 → report を中心にした bundle
- ``situation``: 台帳 1 件 → campaign / grouping + ACH の note
"""
