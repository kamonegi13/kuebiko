// Grok タスク定義カード (2026-08-15): 購読ソースの Grok セクションに常設。
// Grok 側スケジュールタスク (プロンプト/頻度/窓) の「写し」を DB 版保存し、
// アカウント事故・誤編集による収集設計の喪失に備える。
// SSoT は Grok 側 — ここで編集しても Grok には反映されない (synced_at を利用者が記録)。

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ClipboardList, Loader2, Plus, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import { grokApi, type GrokTaskDef } from "../api/grok";

const EMPTY_TASK: GrokTaskDef = {
  id: "",
  name: "",
  schedule: "",
  window: "",
  prompt: "",
  note: "",
  synced_at: "",
};

function TaskView({ task }: { task: GrokTaskDef }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-border-subtle bg-surface-2">
      <button
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-fg hover:bg-surface-3"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span className="font-semibold">{task.name}</span>
        <span className="text-fg-subtle font-mono">{task.id}</span>
        {task.schedule && <span className="text-fg-muted">{task.schedule}</span>}
        {task.window && <span className="text-fg-muted">窓: {task.window}</span>}
        {task.synced_at && <span className="ml-auto text-fg-subtle">同期: {task.synced_at}</span>}
      </button>
      {open && (
        <div className="border-t border-border-subtle px-3 py-2">
          {task.note && <p className="m-0 mb-2 text-xs text-fg-muted">{task.note}</p>}
          <pre className="m-0 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-surface-3 p-2 font-mono text-[11px] text-fg">
            {task.prompt}
          </pre>
        </div>
      )}
    </div>
  );
}

function TaskEditor({
  task,
  onChange,
  onDelete,
}: {
  task: GrokTaskDef;
  onChange: (next: GrokTaskDef) => void;
  onDelete: () => void;
}) {
  const field = (label: string, key: keyof GrokTaskDef, placeholder: string) => (
    <label className="flex flex-col gap-0.5 text-[11px] text-fg-muted">
      {label}
      <input
        className="rounded border border-border-subtle bg-surface-1 px-2 py-1 text-xs text-fg"
        value={task[key]}
        placeholder={placeholder}
        onChange={(e) => onChange({ ...task, [key]: e.target.value })}
      />
    </label>
  );
  return (
    <div className="rounded-md border border-border-subtle bg-surface-2 p-3 space-y-2">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {field("id (英小文字/数字/-/_)", "id", "x_native_signal")}
        {field("名称", "name", "X Native Signal")}
        {field("スケジュール", "schedule", "Hourly / Every day / All day")}
        {field("対象窓", "window", "直近90分")}
        {field("Grok 側へ反映した日", "synced_at", "2026-08-15")}
        {field("補足", "note", "テーマ A-F (グローバル早期シグナル)")}
      </div>
      <label className="flex flex-col gap-0.5 text-[11px] text-fg-muted">
        プロンプト本文
        <textarea
          className="min-h-40 rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-[11px] text-fg"
          value={task.prompt}
          onChange={(e) => onChange({ ...task, prompt: e.target.value })}
        />
      </label>
      <button
        className="inline-flex items-center gap-1 rounded-md border border-border-subtle px-2 py-1 text-[11px] text-critical hover:bg-surface-3"
        onClick={onDelete}
      >
        <Trash2 className="h-3 w-3" /> このタスクを削除
      </button>
    </div>
  );
}

function isBlankTask(t: GrokTaskDef): boolean {
  return !t.id.trim() && !t.name.trim() && !t.prompt.trim();
}

// 保存前の事前検証。空行は呼び出し側で除外済みの前提で、不足フィールドを日本語で返す。
function validateForSave(tasks: GrokTaskDef[]): string | null {
  for (const [i, t] of tasks.entries()) {
    const missing = [
      !t.id.trim() && "id",
      !t.name.trim() && "名称",
      !t.prompt.trim() && "プロンプト本文",
    ].filter(Boolean);
    if (missing.length > 0) {
      return `タスク ${i + 1} (${t.name.trim() || t.id.trim() || "無題"}): ${missing.join(" / ")} が未入力です`;
    }
  }
  return null;
}

