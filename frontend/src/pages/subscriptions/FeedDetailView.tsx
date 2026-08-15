// 購読ソース 1 件の詳細ビュー (Drawer の中身) と、そのライブ確認セクション。
// SubscriptionsPage.tsx が 800 行上限を超えたため verbatim 分割 (2026-08-15)。
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Repeat, Settings2, Trash2, X } from "lucide-react";
import { sourcesV2Api } from "../../api/sources_v2";
import { Spinner } from "../../components/Spinner";
import { useChannelMeta } from "../../components/channel";
import { vocabLabel } from "../../hooks/useVocab";
import { formatJstDate } from "../../utils/date";
import { SourceEditForm } from "./SourceEditForm";
import { LOW_CONTRIB_LABELS, QualityBadge, type EnrichedFeed } from "./shared";

export function FeedDetailView({
  feed: f,
  onDeleted,
  onSwitchTransport,
  readOnly = false,
}: {
  feed: EnrichedFeed;
  onDeleted: () => void;
  /** 取得方式の変更 = 別方式で登録し直す (ウィザードを開く) */
  onSwitchTransport?: (url: string, feedId: string, title: string, folder: string) => void;
  readOnly?: boolean;
}) {
  const st = f.stats;
  const chMeta = useChannelMeta();
  const qc = useQueryClient();
  const [editingName, setEditingName] = useState(false);
  const [editingSettings, setEditingSettings] = useState(false);
  const [draftName, setDraftName] = useState(f.title);
  const deleteMut = useMutation({
    mutationFn: () => sourcesV2Api.deleteSource(f.feed_id),
    onSuccess: (res) => {
      if (res.removed) onDeleted();
      else alert(`削除に失敗しました: ${res.error ?? "unknown"}`);
    },
    onError: (e: unknown) => alert(`削除に失敗しました: ${e instanceof Error ? e.message : e}`),
  });
  const renameMut = useMutation({
    mutationFn: (name: string) => sourcesV2Api.setDisplayName(f.feed_id, name),
    onSuccess: (res) => {
      if (res.affected > 0) {
        setEditingName(false);
        qc.invalidateQueries({ queryKey: ["subscriptions"] });
      } else {
        alert(`表示名の変更に失敗しました: ${res.error ?? "対象なし"}`);
      }
    },
    onError: (e: unknown) => alert(`表示名の変更に失敗しました: ${e instanceof Error ? e.message : e}`),
  });
  const toggleMut = useMutation({
    mutationFn: (action: "enable" | "disable") => sourcesV2Api.bulk([f.feed_id], action),
    onSuccess: (res) => {
      if (res.affected > 0) onDeleted(); // close + 一覧 invalidate (状態を反映)
      else alert(`状態変更に失敗しました: ${res.error ?? "対象なし"}`);
    },
    onError: (e: unknown) => alert(`状態変更に失敗しました: ${e instanceof Error ? e.message : e}`),
  });

  function confirmDelete() {
    if (window.confirm(`「${f.title}」を購読から削除しますか?（取り消せません）`)) {
      deleteMut.mutate();
    }
  }

  return (
    <div className="space-y-3">
      <div className="bg-surface-2 rounded p-3 space-y-2">
        <div className="flex items-baseline justify-between gap-2 flex-wrap">
          {editingName ? (
            <div className="flex items-center gap-1.5 flex-1 min-w-[200px]">
              <input
                autoFocus
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && draftName.trim()) renameMut.mutate(draftName.trim());
                  if (e.key === "Escape") setEditingName(false);
                }}
                className="flex-1 bg-surface-3 border border-border-default rounded px-2 py-1 text-sm text-fg"
              />
              <button
                onClick={() => draftName.trim() && renameMut.mutate(draftName.trim())}
                disabled={!draftName.trim() || renameMut.isPending}
                className="text-accent hover:underline text-xs disabled:opacity-50"
              >
                {renameMut.isPending ? "保存中…" : "保存"}
              </button>
              <button onClick={() => { setDraftName(f.title); setEditingName(false); }} className="text-fg-subtle hover:text-fg text-xs">
                取消
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 min-w-0">
              <h3 className="m-0 text-md font-semibold text-fg truncate">{f.title}</h3>
              {!readOnly && (
                <>
                  <button
                    onClick={() => { setDraftName(f.title); setEditingName(true); }}
                    title="表示名を変更"
                    className="text-fg-subtle hover:text-accent text-xs shrink-0 inline-flex items-center gap-1"
                  >
                    <Pencil className="h-3.5 w-3.5" /> 名称変更
                  </button>
                  <button
                    onClick={() => setEditingSettings((v) => !v)}
                    title="URL・フォルダ・取得条件を変更"
                    className="text-fg-subtle hover:text-accent text-xs shrink-0 inline-flex items-center gap-1"
                  >
                    <Settings2 className="h-3.5 w-3.5" /> 取得設定
                  </button>
                  {onSwitchTransport && (
                    <button
                      onClick={() =>
                        onSwitchTransport(
                          f.html_url || f.url,
                          f.feed_id,
                          f.title,
                          f.folder_labels[0] ?? "",
                        )
                      }
                      title="RSS / サイトマップ / スクレイパー を切り替える (別方式で登録し直す)"
                      className="text-fg-subtle hover:text-accent text-xs shrink-0 inline-flex items-center gap-1"
                    >
                      <Repeat className="h-3.5 w-3.5" /> 取得方式を変更
                    </button>
                  )}
                </>
              )}
            </div>
          )}
          <a href={f.html_url || f.url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline text-xs">サイト →</a>
        </div>
        {f.folder_labels.length > 0 && (
          <div className="flex gap-1.5 flex-wrap">
            {f.folder_labels.map((l) => (
              <span key={l} className="text-[10px] uppercase bg-surface-3 text-fg-muted px-1.5 py-0.5 rounded font-mono">{l}</span>
            ))}
          </div>
        )}
        <div className="text-xs text-fg-subtle break-all">{f.url}</div>
      </div>

      {editingSettings && !readOnly && (
        <SourceEditForm
          feedId={f.feed_id}
          onSaved={() => {
            setEditingSettings(false);
            onDeleted(); // Drawer を閉じて一覧を再取得 (URL 変更で feed_id が変わるため)
          }}
          onCancel={() => setEditingSettings(false)}
        />
      )}

      {st?.quality_score !== undefined && (
        <div className="bg-surface-2 rounded p-3 space-y-2">
          <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">貢献度スコア</h4>
          <div className="flex items-baseline gap-2">
            <QualityBadge score={st.quality_score} />
            <span className="text-fg-subtle text-xs">/100</span>
            {st.low_contrib_labels && st.low_contrib_labels.length > 0 && (
              <div className="flex gap-1 ml-2">
                {st.low_contrib_labels.map((l) => (
                  <span
                    key={l}
                    className={`text-[10px] px-1.5 py-0.5 rounded ${
                      l === "new"
                        ? "bg-accent-subtle text-accent border border-accent/30"
                        : "bg-warning-soft text-warning border border-warning/30"
                    }`}
                  >
                    {LOW_CONTRIB_LABELS[l] ?? l}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {st ? (
        <div className="bg-surface-2 rounded p-3 space-y-2">
          <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">運用統計 (30 日)</h4>
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <tbody className="[&>tr>th]:text-left [&>tr>th]:text-fg-muted [&>tr>th]:font-normal [&>tr>th]:py-0.5 [&>tr>th]:pr-3 [&>tr>th]:w-32 [&>tr>td]:py-0.5 [&>tr>td]:text-fg [&>tr>td]:tnum">
              <tr><th>投稿数</th><td>{st.posted_count}</td></tr>
              <tr><th>重複でスキップ</th><td>{st.dup_skipped}</td></tr>
              {(st.extract_failed_count ?? 0) > 0 && (
                <tr><th>本文抽出 失敗</th><td className="text-critical font-semibold">{st.extract_failed_count} 件 (feed は取得成功・本文が取れず記事化されない)</td></tr>
              )}
              <tr><th>重要度: 高</th><td className={st.high_count > 0 ? "text-critical font-semibold" : ""}>{st.high_count}</td></tr>
              <tr><th>重要度: 中</th><td className={st.medium_count > 0 ? "text-warning" : ""}>{st.medium_count}</td></tr>
              <tr><th>重要度: 低</th><td>{st.low_count}</td></tr>
              <tr><th>{chMeta("alert").label}</th><td>{st.alert_count}</td></tr>
              <tr><th>{chMeta("brief").label}</th><td>{st.brief_count}</td></tr>
              <tr><th>{chMeta("watch").label}</th><td>{st.watch_count}</td></tr>
              <tr><th>初回検出</th><td className="text-fg-subtle">{formatJstDate(st.first_seen_at)}</td></tr>
            </tbody>
          </table></div>
        </div>
      ) : (
        <div className="text-fg-subtle text-sm italic">この feed の運用統計データはまだありません (新規購読 / 記事なし)。</div>
      )}

      <LivePreviewSection feedId={f.feed_id} />

      <div className="border-t border-border-subtle pt-3 flex items-center justify-between gap-2">
        <span className="text-fg-subtle text-[11px]">
          状態: {f.enabled === false ? "無効" : "有効"}
        </span>
        {readOnly ? (
          <span className="text-[10px] uppercase tracking-wider bg-surface-3 text-fg-subtle px-2 py-1 rounded font-mono">閲覧専用</span>
        ) : (
          <div className="flex items-center gap-2">
            <button
              onClick={() => toggleMut.mutate(f.enabled === false ? "enable" : "disable")}
              disabled={toggleMut.isPending}
              className="border border-border-subtle text-fg hover:bg-surface-3 px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50 whitespace-nowrap inline-flex items-center gap-1.5"
            >
              {toggleMut.isPending ? "更新中…" : f.enabled === false ? <><Check className="h-3.5 w-3.5" /> 有効化</> : <>⊘ 無効化</>}
            </button>
            <button
              onClick={confirmDelete}
              disabled={deleteMut.isPending}
              className="border border-critical/50 text-critical hover:bg-critical-soft px-3 py-1.5 rounded text-sm font-semibold disabled:opacity-50 whitespace-nowrap inline-flex items-center gap-1.5"
            >
              {deleteMut.isPending ? "削除中…" : <><Trash2 className="h-3.5 w-3.5" /> 削除</>}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Phase F: ソースが今も機能しているか最新情報をライブ取得して確認する。
function LivePreviewSection({ feedId }: { feedId: string }) {
  const { data, isFetching, isError, refetch } = useQuery({
    queryKey: ["live_preview", feedId],
    queryFn: () => sourcesV2Api.livePreview(feedId),
    staleTime: 60_000, // 開くたびに連打しない (60s キャッシュ、手動再取得は可)
    retry: false,
  });

  return (
    <div className="bg-surface-2 rounded p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
          ライブプレビュー (最新取得)
        </h4>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="text-accent hover:underline text-[11px] disabled:text-fg-subtle"
        >
          {isFetching ? "取得中…" : "↻ 再取得"}
        </button>
      </div>

      {isFetching && (
        <div className="flex items-center gap-2 text-xs text-fg-subtle">
          <Spinner /> サイトから最新情報を取得中…
        </div>
      )}

      {!isFetching && (isError || (data && !data.ok)) && (
        <div className="bg-critical-soft border border-critical/40 rounded p-2 text-xs text-fg">
          <span className="inline-flex items-center gap-1"><X className="h-3.5 w-3.5" /> 取得に失敗しました{data?.error ? `: ${data.error}` : ""}</span>
          <div className="text-fg-subtle mt-1">
            登録時から site 構造が変わった / feed が落ちている可能性があります。
          </div>
        </div>
      )}

      {!isFetching && data && data.ok && (
        <>
          <div className="text-xs text-success flex items-center gap-1">
            <Check className="h-3.5 w-3.5" /> {data.items.length} 件取得 ({vocabLabel("transport", data.kind)})
          </div>
          {data.fetch_stage === "browser" && (
            <div className="text-[11px] text-warning">
              bot UA はこのサイトにブロックされるため、ブラウザ相当 UA へ自動切替して取得しました (本番の定期取得も同じ動作)。
            </div>
          )}
          <ul className="divide-y divide-border-subtle border border-border-subtle rounded">
            {data.items.map((a, i) => (
              <li key={i} className="px-2.5 py-1.5 text-xs">
                <a
                  href={a.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-fg hover:text-accent hover:underline font-medium"
                >
                  {a.title}
                </a>
                {a.published && (
                  <span className="text-fg-subtle ml-2">{formatJstDate(a.published)}</span>
                )}
                <div className="text-[10px] text-fg-subtle font-mono break-all">{a.url}</div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
