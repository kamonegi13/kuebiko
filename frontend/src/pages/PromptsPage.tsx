import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { pageContainer } from "../components/Page";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pagesApi } from "../api/pages";

export function PromptsPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const { data: list } = useQuery({
    queryKey: ["prompts-list"],
    queryFn: () => pagesApi.promptsList(),
  });
  const { data: file } = useQuery({
    queryKey: ["prompts-file", selected],
    queryFn: () => pagesApi.promptsFile(selected!),
    enabled: !!selected,
  });

  useEffect(() => {
    if (file) {
      setContent(file.content);
      setDirty(false);
    }
  }, [file]);

  // 初期表示で最初の prompt を selected
  useEffect(() => {
    if (!selected && list?.files.length) setSelected(list.files[0]);
  }, [list, selected]);

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

  return (
    <div className={pageContainer("wide")}>
      <h2 className="m-0 mb-4 text-xl font-bold text-fg tracking-tight">プロンプト編集</h2>

      <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4">
        <aside className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden max-h-[700px] overflow-y-auto">
          <div className="px-3 py-2 text-xs uppercase tracking-wider font-semibold text-fg-muted border-b border-border-subtle">ファイル</div>
          {list?.files.map((p) => (
            <div
              key={p}
              onClick={() => setSelected(p)}
              className={`px-3 py-2 cursor-pointer text-sm border-b border-border-subtle font-mono ${
                selected === p ? "bg-accent-subtle text-accent-hover" : "hover:bg-surface-2 text-fg"
              }`}
            >
              {p.replace(/^prompts\//, "")}
            </div>
          ))}
        </aside>

        <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
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
                className="bg-accent text-bg px-4 py-1.5 rounded-md font-semibold text-sm hover:bg-accent-hover disabled:opacity-40 transition-colors inline-flex items-center gap-1.5"
              >
                <Save className="h-4 w-4" />
                {save.isPending ? "保存中..." : "保存"}
              </button>
            </div>
          </div>
          <textarea
            value={content}
            onChange={(e) => { setContent(e.target.value); setDirty(true); }}
            className="w-full h-[640px] bg-black/60 text-fg font-mono text-xs p-4 outline-none resize-none leading-relaxed"
            spellCheck={false}
          />
        </div>
      </div>
    </div>
  );
}
