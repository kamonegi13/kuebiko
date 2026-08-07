// 分析チャット (2026-07-12): 自然言語で収集・分析データを照会し簡易レポートを得る。
// backend が LLM でツール計画 → read-only データツール実行 → 接地回答を返す。
// readonly 公開 instance でも利用可 (2026-07-19 allowlist 化。model override は無効)。

import { useEffect, useRef, useState } from "react";
import { Cpu, Loader2, MessageSquarePlus, Send, Wrench } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { pagesApi } from "../api/pages";
import { pageContainer, PageHeader } from "../components/Page";
import { MarkdownText } from "../components/MarkdownText";
import type { AssistantToolRun, AssistantTurn } from "../api/types";

interface ChatMessage extends AssistantTurn {
  tools?: AssistantToolRun[];
  model?: string;
  error?: boolean;
}

// 会話単位のモデル override の永続キー (端末ローカル。空 = dialog ティア既定)
const MODEL_STORAGE_KEY = "assistant_model_override";
// 会話履歴の永続キー (端末ローカル)。リロード・タブ遷移で会話が消えないように保持する。
// LLM に注入されるのは直近 6 ターンのみ (backend 側 clip) — 保存量はここで cap する。
const HISTORY_STORAGE_KEY = "assistant_chat_history";
const HISTORY_KEEP_MESSAGES = 60;

function loadStoredMessages(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(HISTORY_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (m): m is ChatMessage =>
        typeof m === "object" && m !== null && "role" in m && "content" in m,
    );
  } catch {
    return [];
  }
}

const SUGGESTIONS = [
  "直近 7 日で日本に関係する重要な脅威をレポートして",
  "中国系アクターの直近 30 日の活動を要約して",
  "追跡中の情勢で確度が変わったものは?",
  "直近 30 日の記事をカテゴリ別に集計して",
];

