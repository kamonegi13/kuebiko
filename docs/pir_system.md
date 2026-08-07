# PIR (Priority Intelligence Requirements) System

CTI doctrine の中心概念 PIR を tool の first-class entity として扱うシステム。
`config/pir.yaml` を single source of truth とし、triage / routing / synthesis /
Spotlight を PIR-driven にする。

## 設計原則

1. **PIR is canonical intent**: description (自然文) が user の意図、structured
   fields は LLM compile された中間表現。description 編集で再 compile 可能。
2. **Behavior preservation**: migration 直後は全 PIR が `target_channel: auto` /
   `target_importance: auto` で挿入され、既存挙動 (R1-R5b routing、hardcoded
   triage prompt) が保たれる。user が編集して explicit channel 設定をして
   初めて PIR-driven 挙動が活性化。
3. **Shadow mode**: 期間限定 / tag / weak/exclude signals / 自動提案 etc. の
   高度機能は UI のみで入力可、logic は未注入 (観察期間で必要性判断)。

## アーキテクチャ

```
config/pir.yaml (yaml SSoT)
   │
   ├─ src/pir/loader.py        yaml ↔ Pydantic (PirConfig, Pir)
   ├─ src/pir/models.py        schema (Level 2 + Shadow fields)
   ├─ src/pir/compiler.py      LLM 経由 description → structured fields
   ├─ src/pir/evaluator.py     article × PIR の match logic (preview / KPI 共有)
   ├─ src/pir/integration.py   triage/routing/synthesis への注入 helper
   │
   ├→ src/tools/article_triage.py: PIR-driven prompt template
   ├→ src/cti/router.py: R0 (PIR override) 追加
   ├→ src/synthesis/generator.py: prompt に pir_context 注入
   │
   ├─ src/ui/api/pir.py        CRUD + KPI + preview + compile API
   └─ frontend/src/pages/Pir*.tsx  一覧 / 詳細 / 編集 + Dashboard widget
```

## yaml schema (Level 2 + Shadow)

```yaml
version: 1
mission: 日本の CTI 担当者として、日本標的の脅威を漏らさず検知する
priorities:
  - id: pir_china_apt                       # 一意 ID
    title: 中国系 APT 動向                  # 表示用
    description: |                          # canonical intent (LLM compile の元)
      中国系 APT (Volt Typhoon, ...) の動向を最優先で検知。
    enabled: true                           # on/off

    # Level 2 main: filter
    strong_signals:
      keywords: []
      actors: [Volt Typhoon, APT41, ...]
      sectors: []
      countries: [CN]
      feed_titles: []

    # Level 2: routing (auto = 既存 R1-R5b に委ねる)
    target_importance: high                 # high | medium | low | auto
    target_channel: auto                    # alert | brief | watch | japan_watch | grok_daily | auto

    # Level 2: Spotlight
    spotlight:
      enabled: false                        # true で自動 Spotlight 生成
      title: "🇨🇳 中国 APT 動向"
      window: weekly                        # daily | weekly | monthly

    # Shadow F1: 期間限定 (UI 入力可、logic 未注入)
    valid_from: null
    valid_until: null

    # Shadow F2: tag
    tags: []

    # Shadow F3: weak / exclude signals
    weak_signals: { keywords: [], actors: [], ... }
    exclude_signals: { keywords: [], article_types: [] }

    # 自動メタデータ
    metadata:
      created_at: ...
      updated_at: ...
      migrated_from: article_triage.py:hardcoded:pir_china_apt
      approved_by_user: false               # false = UI 上で「承認待ち」banner
      rationale: ...                        # LLM compile の理由
```

## 運用 flow

### 新規 PIR 作成 (UI)

```
/app/pir/edit
  Step 1: description 入力 (Level 1、自然文)
  Step 2: [🤖 AI で構造化] click
  Step 3: LLM (gemma4:26b) が 10-60 秒で structured 生成
  Step 4: 6 カテゴリ別に提示 (検索語彙 / 攻撃主体 / 業界 / 国 / 情報源 / Spotlight)
  Step 5: user が review / chip 追加削除 / confidence dot で確信度確認
  Step 6: [👀 Preview] click で過去 7d match 件数 + サンプル表示
  Step 7: [Save] → /app/pir/{id} へ遷移
```

### 既存 hardcoded PIR の migration

```bash
# preview (実行されない)
uv run python scripts/migrate_existing_pir.py

# 本実行 (config/pir.yaml に書込み、既存 id は skip)
uv run python scripts/migrate_existing_pir.py --apply
```

13 high + 4 medium = 17 PIR が draft として投入され、UI で順次 [承認] 可能。
全 PIR は `target_channel: auto` なので tool 挙動は変わらない。

### Behavior preservation verification

```bash
uv run python scripts/verify_pir_migration.py            # quick (30 件)
uv run python scripts/verify_pir_migration.py --n 300    # 本格 (300 件)
```

検証基準:
- 全体 agreement rate: >= 90%
- high → low の flip: 0 件 (許容ゼロ)
- medium → low の flip: <= 2%
- JP feeds の medium+ 維持率: >= 95%
- 重要 actor の high 維持率: >= 95%

## 緊急 rollback

PIR-driven triage に問題が起きた場合の immediate rollback:

```bash
# .env or container env で設定
PIR_DRIVEN_TRIAGE=0
PIR_DRIVEN_ROUTING=0

# container 再起動で legacy hardcoded prompt に完全 fallback
docker compose up -d
```

`article_triage.py._build_prompt_legacy_hardcoded()` が常に保持されているため、
即座に元の挙動に戻せる。1 ヶ月安定運用後に legacy 関数も削除可能。

## Shadow features の観察結果 (2026-07-23 クローズ)

観察期間 (~2ヶ月) の結論: **F1/F2/F3 は利用 0 件で撤去** (2026-07-23)。弱補強は
条件ツリーの keyword AND 枝、除外は not 節が同じ意図をより正確に表現する
([pir_authoring_unification_design.md](pir_authoring_unification_design.md))。
過去データの残存キーは `loader.strip_legacy_pir_keys` が読み捨てる。

| F# | 機能 | 結論 |
|---|---|---|
| F1 | valid_from / valid_until | 撤去 (設定 0 件) |
| F2 | tags | 撤去 (設定 0 件、21 PIR 規模で grouping 需要なし) |
| F3 | weak / exclude signals | 撤去 (設定 0 件、条件ツリーで表現) |
| F4 | coverage time series | KPI (7d/30d + Coverage widget) として実装済 |
| F5 | 自動 PIR 提案 (stub) | 据え置き (発見支援の査定 2026-06-14 に整合) |
| F6 | PIR 重複検知 (未実装) | 据え置き (実害未観測) |

## 関連

- CLAUDE.md §13 "PIR-driven architecture"
- [pir_signal_first_matching_design.md](pir_signal_first_matching_design.md) —
  照合の signal-first 再設計 (2026-07-22〜23。keyword OR → 意味プロパティ述語ツリー)
- [pir_concept_llm_judge_design.md](pir_concept_llm_judge_design.md) —
  概念 PIR の LLM 主題判定層 (2026-07-23。PIR とは何か / 3 層モデル / 検証記録)
- memory/cti_pir.md (deprecated、本システムに移行済)
- memory/jp_collection_gap_root_cause.md (この設計の前提となった問題)
