# STIX 2.1 書き出し (2026-09-27 再設計)

kuebiko の分析結果を STIX 2.1 (OASIS) に準拠した bundle で書き出す。受け手は OpenCTI / MISP / TIP 等。
実装は `src/cti/stix/`。準拠は OASIS 公式の検証器 (stix2-validator、strict・参照の実在) でテストが固定する。

## 書き出しの面

| 面 | 入口 | 中心 |
|---|---|---|
| 記事 1 件 | `GET /api/v1/articles/{id}/stix`・Discord 投稿の添付 | `report` |
| 事象 1 件 | `GET /api/v1/eventnews/{id}/stix` (事象ニュースの「STIX」) | kuebiko が書いた `report` (見出し・BLUF・要点、事実は出典の記事 id つきで拡張へ) → 構成記事の `report` |
| 台帳 1 件 | `GET /api/v1/situations/{id}/stix` (台帳画面の「STIX」) | `campaign` または `intrusion-set` + `grouping` + `note` |
| アクター 1 件 | `GET /api/v1/actors/{id}/stix` (アクター辞書の「STIX」) | `intrusion-set` / `threat-actor` + 主題の記事 (直近 180 日・最大 50 件) の `report` を `grouping` で束ねる |

## kuebiko → STIX の対応

| kuebiko | STIX 2.1 | 備考 |
|---|---|---|
| 記事 | `report` | `published` = 公開時刻、`external_references` = 出典 URL、`object_refs` = 記事から作った全オブジェクト |
| 辞書のアクター (group) | `intrusion-set` | 別名・ATT&CK の G 番号・`primary_motivation` (記事の intent を attack-motivation-ov へ) |
| 辞書の機関・請負 | `threat-actor` (`nation-state`) | group → 機関は `attributed-to` (STIX で intrusion-set が帰属できる先は threat-actor) |
| マルウェア / ツール | `malware` (is_family) / `tool` | `malware_types` = 辞書の種別 (malware_aliases.yaml の type) を malware-type-ov へ (rat→remote-access-trojan・botnet→bot・loader→downloader・infostealer→spyware) |
| TTP | `attack-pattern` | ATT&CK の `external_id` + `kill_chain_phases` (技術の辞書 data/cti/attack_techniques.json) |
| CVE | `vulnerability` | NVD への参照 |
| 被害組織 / 業種 / 国 | `identity` (organization) / `identity` (class) / `location` | 業種の STIX 語彙は `config/cti/victim_sectors.yaml` の `stix` が SSoT |
| IOC | `indicator` | STIX パターン (`valid_from` = 記事の公開時刻) |
| 台帳 (キャンペーン) | `campaign` | `first_seen` = 開設、`last_seen` = 最終証拠。鍵の group へ `attributed-to` |
| 台帳 (アクター追跡) | 鍵の group の `intrusion-set` | 持続的な主体 (期間の終わりが無い) |
| 台帳の束 | `grouping` (`suspicious-activity`) | 中心のオブジェクト + 証拠の記事の `report` |
| ACH の判断 | `note` + `confidence` | 先頭仮説・仮説ごとの整合/不整合・前提・欠けている証拠・兆候 |

## 決めごと

- **関係は主題アクターにだけ張る** (`uses` / `targets` / `indicates` / `attributed-to`)。言及だけのアクターは
  report の `object_refs` に載せるが手口・被害と結ばない。言及を帰属として外に出すと受け手の知識グラフを汚す。
- **関係の confidence は主題の判定経路から**: フィード・見出しの別名 = high (85)、LLM = その確度
  (STIX 仕様 Appendix A の High-Medium-Low 尺度: 85 / 50 / 15)。`indicates` は主題が 1 つに定まるときだけ。
- **ID は決定論** (uuid5 のハッシュを UUIDv4 の形に整える)。同じ対象は同じ ID になり、受け手で重複が畳まれる。
  参照系 (アクター・マルウェア・技術・CVE・組織・国) の `created` は固定値 — 同じ ID で `created` が違う版を作らない。
  関係は **主張した文脈 (記事・台帳) ごとに別の ID** — 同じ組でも確度や日時が違うため。
- **kuebiko 独自の属性は正式な拡張** (`extension-definition`, property-extension、
  スキーマ `docs/stix/kuebiko-extension.schema.json`)。`x_` 始まりの独自プロパティ (2.0 流) は使わない。
- **TLP:WHITE** (仕様の定義済みマーキング)。書き出すのは公開情報の要約。
- 証拠は評価済み (ACH が引用した) ものだけ。弱い証拠 (weak_at) は外れる。
- アクターの書き出しは LLM medium の主題を外す (精度 61%、国家の集計と同じ)。件数を確度に読み替えない —
  関係は記事ごとのまま (収集量は重要性でも確かさでもない)。

## 検証

- `scripts/fetch_stix_schemas.sh` が OASIS の JSON スキーマを data/ に取得する (stix2-validator の wheel に同梱されていない)。
- テスト: `tests/unit/test_stix_{objects,article,situation}.py`・`test_stix_from_briefing.py`。
  strict (SHOULD も違反扱い) + 参照の実在。外しているのは 302 (外部参照の URL に中身のハッシュ) だけ —
  取得先の中身のハッシュは実務上付けられず、MITRE ATT&CK 自身の STIX も付けていない。
- 拡張の値は JSON スキーマで検証する (`additionalProperties: false` — 属性を足したらスキーマも更新する)。