export function AssistantPage() {
  const [messages, setMessages] = useState<ChatMessage[]>(loadStoredMessages);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [model, setModel] = useState<string>(
    () => localStorage.getItem(MODEL_STORAGE_KEY) ?? "",
  );
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // 会話を端末ローカルへ永続化 (リロード耐性)。古い分は cap して肥大を防ぐ。
  useEffect(() => {
    try {
      localStorage.setItem(
        HISTORY_STORAGE_KEY,
        JSON.stringify(messages.slice(-HISTORY_KEEP_MESSAGES)),
      );
    } catch {
      // quota 超過等は無視 (会話継続を妨げない)
    }
  }, [messages]);

  const newConversation = () => {
    setMessages([]);
    localStorage.removeItem(HISTORY_STORAGE_KEY);
  };

  // モデル選択肢 (dialog ティア既定 + ローカル + 外部)。設定タブと同じ語彙。
  const { data: tiers } = useQuery({
    queryKey: ["model-tiers"],
    queryFn: () => pagesApi.getModelTiers(),
    staleTime: 60_000,
  });

  // 実効モデル (override 未選択なら dialog ティア既定) と、その所在に応じた表示文言。
  const effectiveModel = model || tiers?.tiers.dialog || "";
  const isExternalModel =
    effectiveModel.startsWith("claudecode:") || effectiveModel.startsWith("anthropic:");
  const busyNote = isExternalModel
    ? `${effectiveModel}・外部、数秒〜20 秒`
    : `${effectiveModel || "ローカル LLM"}・ローカル、10〜40 秒`;

  const switchModel = (m: string) => {
    setModel(m);
    localStorage.setItem(MODEL_STORAGE_KEY, m);
  };

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    const history: AssistantTurn[] = messages.map((m) => ({ role: m.role, content: m.content }));
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    setBusy(true);
    try {
      const res = await api.assistantChat(message, history, model);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: res.answer, tools: res.tools, model: res.model },
      ]);
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : "不明なエラー";
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `エラーが発生しました (${detail})。時間をおいて再試行してください。`,
          error: true,
        },
      ]);
    } finally {
      setBusy(false);
      setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
    }
  }

  return (
    <div className={`${pageContainer("wide")} space-y-5`}>
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <PageHeader
          title="分析チャット"
          subtitle="収集・分析済みデータ (記事検索 / 集計 / 状況台帳 / アクター動向) を自然言語で照会し、出典つきの簡易レポートを得る。回答は LLM (入力欄左で選択可) がツール実行結果のみを根拠に生成。直近 6 ターンを文脈として引き継ぎ、会話はこの端末に保持されます。"
        />
        {messages.length > 0 && (
          <button
            onClick={newConversation}
            disabled={busy}
            title="会話をクリアして新しく始める (文脈もリセット)"
            className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border-subtle text-sm text-fg-muted hover:bg-surface-2 hover:text-fg transition-colors disabled:opacity-50"
          >
            <MessageSquarePlus className="h-4 w-4" />
            新しい会話
          </button>
        )}
      </div>

      <div className="bg-surface-1 border border-border-subtle rounded-lg flex flex-col min-h-[60vh]">
        {/* メッセージ一覧 */}
        <div className="flex-1 p-4 space-y-4 overflow-y-auto">
          {messages.length === 0 && (
            <div className="space-y-3">
              <p className="text-sm text-fg-subtle m-0">
                質問例 (クリックで送信)。応答時間はモデルにより数秒〜40 秒程度です。
              </p>
              <div className="flex flex-wrap gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => void send(s)}
                    className="text-sm text-left px-3 py-1.5 rounded border border-border-subtle text-fg-muted hover:bg-surface-2 transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={i} className={m.role === "user" ? "flex justify-end" : "flex"}>
              <div
                className={
                  m.role === "user"
                    ? "max-w-[85%] bg-surface-2 border border-border-subtle rounded-lg px-3 py-2"
                    : "max-w-[95%] min-w-0 space-y-2"
                }
              >
                {m.role === "user" ? (
                  <p className="text-sm text-fg m-0 whitespace-pre-wrap">{m.content}</p>
                ) : (
                  <>
                    {m.tools && m.tools.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {m.tools.map((t, ti) => (
                          <span
                            key={ti}
                            className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border border-border-default text-fg-muted"
                          >
                            <Wrench className="h-3 w-3" />
                            {t.summary}
                          </span>
                        ))}
                      </div>
                    )}
                    <div
                      className={`border rounded-lg px-3 py-2 min-w-0 ${m.error ? "border-critical/30 bg-critical-soft" : "border-border-subtle"}`}
                    >
                      <MarkdownText>{m.content}</MarkdownText>
                    </div>
                    {m.model && (
                      <div className="text-[10px] text-fg-subtle inline-flex items-center gap-1">
                        <Cpu className="h-3 w-3" />
                        {m.model}
                        {m.model.includes("→") && (
                          <span className="text-warning">(ローカルに自動切替)</span>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}

          {busy && (
            <div className="flex items-center gap-2 text-sm text-fg-subtle">
              <Loader2 className="h-4 w-4 animate-spin" />
              データを照会して回答を生成中… ({busyNote})
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* 入力欄 + モデル選択 (会話単位 override、端末ローカルに記憶) */}
        <form
          className="border-t border-border-subtle p-3 flex gap-2 items-center flex-wrap"
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
        >
          <select
            value={model}
            onChange={(e) => switchModel(e.target.value)}
            title="この会話で使うモデル (未選択なら設定の既定モデル)"
            className="shrink-0 bg-surface-2 border border-border-subtle rounded-md px-2 py-2 text-xs text-fg-muted max-w-[220px]"
            disabled={busy}
          >
            <option value="">既定{tiers?.tiers.dialog ? ` (${tiers.tiers.dialog})` : ""}</option>
            {tiers && (
              <>
                <optgroup label="ローカル (Ollama)">
                  {tiers.available.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </optgroup>
                {tiers.claude_code.length > 0 && (
                  <optgroup label="Claude Code (サブスク枠)">
                    {tiers.claude_code.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </optgroup>
                )}
                {tiers.external.length > 0 && (
                  <optgroup label="Anthropic API (従量課金)">
                    {tiers.external.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </optgroup>
                )}
              </>
            )}
          </select>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="例: 先週の Volt Typhoon 関連の動きをまとめて"
            className="flex-1 min-w-0 bg-surface-2 border border-border-subtle rounded-md px-3 py-2 text-sm text-fg placeholder:text-fg-subtle focus:outline-none focus:border-accent"
            disabled={busy}
          />
          <button
            type="submit"
            disabled={busy || !input.trim()}
            className="shrink-0 inline-flex items-center gap-1.5 px-3 py-2 rounded-md bg-accent-subtle text-accent border border-accent/30 text-sm disabled:opacity-50 hover:bg-accent/10 transition-colors"
          >
            <Send className="h-4 w-4" />
            送信
          </button>
        </form>
      </div>
    </div>
  );
}
