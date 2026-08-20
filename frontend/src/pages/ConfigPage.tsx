import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Save } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi, type ModelTiersResponse, type TierMeta } from "../api/pages";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { ConnectionsPanel } from "./config/ConnectionsPanel";
import { FileGroupList } from "../components/FileGroupList";
import { ConfigHistoryView } from "./config/ConfigHistoryView";
import { AccessAuditCard } from "../components/AccessAuditCard";
import { HostWatchdogCard } from "../components/HostWatchdogCard";
import { SummarizerRubricEditor } from "./config/SummarizerRubricEditor";
import { BlockPromptEditor } from "./config/BlockPromptEditor";
import { promptBlocksApi, type ManagedPrompt, type ManagedPromptKind } from "../api/promptBlocks";

// summarizer プロンプトの層分け (WP3): 判定基準の SSoT は DB (rubric)。このパスだけ
// PromptsEditor が構造化エディタに分岐する (他ファイルは既存の raw textarea のまま)。

// プロンプト層分けの一般化 (2026-08-20): GET /api/v1/prompts/managed が返す kind は
// backend の内部語彙 (ComposerKind) そのままなので、UI では日本語に写像してから出す
// (生 enum 直接表示禁止)。
const MANAGED_KIND_LABEL: Record<ManagedPromptKind, string> = {
  field_rubric: "フィールド別",
  block: "ブロック",
  python_block: "ブロック (Python 所有)",
};

// 設定画面の整理 (2026-08-02): タブが **3 種類の異なるもの** を同列に並べていて
// 「ごちゃごちゃ」していた。種類で群に分け、群内は概念順に並べる:
//   【設定】   接続 / モデル / プロンプト / システム   … 値を決めるもの
//   【記録】   履歴・監査                              … 後から追跡するもの (設定ではない)
//   【上級】   設定ファイル (raw YAML)                 … エスケープハッチ。他タブと重複し、
//              2026-06-10 に「raw 直編集は不適当」と方針決定済みなので末尾へ隔離
// マッチリストは配信ルールの語彙なので情報フローへ移した (使う場所と設定する場所を一致させる)。
type ConfigTab = "connections" | "models" | "prompts" | "system" | "history" | "yaml";

const HASH_TABS: ConfigTab[] = ["models", "prompts", "system", "history", "yaml"];

// タブ表示 (群ごとに区切る)。ラベルは日本語で統一 (旧 "yaml"/"prompts" は英小文字だった)。
const TAB_GROUPS: { group: string; tabs: { id: ConfigTab; label: string }[] }[] = [
  {
    group: "設定",
    tabs: [
      { id: "connections", label: "接続" },
      { id: "models", label: "モデル" },
      { id: "prompts", label: "プロンプト" },
      { id: "system", label: "システム" },
    ],
  },
  { group: "記録", tabs: [{ id: "history", label: "履歴・監査" }] },
  { group: "上級", tabs: [{ id: "yaml", label: "設定ファイル" }] },
];

export function ConfigPage() {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();

  // URL hash で sub-tab state (旧専用ページからの redirect = #prompts / #match-lists / #history)
  // NOTE: hooks は read_only 分岐より前に全て呼ぶ (rules of hooks)
  // .env タブは廃止 (設定・死活の画面統合 P4)。既定タブは「接続」— 初期設定・復旧時に
  // 接続を持つ設定 (webhook / IMAP / LLM / tunnel) をワンストップで埋められる集約ビュー
  // (各対象画面と同一カード・同一 API の再掲。.env ファイル自体は保存層として存続)。
  const initialTab: ConfigTab =
    typeof window !== "undefined"
      ? (HASH_TABS.find((t) => window.location.hash === `#${t}`) ?? "connections")
      : "connections";
  const [tab, setTab] = useState<ConfigTab>(initialTab);

  if (read_only) {
    return (
      <div className={`${pageContainer("wide")} text-center`}>
        <h2 className="m-0 mb-4 text-xl font-bold text-fg tracking-tight">設定（閲覧のみ）</h2>
        <div className="bg-warning-soft border border-warning rounded-lg p-6 text-sm text-fg-muted">
          <p className="m-0 mb-2 inline-flex items-center gap-1"><AlertTriangle className="h-4 w-4" /> この画面は <strong className="text-fg">閲覧のみモード</strong> で動作しており、設定編集は無効化されています。</p>
          <p className="m-0">設定変更が必要な場合は、PC 上の通常の管理画面 (<code className="text-fg">http://127.0.0.1:8001/app/config</code>) からアクセスしてください。</p>
        </div>
      </div>
    );
  }

  const switchTab = (next: ConfigTab) => {
    setTab(next);
    if (typeof window !== "undefined") {
      history.replaceState(null, "", next === "connections" ? "/app/config" : `/app/config#${next}`);
    }
  };

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <h2 className="m-0 text-xl font-bold text-fg tracking-tight">設定</h2>

      <div className="flex items-center border-b border-border-subtle flex-wrap">
        {TAB_GROUPS.map((g, gi) => (
          <div key={g.group} className="flex items-center">
            {gi > 0 && <span className="mx-2 h-4 w-px bg-border-subtle" aria-hidden />}
            {g.tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => switchTab(t.id)}
                title={`${g.group}: ${t.label}`}
                className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
                  tab === t.id
                    ? "text-accent-hover border-accent font-semibold"
                    : "text-fg-muted hover:text-fg border-transparent"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        ))}
      </div>

      {tab === "connections" && <ConnectionsPanel onOpenModels={() => switchTab("models")} />}
      {tab === "yaml" && <YamlEditor qc={qc} />}
      {tab === "prompts" && <PromptsEditor qc={qc} />}
      {tab === "models" && <ModelTiersEditor qc={qc} />}
      {tab === "system" && <SystemEditor />}
      {tab === "history" && (
        <div className="space-y-4">
          {/* 「後から何が起きたか追う」記録を 1 か所に: 誰が入ったか (監査) と
              何を変えたか (設定履歴)。設定そのものではないので他タブと分けている。 */}
          <AccessAuditCard />
          <ConfigHistoryView />
        </div>
      )}
    </div>
  );
}

