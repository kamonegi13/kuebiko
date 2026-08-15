// ファイル一覧の区分グループ表示 (2026-08-15)。
// プロンプト/設定ファイル tab の「生ファイル名の羅列で何をするファイルか読めない」を解消する。
// 区分・表示名・説明の SSoT は backend (src/ui/services/file_catalog.py) — ここは描画のみ。

export interface CatalogedFile {
  path: string;
  label: string;
  description: string;
}

export interface FileGroup {
  category: string;
  files: CatalogedFile[];
}

interface FileGroupListProps {
  groups: FileGroup[];
  selected: string | null;
  onSelect: (path: string) => void;
  stripPrefix: string; // 例: "prompts/" — ファイル名表示から取り除く接頭辞
}

export function FileGroupList({ groups, selected, onSelect, stripPrefix }: FileGroupListProps) {
  return (
    <>
      {groups.map((g) => (
        <div key={g.category}>
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider font-semibold text-fg-subtle bg-surface-2 border-b border-border-subtle sticky top-0">
            {g.category}
          </div>
          {g.files.map((f) => (
            <div
              key={f.path}
              onClick={() => onSelect(f.path)}
              title={f.description}
              className={`px-3 py-2 cursor-pointer border-b border-border-subtle ${
                selected === f.path
                  ? "bg-accent-subtle text-accent-hover"
                  : "hover:bg-surface-2 text-fg"
              }`}
            >
              <div className="text-sm leading-tight">{f.label}</div>
              <div className="font-mono text-[10px] text-fg-subtle leading-tight mt-0.5">
                {f.path.startsWith(stripPrefix) ? f.path.slice(stripPrefix.length) : f.path}
              </div>
            </div>
          ))}
        </div>
      ))}
    </>
  );
}
