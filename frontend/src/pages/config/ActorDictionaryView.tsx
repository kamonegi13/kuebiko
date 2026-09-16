// アクター辞書の定義管理 — 設定【定義】タブ (S7)。
//
// 配置基準 (CLAUDE.md §11):「専用ページは閲覧のみ、定義の変更は設定カテゴリ」。
// `/app/actors` (アクター辞書) と脅威アクター面は**閲覧**に専念し、canonical・別名・
// 国・スポンサー等の**定義の変更**はここに集約する。
//
// ⚠ **編集フォーム自体は動かさず載せ替えるだけ**にする。辞書には identity 8 原則
// (id 不変 / merge = redirect 墓標) と alias 衝突検証があり、フォームの中身に手を
// 入れると検証を壊す危険がある (`src/cti/actor_editor.py` が核心)。
//
// 移設の 3 要件 (docs/settings_consolidation_plan.md §4):
//   ①効果  … 保存時の alias 衝突検証 (400 で拒否) が担う
//   ②参照関係 … used_by_pirs (この別名を変えると名指しが外れる SIR)
//   ③成績  … 言及記事数は閲覧面 (脅威アクター) が持つ
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Pencil } from "lucide-react";

import { pagesApi, type ActorRecord } from "../../api/pages";
import { ActorEditForm } from "../actors/ActorDetail";

export function ActorDictionaryView() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["actors"], queryFn: () => pagesApi.getActors() });
  const [editing, setEditing] = useState<ActorRecord | null>(null);
  const [filter, setFilter] = useState("");

  const actors = useMemo(() => {
    const all = data?.actors ?? [];
    const q = filter.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (a) =>
        a.canonical.toLowerCase().includes(q) ||
        a.id.toLowerCase().includes(q) ||
        (a.aliases ?? []).some((x) => x.toLowerCase().includes(q)),
    );
  }, [data, filter]);

  if (isLoading) return <div className="text-sm text-fg-subtle">読み込み中…</div>;

  if (editing) {
    return (
      <div className="space-y-3">
        <button
          onClick={() => setEditing(null)}
          className="text-xs text-fg-muted hover:text-fg"
        >
          ← 辞書一覧へ
        </button>
        <ActorEditForm
          actor={editing}
          families={data?.families ?? []}
          readOnly={false}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["actors"] });
            setEditing(null);
          }}
          onCancel={() => setEditing(null)}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="m-0 text-lg font-bold text-fg tracking-tight">アクター辞書</h3>
          <span className="text-xs text-fg-subtle">{actors.length} 件</span>
          <a
            href="/app/actors"
            className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            脅威評価・活動を見る
          </a>
        </div>
        <input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="名前・別名で検索"
          className="bg-surface-2 border border-border-subtle rounded px-2 py-1.5 text-sm text-fg w-48"
        />
      </div>

      <p className="m-0 text-sm text-fg-muted">
        別名を変えると、そのアクターを**名指ししている SIR** の該当が静かに外れる。
        下の「参照 SIR」を確認してから編集する。
      </p>

      <div className="space-y-1.5">
        {actors.map((a) => (
          <div
            key={a.id}
            className="bg-surface-1 border border-border-subtle rounded p-2.5 flex items-start justify-between gap-3"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-semibold text-fg">{a.canonical}</span>
                <span className="font-mono text-[11px] text-fg-subtle">{a.id}</span>
                {a.nation && (
                  <span className="text-[11px] text-fg-subtle border border-border-default rounded px-1">
                    {a.nation}
                  </span>
                )}
              </div>
              <div className="mt-0.5 text-[12px] text-fg-subtle">
                別名 {(a.aliases ?? []).length} 件
                {a.used_by_pirs && a.used_by_pirs.length > 0 ? (
                  <> · 参照 SIR: {a.used_by_pirs.join(", ")}</>
                ) : (
                  <> · 参照 SIR なし</>
                )}
              </div>
            </div>
            <button
              onClick={() => setEditing(a)}
              className="shrink-0 inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent border border-border-subtle rounded px-2 py-1"
            >
              <Pencil className="h-3.5 w-3.5" /> 編集
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