// システム設定 (設定・死活の画面統合 P3): 残余の .env キー (LOG_LEVEL / TIMEZONE) のみ。
// 接続系キーは各対象画面 (チャンネル=情報フロー / IMAP=購読ソース / LLM=モデルタブ) で管理し、
// .env ファイルは不可視の保存層として存続 (障害時はホストで直接編集可)。
function SystemEditor() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["config"],
    queryFn: () => pagesApi.config(),
  });
  const [logLevel, setLogLevel] = useState<string | null>(null);
  const [timezone, setTimezone] = useState<string | null>(null);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const currentLog = (data?.env_values["LOG_LEVEL"] || "INFO").toUpperCase();
  const currentTz = data?.env_values["TIMEZONE"] || "Asia/Tokyo";

  const save = useMutation({
    mutationFn: () => pagesApi.configSystemSave(logLevel ?? currentLog, timezone ?? currentTz),
    onSuccess: () => {
      setLogLevel(null);
      setTimezone(null);
      setMessage({ kind: "success", text: "保存しました" });
      qc.invalidateQueries({ queryKey: ["config"] });
      setTimeout(() => setMessage(null), 3000);
    },
    onError: (e: Error) => setMessage({ kind: "error", text: e.message }),
  });

  if (!data) return <div className="text-fg-muted text-sm">読み込み中...</div>;

  const dirty =
    (logLevel !== null && logLevel !== currentLog) || (timezone !== null && timezone !== currentTz);
  return (
    <div className="max-w-[720px] space-y-3">
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
        <h3 className="m-0 text-md font-semibold text-fg">システム</h3>
        <label className="block text-xs text-fg-subtle">
          ログレベル
          <select
            value={logLevel ?? currentLog}
            onChange={(e) => setLogLevel(e.target.value)}
            className="mt-0.5 block w-full rounded border border-border-subtle bg-surface-2 px-2 py-1.5 text-sm text-fg"
          >
            {["DEBUG", "INFO", "WARNING", "ERROR"].map((lv) => (
              <option key={lv} value={lv}>{lv}</option>
            ))}
          </select>
          <span className="mt-0.5 block text-[11px] text-fg-subtle">
            本番は INFO。保存後に起動するジョブ実行から反映 (アプリ本体のログ出力は再起動後)。
          </span>
        </label>
        <label className="block text-xs text-fg-subtle">
          タイムゾーン
          <input
            value={timezone ?? currentTz}
            onChange={(e) => setTimezone(e.target.value)}
            placeholder="Asia/Tokyo"
            className="mt-0.5 block w-full rounded border border-border-subtle bg-surface-2 px-2 py-1.5 font-mono text-sm text-fg"
          />
          <span className="mt-0.5 block text-[11px] text-fg-subtle">
            画面の時刻表示に即時反映。スケジューラの cron 解釈は Asia/Tokyo 固定でこの設定の影響を受けません。
          </span>
        </label>
        <div className="flex items-center gap-2">
          <button
            onClick={() => save.mutate()}
            disabled={!dirty || save.isPending}
            className="inline-flex items-center gap-1 bg-accent text-bg px-3 py-1 rounded font-semibold text-xs hover:bg-accent-hover disabled:opacity-40 transition-colors"
          >
            {save.isPending ? "保存中..." : <><Save className="h-3.5 w-3.5" /> 保存</>}
          </button>
          {message && (
            <span className={`text-xs px-2 py-0.5 rounded ${message.kind === "success" ? "bg-success-soft text-success" : "bg-critical-soft text-critical"}`}>
              {message.text}
            </span>
          )}
        </div>
      </div>
      {/* ホスト復旧 watchdog: macOS のスリープ復帰失敗を相手にする **この端末固有の設定**。
          外部サービスとの接続ではないので接続タブではなくここが正しい (2026-08-02 整理)。 */}
      <HostWatchdogCard readOnly={false} />

      <p className="m-0 text-[11px] text-fg-subtle">
        接続系の設定 (Discord webhook / Grok メール / LLM / モバイル公開) は「接続」タブ。
        配信ルール・チャンネルは情報フロー、ソースは購読ソース、ジョブは実行管理と、
        設定は対象画面の隣にも置いています (どちらで編集しても同じ保存先)。
      </p>
    </div>
  );
}