export function GrokTasksCard({ readOnly }: { readOnly: boolean }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<GrokTaskDef[] | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const { data, isError } = useQuery({
    queryKey: ["grok-tasks"],
    queryFn: () => grokApi.tasks(),
    enabled: !readOnly, // 公開面では denylist により 403 のため取得しない
  });

  const save = useMutation({
    mutationFn: (tasks: GrokTaskDef[]) => grokApi.saveTasks(tasks),
    onSuccess: () => {
      setEditing(null);
      void qc.invalidateQueries({ queryKey: ["grok-tasks"] });
    },
  });

  if (readOnly) return null;

  const tasks = data?.tasks ?? [];

  return (
    <div className="rounded-lg border border-border-subtle p-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-4 w-4 text-fg-muted" />
          <span className="font-bold text-fg text-sm">Grok タスク定義 (写し)</span>
          {data?.saved_at && (
            <span className="text-xs text-fg-subtle">
              v{data.version} · {data.saved_at.slice(0, 16)}
            </span>
          )}
        </div>
        {editing === null ? (
          <button
            className="rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3"
            onClick={() => {
              setValidationError(null);
              setEditing(tasks.map((t) => ({ ...t })));
            }}
          >
            編集
          </button>
        ) : (
          <div className="flex items-center gap-2">
            <button
              className="rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3"
              onClick={() => {
                setValidationError(null);
                setEditing(null);
              }}
            >
              キャンセル
            </button>
            <button
              className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-2 px-3 py-1.5 text-xs text-fg hover:bg-surface-3 disabled:opacity-50"
              disabled={save.isPending}
              onClick={() => {
                // 完全に空の行 (追加したが未入力) は黙って除外する
                const filled = editing.filter((t) => !isBlankTask(t));
                const err = validateForSave(filled);
                if (err) {
                  setValidationError(err);
                  return;
                }
                setValidationError(null);
                if (window.confirm("Grok タスク定義の写しを保存しますか? (版履歴に残ります)")) {
                  save.mutate(filled);
                }
              }}
            >
              {save.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Save className="h-3.5 w-3.5" />
              )}
              保存
            </button>
          </div>
        )}
      </div>

      <p className="m-0 mt-1 text-[11px] text-fg-subtle">
        実体 (SSoT) は Grok 側のスケジュールタスクです。ここは喪失に備えた記録用の写しで、
        保存しても Grok には反映されません。Grok 側を変更したら写しも更新し「反映した日」を記録してください。
      </p>

      <div className="mt-3 space-y-2">
        {isError && (
          <p className="m-0 text-xs text-critical">タスク定義の取得に失敗しました。</p>
        )}
        {editing === null ? (
          tasks.length === 0 ? (
            <p className="m-0 text-xs text-fg-muted">
              未登録です。「編集」から Grok 側のタスク内容を貼り付けて保存してください。
            </p>
          ) : (
            tasks.map((t) => <TaskView key={t.id} task={t} />)
          )
        ) : (
          <>
            {editing.map((t, i) => (
              <TaskEditor
                key={i}
                task={t}
                onChange={(next) => setEditing(editing.map((x, j) => (j === i ? next : x)))}
                onDelete={() => setEditing(editing.filter((_, j) => j !== i))}
              />
            ))}
            <button
              className="inline-flex items-center gap-1 rounded-md border border-border-subtle px-2 py-1 text-xs text-fg hover:bg-surface-3"
              onClick={() => setEditing([...editing, { ...EMPTY_TASK }])}
            >
              <Plus className="h-3.5 w-3.5" /> タスクを追加
            </button>
            {validationError && (
              <p className="m-0 text-xs text-critical">{validationError}</p>
            )}
            {save.isError && (
              <p className="m-0 text-xs text-critical">保存失敗: {(save.error as Error).message}</p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