function YamlEditor({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  const { data } = useQuery({
    queryKey: ["config"],
    queryFn: () => pagesApi.config(),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const { data: yaml } = useQuery({
    queryKey: ["config-yaml", selected],
    queryFn: () => pagesApi.configYaml(selected!),
    enabled: !!selected,
  });

  useEffect(() => {
    if (yaml) {
      setContent(yaml.content);
      setDirty(false);
    }
  }, [yaml]);

  useEffect(() => {
    if (!selected && data?.yaml_files.length) setSelected(data.yaml_files[0]);
  }, [data, selected]);

  const save = useMutation({
    mutationFn: () => pagesApi.configYamlSave(selected!, content),
    onSuccess: () => {
      setMessage({ kind: "success", text: "保存しました" });
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["config-yaml", selected] });
      setTimeout(() => setMessage(null), 3000);
    },
    onError: (e: Error) => setMessage({ kind: "error", text: e.message }),
  });

  if (!data) return <div className="text-fg-muted text-sm">読み込み中...</div>;

  return (
    <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-4">
      <aside className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden h-[calc(100vh-15rem)] min-h-[24rem] overflow-y-auto">
        <FileGroupList
          groups={data.yaml_groups ?? []}
          selected={selected}
          onSelect={setSelected}
          stripPrefix="config/"
        />
      </aside>
      <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
        <div className="px-4 py-2.5 border-b border-border-subtle flex items-center justify-between">
          <div className="text-fg font-mono text-sm">{selected || "選択..."}</div>
          <div className="flex items-center gap-3">
            {message && <span className={`text-xs px-2 py-0.5 rounded ${message.kind === "success" ? "bg-success-soft text-success" : "bg-critical-soft text-critical"}`}>{message.text}</span>}
            <button onClick={() => save.mutate()} disabled={!dirty || save.isPending || !selected} className="inline-flex items-center gap-1 bg-accent text-bg px-4 py-1.5 rounded-md font-semibold text-sm hover:bg-accent-hover disabled:opacity-40 transition-colors">{save.isPending ? "保存中..." : <><Save className="h-4 w-4" /> 保存</>}</button>
          </div>
        </div>
        <textarea
          value={content}
          onChange={(e) => { setContent(e.target.value); setDirty(true); }}
          className="w-full h-[calc(100vh-18rem)] min-h-[21rem] bg-black/60 text-fg font-mono text-xs p-4 outline-none resize-none leading-relaxed"
          spellCheck={false}
        />
      </div>
    </div>
  );
}

// Phase Diamond verify: 旧 /app/prompts (PromptsPage) を Config の sub-tab として統合。
function PromptsEditor({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  // 選択モデル (2026-08-20 再設計): **左は「プロンプト 1 本 = 1 entry」の単一リスト**、
  // 右上に「編集 (カード) / 実ファイル (raw)」の切替。managed (編集層あり) の据置 .j2 は
  // 一覧に別 entry として出さず、raw モードで同じ選択から開く。
  const [selected, setSelected] = useState<string | null>(null); // 実ファイルの path (一覧の key)
  const [mode, setMode] = useState<"card" | "raw">("card");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const { data: list } = useQuery({
    queryKey: ["prompts-list"],
    queryFn: () => pagesApi.promptsList(),
  });
  const { data: managed } = useQuery({
    queryKey: ["prompts-managed"],
    queryFn: () => promptBlocksApi.managed(),
  });

  // 実ファイル path → managed entry (編集層を持つプロンプト) の対応。
  const managedByPath = useMemo(() => {
    const m = new Map<string, ManagedPrompt>();
    for (const p of managed?.prompts ?? []) m.set(p.legacy_path, p);
    return m;
  }, [managed]);

  const current = selected != null ? managedByPath.get(selected) ?? null : null;
  const showRawEditor = current == null || mode === "raw";
  // python_block (2026-08-21): .j2 skeleton を持たない Python 所有プロンプトは実ファイルが
  // prompts/**/*.j2 の一覧 (list.files) に現れない — allowlist (FileEditor) が .py を対象外に
  // しているため raw 編集自体が構造的に不可能。「実ファイル」切替を出さず banner に留める。
  const hasRawFile = current != null && (list?.files ?? []).includes(current.legacy_path);

  // list.groups (実ファイル一覧) に対応が無い managed entry (= python_block 系) を、
  // 専用の区分としてサイドバーへ追加する (実ファイルが無いプロンプトが選択肢から消えないため)。
  const codeOwnedGroup = useMemo(() => {
    const rawFiles = new Set(list?.files ?? []);
    const entries = (managed?.prompts ?? []).filter((m) => !rawFiles.has(m.legacy_path));
    if (entries.length === 0) return null;
    return { category: "解析ロジック (Python 所有)", files: entries };
  }, [managed, list]);

  const { data: file } = useQuery({
    queryKey: ["prompts-file", selected],
    queryFn: () => pagesApi.promptsFile(selected!),
    enabled: !!selected && showRawEditor,
  });

  useEffect(() => {
    if (file) {
      setContent(file.content);
      setDirty(false);
    }
  }, [file]);

  // 既定選択 = summarizer (編集層が本線)。managed が読めない異常時のみ raw 先頭。
  useEffect(() => {
    if (selected) return;
    if (managed?.prompts.length) setSelected(managed.prompts[0].legacy_path);
    else if (managed && list?.files.length) setSelected(list.files[0]);
  }, [managed, list, selected]);

  const select = (path: string) => {
    setSelected(path);
    setMode("card"); // 選び直しは常にカード (編集層) から。実ファイルは明示切替のみ
  };

  const save = useMutation({
    mutationFn: () => pagesApi.promptsSave(selected!, content),
    onSuccess: () => {
      setMessage({ kind: "success", text: "保存しました" });
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["prompts-file", selected] });
      setTimeout(() => setMessage(null), 3000);
    },
    onError: (e: Error) => setMessage({ kind: "error", text: e.message }),
  });

  if (!list) return <div className="text-fg-muted text-sm">読み込み中...</div>;

  return (
    <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4">
      {/* 単一リスト: file_catalog の区分見出し + 2 行 entry。managed は編集層のタイトルで表示 */}
      <aside className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden h-[calc(100vh-13rem)] min-h-[24rem] overflow-y-auto">
        {(list.groups ?? []).map((g) => (
          <div key={g.category}>
            <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider font-semibold text-fg-subtle bg-surface-2 border-b border-border-subtle sticky top-0">
              {g.category}
            </div>
            {g.files.map((f) => {
              const m = managedByPath.get(f.path);
              return (
                <div
                  key={f.path}
                  onClick={() => select(f.path)}
                  title={f.description}
                  className={`px-3 py-2 cursor-pointer border-b border-border-subtle ${
                    selected === f.path ? "bg-accent-subtle text-accent-hover" : "hover:bg-surface-2 text-fg"
                  }`}
                >
                  <div className="text-sm leading-tight">{m ? m.title : f.label}</div>
                  <div className="font-mono text-[10px] text-fg-subtle leading-tight mt-0.5">
                    {m ? m.prompt_id : f.path.replace(/^prompts\//, "")}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
        {/* 実ファイルを持たない managed prompt (python_block 系) 専用の区分 */}
        {codeOwnedGroup && (
          <div key={codeOwnedGroup.category}>
            <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider font-semibold text-fg-subtle bg-surface-2 border-b border-border-subtle sticky top-0">
              {codeOwnedGroup.category}
            </div>
            {codeOwnedGroup.files.map((m) => (
              <div
                key={m.legacy_path}
                onClick={() => select(m.legacy_path)}
                title={`${m.prompt_id} (実ファイルは無し — 構造は Python コードが所有)`}
                className={`px-3 py-2 cursor-pointer border-b border-border-subtle ${
                  selected === m.legacy_path ? "bg-accent-subtle text-accent-hover" : "hover:bg-surface-2 text-fg"
                }`}
              >
                <div className="text-sm leading-tight">{m.title}</div>
                <div className="font-mono text-[10px] text-fg-subtle leading-tight mt-0.5">{m.prompt_id}</div>
              </div>
            ))}
          </div>
        )}
      </aside>

      <div className="min-w-0 space-y-3">
        {/* 編集層を持つプロンプトのみ、右上でカード/raw を切り替える */}
        {current && (
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex rounded-md border border-border-subtle overflow-hidden">
              {(hasRawFile ? (["card", "raw"] as const) : (["card"] as const)).map((m2) => (
                <button
                  key={m2}
                  onClick={() => setMode(m2)}
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                    mode === m2 ? "bg-accent-subtle text-accent-hover" : "text-fg-muted hover:bg-surface-2 hover:text-fg"
                  }`}
                >
                  {m2 === "card" ? "編集 (カード)" : "実ファイル (raw)"}
                </button>
              ))}
            </div>
            <span className="rounded-sm bg-surface-3 px-1.5 py-px text-[10px] text-fg-subtle">
              {MANAGED_KIND_LABEL[current.kind]}
            </span>
          </div>
        )}

        {current && mode === "card" ? (
          current.kind === "field_rubric" ? (
            <SummarizerRubricEditor />
          ) : (
            <BlockPromptEditor promptId={current.prompt_id} />
          )
        ) : (
          <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
            {current && (
              <div className="border-b border-warning bg-warning-soft px-4 py-2 text-xs text-warning">
                rollback 用の据置ファイルです — 編集層が有効な間、ここでの編集は運用に効きません。
                運用中の変更は「編集 (カード)」から行ってください。
              </div>
            )}
            <div className="px-4 py-2.5 border-b border-border-subtle flex items-center justify-between">
              <div className="text-fg font-mono text-sm">{selected || "ファイルを選択..."}</div>
              <div className="flex items-center gap-3">
                {file?.backup_exists && <span className="text-fg-subtle text-xs">バックアップあり</span>}
                {message && (
                  <span className={`text-xs px-2 py-0.5 rounded ${message.kind === "success" ? "bg-success-soft text-success" : "bg-critical-soft text-critical"}`}>{message.text}</span>
                )}
                <button
                  onClick={() => save.mutate()}
                  disabled={!dirty || save.isPending || !selected}
                  className="inline-flex items-center gap-1 bg-accent text-bg px-4 py-1.5 rounded-md font-semibold text-sm hover:bg-accent-hover disabled:opacity-40 transition-colors"
                >
                  {save.isPending ? "保存中..." : <><Save className="h-4 w-4" /> 保存</>}
                </button>
              </div>
            </div>
            <textarea
              value={content}
              onChange={(e) => { setContent(e.target.value); setDirty(true); }}
              className="w-full h-[calc(100vh-20rem)] min-h-[21rem] bg-black/60 text-fg font-mono text-xs p-4 outline-none resize-none leading-relaxed"
              spellCheck={false}
            />
          </div>
        )}
      </div>
    </div>
  );
}

const TIER_ORDER = ["narrative", "reasoning", "dialog", "fast", "embedding"] as const;

// Claude Code サブスク bridge の状態 + 消費量。レート残量 API は存在しないため、
// bridge が自己観測した消費 (5h 窓 = レート律速の単位 / 今日 / 7日) を表示する。
function ClaudeCodeStatus({ enabled, usage, auth, update, qc }: {
  enabled: boolean;
  usage: import("../api/pages").ClaudeCodeUsage | null;
  auth: { token_set: boolean; mode: string } | null;
  update: { current: string; latest: string; available: boolean } | null;
  qc: ReturnType<typeof useQueryClient>;
}) {
  const fmtTok = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  const [tokenInput, setTokenInput] = useState("");
  const tokenMut = useMutation({
    mutationFn: (token: string) => pagesApi.saveClaudeCodeToken(token),
    onSuccess: () => {
      setTokenInput("");
      qc.invalidateQueries({ queryKey: ["model-tiers"] });
    },
  });
  const updateMut = useMutation({
    mutationFn: () => pagesApi.claudeCodeUpdate(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["model-tiers"] }),
  });
  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-2">
      <h3 className="m-0 text-sm font-semibold text-fg flex items-center gap-2 flex-wrap">
        <span className="whitespace-nowrap">Claude Code (サブスク枠)</span>
        <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${enabled ? "bg-success" : "bg-fg-subtle"}`} title={enabled ? "外部LLM接続 稼働中" : "外部LLM接続 停止中"} />
        {auth && (
          <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-semibold whitespace-nowrap">
            認証: {auth.token_set ? "トークン" : "ホストログイン"}
          </span>
        )}
      </h3>
      {!enabled && (
        <p className="m-0 text-xs text-fg-subtle">
          外部LLM接続が停止しています。PC 側で接続プログラムを起動してください
          （自動起動を設定済みなら自動で復帰します）。
        </p>
      )}
      {enabled && update && (
        <div className="flex items-center gap-2 flex-wrap text-[10.5px]">
          <span className="text-fg-subtle">CLI {update.current.split(" ")[0]}</span>
          {update.available ? (
            <>
              <span className="text-warning font-semibold">新しい版 v{update.latest} があります</span>
              <button
                onClick={() => updateMut.mutate()}
                disabled={updateMut.isPending}
                className="px-2 py-0.5 rounded text-[10.5px] font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
              >
                {updateMut.isPending ? "更新中… (~1分)" : "更新"}
              </button>
            </>
          ) : (
            <span className="text-fg-subtle">最新版です</span>
          )}
          {updateMut.isSuccess && (
            <span className="text-success">
              {updateMut.data.updated ? `→ ${updateMut.data.after.split(" ")[0]} に更新しました` : "変更なし"}
            </span>
          )}
          {updateMut.isError && (
            <span className="text-critical">更新失敗: {(updateMut.error as Error).message}</span>
          )}
        </div>
      )}
      {enabled && (
        <div className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder={auth?.token_set ? "トークン設定済 — 置き換え…" : "sk-ant-oat… (claude setup-token で発行)"}
              autoComplete="off"
              className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs flex-1 min-w-[160px]"
            />
            <button
              onClick={() => tokenMut.mutate(tokenInput.trim())}
              disabled={tokenMut.isPending || !tokenInput.trim()}
              className="px-2 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
            >
              トークン保存
            </button>
            {auth?.token_set && (
              <button
                onClick={() => tokenMut.mutate("")}
                disabled={tokenMut.isPending}
                className="text-[11px] text-fg-subtle hover:underline disabled:opacity-50"
              >
                削除
              </button>
            )}
          </div>
          <div className="space-y-1">
            <p className="m-0 text-[10px] text-fg-subtle">
              トークンの発行はターミナルで下記を実行 (URL 認可 → コード貼付 → トークン表示)。
              出力されたトークンを上の欄に貼付 (保存は即時反映)。
            </p>
            <pre className="m-0 bg-surface-2 border border-border-subtle rounded px-2 py-1 text-[9.5px] text-fg-muted overflow-x-auto whitespace-pre">
{`docker exec -it -e HOME=/data/claude-home claude-bridge \\
  /data/claude-home/.local/bin/claude setup-token`}
            </pre>
          </div>
          {tokenMut.isError && (
            <p className="m-0 text-[11px] text-critical">{(tokenMut.error as Error).message}</p>
          )}
        </div>
      )}
      {enabled && usage && (
        <div className="overflow-x-auto">
          <table className="text-xs text-fg-muted w-full max-w-lg">
            <thead>
              <tr className="text-fg-subtle text-[10.5px] uppercase tracking-wider">
                <th className="text-left py-1 pr-3">期間</th>
                <th className="text-right py-1 px-2">呼出回数</th>
                <th className="text-right py-1 px-2">入力トークン</th>
                <th className="text-right py-1 px-2">出力トークン</th>
                <th className="text-right py-1 pl-2" title="サブスクでは請求されない。API で同じ処理をした場合の換算額">API換算</th>
              </tr>
            </thead>
            <tbody className="tnum">
              {([["直近5時間（レート上限の集計単位）", usage.window_5h], ["今日", usage.today], ["7 日間", usage.days7]] as const).map(([label, w]) => (
                <tr key={label} className="border-t border-border-subtle">
                  <td className="py-1 pr-3 text-fg">{label}</td>
                  <td className="text-right py-1 px-2">{w ? w.calls : "—"}</td>
                  <td className="text-right py-1 px-2">{w ? fmtTok(w.input_tokens + w.cache_read_tokens) : "—"}</td>
                  <td className="text-right py-1 px-2">{w ? fmtTok(w.output_tokens) : "—"}</td>
                  <td className="text-right py-1 pl-2">{w ? `$${w.cost_usd_equivalent.toFixed(2)}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="m-0 mt-1.5 text-[10.5px] text-fg-subtle">
            サブスクのレート上限は 5 時間ごとに回復します（残量は取得できないため使用量を表示）。
            {usage.last_call_at && <> 最終呼出: {usage.last_call_at.slice(0, 16).replace("T", " ")}</>}
          </p>
        </div>
      )}
      {enabled && !usage && (
        <p className="m-0 text-xs text-fg-subtle">まだ呼出記録がありません。</p>
      )}
    </div>
  );
}

// 外部 LLM API キーの UI 完結編集。保存先は .env (版履歴 DB には秘密を置かない §4)、
// config は request ごとに .env を再読込するため保存は即時反映 (再起動不要)。
function ModelTiersEditor({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  const { data } = useQuery({
    queryKey: ["model-tiers"],
    queryFn: () => pagesApi.getModelTiers(),
  });
  const [tiers, setTiers] = useState<Record<string, string> | null>(null);
  const [narrativeThink, setNarrativeThink] = useState<string | null>(null);
  useEffect(() => {
    if (data) {
      setTiers({ ...data.tiers });
      setNarrativeThink(data.narrative_think);
    }
  }, [data]);

  const saveMut = useMutation({
    mutationFn: () => pagesApi.saveModelTiers(tiers ?? {}, narrativeThink ?? undefined),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["model-tiers"] }),
  });

  if (!data || tiers === null) {
    return <div className="text-fg-muted text-sm">読み込み中…</div>;
  }

  const setTier = (tier: string, model: string) =>
    setTiers((prev) => ({ ...(prev ?? {}), [tier]: model }));

  const isDirty =
    TIER_ORDER.some((t) => (tiers[t] ?? "") !== (data.tiers[t] ?? "")) ||
    (narrativeThink ?? data.narrative_think) !== data.narrative_think;

  return (
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-4 items-start">
      {/* 左: ティア割当 (この画面の主タスク) */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-5 space-y-4">
        <p className="m-0 text-xs text-fg-subtle leading-relaxed">
          処理ごとにどのティアを使うかは<strong className="text-fg-muted">ツールが決めます</strong>。ここでは
          各ティアに<strong className="text-fg-muted">実モデルを1つ</strong>割り当てるだけです。
          中華系モデルはセキュリティ方針で除外。保存は版履歴に残り、次回の実行から反映されます。
        </p>

        {data.ollama_error && (
          <div className="bg-warning-soft border border-warning rounded p-3 text-xs text-fg-muted inline-flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5" /> Ollama に接続できませんでした ({data.ollama_error})。現在の割当のみ表示しています。
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {TIER_ORDER.map((tier) => (
            <TierCard
              key={tier}
              tier={tier}
              meta={data.meta[tier]}
              current={tiers[tier] ?? ""}
              saved={data.tiers[tier] ?? ""}
              data={data}
              onChange={(m) => setTier(tier, m)}
              narrativeThink={tier === "narrative" ? (narrativeThink ?? data.narrative_think) : undefined}
              onChangeThink={tier === "narrative" ? setNarrativeThink : undefined}
            />
          ))}
        </div>

        <div className="flex items-center gap-3 pt-3 border-t border-border-subtle flex-wrap">
          <button
            onClick={() => saveMut.mutate()}
            disabled={saveMut.isPending || !isDirty}
            className="px-4 py-1.5 rounded text-sm font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            <Save className="h-3.5 w-3.5" /> {saveMut.isPending ? "保存中…" : isDirty ? "変更を保存" : "保存"}
          </button>
          {isDirty && !saveMut.isPending && (
            <span className="text-warning text-xs">未保存の変更があります</span>
          )}
          {saveMut.isSuccess && !isDirty && (
            <span className="inline-flex items-center gap-1 text-success text-sm">
              <Check className="h-3.5 w-3.5" /> 保存しました（次回の実行から反映）
            </span>
          )}
          {saveMut.isError && (
            <span className="text-critical text-sm">保存失敗: {(saveMut.error as Error).message}</span>
          )}
          {data.excluded.length > 0 && (
            <span className="ml-auto text-[11px] text-fg-subtle" title={data.excluded.join(", ")}>
              方針により除外: {data.excluded.length} 件
            </span>
          )}
        </div>
      </div>

      {/* 右: 外部接続レール (キー・消費量 = 割当の前提情報)。登録済みの接続先だけが並ぶ */}
      <div className="space-y-4">
        <ClaudeCodeStatus
          enabled={data.claude_code_enabled}
          usage={data.claude_code_usage}
          auth={data.claude_code_auth}
          update={data.claude_code_update}
          qc={qc}
        />
        <LlmEndpointsPanel
          endpoints={data.endpoints ?? []}
          presets={data.endpoint_presets ?? []}
          anthropic={{
            enabled: data.external_enabled,
            masked: data.anthropic_key_masked,
            usage: data.anthropic_usage,
          }}
          ollama={{
            baseUrl: data.ollama_base_url ?? "",
            error: data.ollama_error,
            modelCount: (data.available ?? []).length,
          }}
          qc={qc}
        />
      </div>
    </div>
  );
}

const fmtTok = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

// llm_usage 由来の消費 (5h窓/今日/7日) の小型テーブル。bridge の自己観測表示と同じ 3 区分。
function UsageMiniTable({ title, usage }: {
  title: string;
  usage: import("../api/pages").LlmUsageWindows;
}) {
  const rows = [
    ["直近5時間", usage.window_5h],
    ["今日", usage.today],
    ["7 日間", usage.days7],
  ] as const;
  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-1.5">
      <h3 className="m-0 text-xs font-semibold text-fg">{title}</h3>
      <table className="text-xs text-fg-muted w-full">
        <thead>
          <tr className="text-fg-subtle text-[10px] uppercase tracking-wider">
            <th className="text-left py-0.5 pr-2">期間</th>
            <th className="text-right py-0.5 px-2">呼出</th>
            <th className="text-right py-0.5 px-2">入力</th>
            <th className="text-right py-0.5 pl-2">出力</th>
          </tr>
        </thead>
        <tbody className="tnum">
          {rows.map(([label, w]) => (
            <tr key={label}>
              <td className="py-0.5 pr-2">{label}</td>
              <td className="text-right py-0.5 px-2">{w ? w.calls : 0}</td>
              <td className="text-right py-0.5 px-2">{w ? fmtTok(w.input_tokens) : 0}</td>
              <td className="text-right py-0.5 pl-2">{w ? fmtTok(w.output_tokens) : 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// LLM 接続先パネル。Anthropic (組み込み・専用アダプタ) とユーザ定義 (OpenAI 互換) を
// 1 つの「接続先」概念に統合 — 設定済みだけが並び、未設定ベンダーの欄は出さない。
function LlmEndpointsPanel({ endpoints, presets, anthropic, ollama, qc }: {
  endpoints: import("../api/pages").LlmEndpointInfo[];
  presets: import("../api/pages").LlmEndpointPreset[];
  anthropic: {
    enabled: boolean;
    masked: string;
    usage: import("../api/pages").LlmUsageWindows | null;
  };
  ollama: { baseUrl: string; error: string | null; modelCount: number };
  qc: ReturnType<typeof useQueryClient>;
}) {
  const [adding, setAdding] = useState<false | "endpoint" | "anthropic">(false);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  // ollama 接続 URL のその場編集 (設定・死活の画面統合 P3)。null = 未編集 (現在値を表示)
  const [ollamaUrlInput, setOllamaUrlInput] = useState<string | null>(null);
  const [ollamaMsg, setOllamaMsg] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const ollamaUrlMut = useMutation({
    mutationFn: (url: string) => pagesApi.saveOllamaUrl(url),
    onSuccess: (r) => {
      setOllamaUrlInput(null);
      setOllamaMsg(
        r.error
          ? { kind: "error", text: `保存しました — ただし接続できません: ${r.error}` }
          : { kind: "success", text: `保存しました — モデル ${r.model_count ?? 0} 件を取得` },
      );
      qc.invalidateQueries({ queryKey: ["model-tiers"] });
    },
    onError: (e: Error) => setOllamaMsg({ kind: "error", text: e.message }),
  });

  const saveMut = useMutation({
    mutationFn: (next: { name: string; base_url: string }[]) => pagesApi.saveLlmEndpoints(next),
    onSuccess: () => {
      setAdding(false);
      setName("");
      setBaseUrl("");
      qc.invalidateQueries({ queryKey: ["model-tiers"] });
    },
  });
  const keyMut = useMutation({
    mutationFn: (v: { name: string; api_key: string }) =>
      pagesApi.saveLlmEndpointKey(v.name, v.api_key),
    onSuccess: (_d, v) => {
      setKeyInputs((prev) => ({ ...prev, [v.name]: "" }));
      qc.invalidateQueries({ queryKey: ["model-tiers"] });
    },
  });
  // Anthropic は専用 API (Messages API アダプタ + ANTHROPIC_API_KEY) — キー保存/削除で出現/消滅
  const anthropicKeyMut = useMutation({
    mutationFn: (apiKey: string) => pagesApi.saveAnthropicKey(apiKey),
    onSuccess: () => {
      setAdding(false);
      setKeyInputs((prev) => ({ ...prev, __anthropic: "" }));
      qc.invalidateQueries({ queryKey: ["model-tiers"] });
    },
  });

  const current = endpoints.map((e) => ({ name: e.name, base_url: e.base_url }));

  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-3">
      <h3 className="m-0 text-sm font-semibold text-fg">接続先 (LLM)</h3>
      <p className="m-0 text-[11px] text-fg-subtle leading-snug">
        既定はローカル Ollama。Anthropic API / OpenAI / Gemini / LM Studio /
        リモート Ollama 等を登録すると各ティアの選択肢に現れます。キーは .env に保存
        (即時反映)。中華系モデルは方針により除外。
      </p>

      {/* 組み込みローカルエンジン (全ティアの既定 + 外部障害時の fallback 先 — 削除不可) */}
      <div className="border border-border-subtle rounded p-2.5 space-y-1.5 bg-surface-2/40">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold text-fg">ollama</span>
          <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-semibold">組み込み・ローカル</span>
          <span
            className={`inline-block w-2 h-2 rounded-full ${ollama.error ? "bg-critical" : "bg-success"}`}
            title={ollama.error ? `接続できません: ${ollama.error}` : "稼働中"}
          />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            value={ollamaUrlInput ?? ollama.baseUrl}
            onChange={(e) => setOllamaUrlInput(e.target.value)}
            placeholder="http://host.docker.internal:11434"
            className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs font-mono flex-1 min-w-[200px]"
          />
          <button
            onClick={() => ollamaUrlMut.mutate((ollamaUrlInput ?? "").trim())}
            disabled={
              ollamaUrlMut.isPending ||
              ollamaUrlInput === null ||
              ollamaUrlInput.trim() === "" ||
              ollamaUrlInput.trim() === ollama.baseUrl
            }
            className="px-2 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
          >
            {ollamaUrlMut.isPending ? "保存中…" : "URL 保存"}
          </button>
        </div>
        <p className="m-0 text-[10px] text-fg-subtle">
          {ollama.error
            ? "Ollama に接続できません。ホスト側の Ollama と接続 URL を確認してください。"
            : `モデル ${ollama.modelCount} 件を取得済み — 全ティアの既定・外部障害時の自動切替先のため削除できません。`}
          {" "}Docker からホストの Ollama を見る場合は http://host.docker.internal:11434。保存は即時反映。
        </p>
        {ollamaMsg && (
          <p className={`m-0 text-[11px] ${ollamaMsg.kind === "success" ? "text-success" : "text-critical"}`}>
            {ollamaMsg.text}
          </p>
        )}
      </div>

      {anthropic.enabled && (
        <div className="border border-border-subtle rounded p-2.5 space-y-1.5 bg-surface-2/40">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-fg">anthropic</span>
            <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-semibold">組み込み</span>
            <code className="text-[10px] text-fg-subtle">Anthropic Messages API (従量課金)</code>
            <button
              onClick={() => anthropicKeyMut.mutate("")}
              disabled={anthropicKeyMut.isPending}
              title="キーを削除して無効化 (先に全ティアをローカルへ戻す必要があります)"
              className="ml-auto text-[11px] text-critical hover:underline disabled:opacity-50"
            >
              削除
            </button>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="password"
              value={keyInputs.__anthropic ?? ""}
              onChange={(e) => setKeyInputs((prev) => ({ ...prev, __anthropic: e.target.value }))}
              placeholder={`キー設定済 (${anthropic.masked}) — 置き換え…`}
              autoComplete="off"
              className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs flex-1 min-w-[140px]"
            />
            <button
              onClick={() => anthropicKeyMut.mutate((keyInputs.__anthropic ?? "").trim())}
              disabled={anthropicKeyMut.isPending || !(keyInputs.__anthropic ?? "").trim()}
              className="px-2 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
            >
              キー保存
            </button>
          </div>
          {anthropic.usage && <UsageMiniTable title="消費" usage={anthropic.usage} />}
          {anthropicKeyMut.isError && (
            <p className="m-0 text-[11px] text-critical">{(anthropicKeyMut.error as Error).message}</p>
          )}
        </div>
      )}

      {endpoints.map((ep) => (
        <div key={ep.name} className="border border-border-subtle rounded p-2.5 space-y-1.5 bg-surface-2/40">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-fg">{ep.name}</span>
            <code className="text-[10px] text-fg-subtle break-all">{ep.base_url}</code>
            <button
              onClick={() => saveMut.mutate(current.filter((c) => c.name !== ep.name))}
              disabled={saveMut.isPending}
              className="ml-auto text-[11px] text-critical hover:underline disabled:opacity-50"
            >
              削除
            </button>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <input
              type="password"
              value={keyInputs[ep.name] ?? ""}
              onChange={(e) => setKeyInputs((prev) => ({ ...prev, [ep.name]: e.target.value }))}
              placeholder={ep.key_set ? `キー設定済 (${ep.key_masked}) — 置き換え…` : "API キー (不要なら空のまま)"}
              autoComplete="off"
              className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs flex-1 min-w-[140px]"
            />
            <button
              onClick={() => keyMut.mutate({ name: ep.name, api_key: (keyInputs[ep.name] ?? "").trim() })}
              disabled={keyMut.isPending || !(keyInputs[ep.name] ?? "").trim()}
              className="px-2 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
            >
              キー保存
            </button>
            {ep.key_set && (
              <button
                onClick={() => keyMut.mutate({ name: ep.name, api_key: "" })}
                disabled={keyMut.isPending}
                className="text-[11px] text-fg-subtle hover:underline disabled:opacity-50"
              >
                キー削除
              </button>
            )}
          </div>
          <p className="m-0 text-[10px] text-fg-subtle">
            {ep.models.length > 0
              ? `モデル ${ep.models.length} 件を取得済み — 各ティアの選択肢に表示中`
              : "モデル一覧を取得できていません (接続先の稼働とキーを確認)"}
          </p>
          {ep.usage && <UsageMiniTable title="消費" usage={ep.usage} />}
        </div>
      ))}

      {adding === false ? (
        <button
          onClick={() => setAdding("endpoint")}
          className="px-3 py-1.5 rounded text-xs font-medium border border-border-subtle text-fg-muted hover:text-fg hover:bg-surface-2"
        >
          + 接続先を追加
        </button>
      ) : (
        <div className="border border-dashed border-border-subtle rounded p-2.5 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <select
              onChange={(e) => {
                if (e.target.value === "__anthropic") {
                  setAdding("anthropic");
                  return;
                }
                setAdding("endpoint");
                const p = presets.find((x) => x.name === e.target.value);
                if (p) {
                  setName(p.name === "custom" ? "" : p.name);
                  setBaseUrl(p.base_url);
                }
              }}
              defaultValue=""
              className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs"
            >
              <option value="" disabled>プリセットを選択…</option>
              {!anthropic.enabled && (
                <option value="__anthropic">Anthropic API (Claude・専用接続)</option>
              )}
              {presets.map((p) => (
                <option key={p.name} value={p.name}>{p.label}</option>
              ))}
            </select>
          </div>
          {adding === "anthropic" ? (
            <>
              <p className="m-0 text-[10.5px] text-fg-subtle">
                Anthropic は専用接続 (Messages API)。キーを保存すると接続先として現れます。
              </p>
              <input
                type="password"
                value={keyInputs.__anthropic ?? ""}
                onChange={(e) => setKeyInputs((prev) => ({ ...prev, __anthropic: e.target.value }))}
                placeholder="sk-ant-…"
                autoComplete="off"
                className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs w-full"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={() => anthropicKeyMut.mutate((keyInputs.__anthropic ?? "").trim())}
                  disabled={anthropicKeyMut.isPending || !(keyInputs.__anthropic ?? "").trim()}
                  className="px-3 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
                >
                  {anthropicKeyMut.isPending ? "保存中…" : "登録"}
                </button>
                <button onClick={() => setAdding(false)} className="text-xs text-fg-subtle hover:underline">
                  取消
                </button>
              </div>
              {anthropicKeyMut.isError && (
                <p className="m-0 text-[11px] text-critical">{(anthropicKeyMut.error as Error).message}</p>
              )}
            </>
          ) : (
            <>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="接続先名 (英小文字、例: openai)"
                className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs w-full"
              />
              <input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="base URL (例: http://192.168.1.10:1234/v1)"
                className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs w-full"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={() => saveMut.mutate([...current, { name: name.trim(), base_url: baseUrl.trim() }])}
                  disabled={saveMut.isPending || !name.trim() || !baseUrl.trim()}
                  className="px-3 py-1 rounded text-xs font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
                >
                  {saveMut.isPending ? "保存中…" : "登録"}
                </button>
                <button onClick={() => setAdding(false)} className="text-xs text-fg-subtle hover:underline">
                  取消
                </button>
              </div>
              {saveMut.isError && (
                <p className="m-0 text-[11px] text-critical">{(saveMut.error as Error).message}</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// 1 ティア = 1 カード。割当の状態 (ローカル/外部/未保存変更) をカード上で完結して見せる。
function TierCard({ tier, meta, current, saved, data, onChange, narrativeThink, onChangeThink }: {
  tier: string;
  meta?: TierMeta;
  current: string;
  saved: string;
  data: ModelTiersResponse;
  onChange: (model: string) => void;
  narrativeThink?: string;
  onChangeThink?: (v: string) => void;
}) {
  // 埋込ティアは外部プロバイダ不可 (API 側も検証で拒否)。
  const external = tier === "embedding" ? [] : data.external ?? [];
  const claudeCode = tier === "embedding" ? [] : data.claude_code ?? [];
  const endpointGroups = tier === "embedding" ? [] : (data.endpoints ?? []).filter((e) => e.models.length > 0);
  const endpointNames = (data.endpoints ?? []).map((e) => e.name);
  const isEndpointCurrent = endpointNames.some((n) => current.startsWith(`${n}:`));
  // 現割当が pull 済一覧に無い場合も失わないよう option に含める。
  const isExternalCurrent =
    external.includes(current) || claudeCode.includes(current) || isEndpointCurrent;
  const localOptions = Array.from(
    new Set([...(isExternalCurrent ? [] : [current]), ...data.available].filter(Boolean)),
  );
  const hasExternalGroups =
    external.length > 0 || claudeCode.length > 0 || endpointGroups.length > 0;
  const isExternal =
    current.startsWith("anthropic:") || current.startsWith("claudecode:") || isEndpointCurrent;
  const externalKind = current.startsWith("anthropic:")
    ? "Anthropic API (従量課金)"
    : current.startsWith("claudecode:")
      ? "Claude Code (サブスク枠)"
      : `接続先「${current.split(":")[0]}」`;
  const dirty = current !== saved;

  return (
    <div className={`rounded-lg border p-3.5 space-y-2 ${dirty ? "border-warning" : "border-border-subtle"} bg-surface-2/40`}>
      <div className="flex items-center gap-2 flex-wrap">
        <h3 className="m-0 text-sm font-semibold text-fg">{meta?.label ?? tier}</h3>
        {isExternal ? (
          <span className="text-[10px] px-1.5 py-px rounded-sm bg-warning-soft text-warning font-semibold">外部</span>
        ) : (
          <span className="text-[10px] px-1.5 py-px rounded-sm bg-surface-3 text-fg-subtle font-semibold">ローカル</span>
        )}
        {dirty && <span className="text-[10px] text-warning ml-auto">未保存</span>}
      </div>
      <p className="m-0 text-[11px] text-fg-subtle leading-snug">{meta?.description}</p>
      <select
        value={current}
        onChange={(e) => onChange(e.target.value)}
        className="w-full bg-surface-2 border border-border-subtle rounded px-2 py-1.5 text-sm text-fg"
      >
        {tier === "embedding" && <option value="">（無効 / 意味による重複判定をオフ）</option>}
        {hasExternalGroups ? (
          <>
            <optgroup label="ローカル (Ollama)">
              {localOptions.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </optgroup>
            {claudeCode.length > 0 && (
              <optgroup label="Claude Code (サブスク枠)">
                {claudeCode.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </optgroup>
            )}
            {external.length > 0 && (
              <optgroup label="Anthropic API (従量課金)">
                {external.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </optgroup>
            )}
            {endpointGroups.map((ep) => (
              <optgroup key={ep.name} label={`接続先: ${ep.name}`}>
                {ep.models.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </optgroup>
            ))}
          </>
        ) : (
          localOptions.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))
        )}
      </select>
      {isExternal && (
        <p className="m-0 text-[10.5px] text-warning leading-snug">
          {externalKind}で実行。利用できないときは自動的にローカルモデルへ切替。
        </p>
      )}
      {onChangeThink !== undefined && (
        <div className="pt-1.5 border-t border-border-subtle space-y-1">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-fg-muted shrink-0">拡張思考 (think)</span>
            <select
              value={narrativeThink ?? "auto"}
              onChange={(e) => onChangeThink(e.target.value)}
              disabled={!isExternal}
              className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs text-fg disabled:opacity-50"
            >
              <option value="auto">使う（外部モデルのみ）</option>
              <option value="off">使わない</option>
            </select>
          </div>
          <p className="m-0 text-[10px] text-fg-subtle leading-snug">
            {isExternal
              ? "分析の深さが上がる一方、生成時間は 2〜3 倍になります。"
              : "ローカルモデルでは常に無効です（外部モデル割当時のみ選べます）。"}
          </p>
        </div>
      )}
      {meta?.examples && (
        <div className="flex flex-wrap gap-1">
          {meta.examples.split(/\s*\/\s*/).map((ex) => (
            <span key={ex} className="text-[10px] px-1.5 py-px rounded-full bg-surface-3 text-fg-muted">
              {ex}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
