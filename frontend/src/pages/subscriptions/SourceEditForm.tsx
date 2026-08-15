// 購読ソース 1 件の取得設定を編集するフォーム (2026-08-15)。
// 登録ウィザードと lifecycle 操作 (有効/無効・削除・フォルダ一括) の間に空いていた
// 「後から直す」経路。サイト移転で URL が変わった / HTML 改修でセレクタが壊れた、を
// 削除→再登録せずに直せるようにする。backend: src/ui/api/_source_editor.py。
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { sourcesV2Api, type EditableSource, type SourceUpdate } from "../../api/sources_v2";
import { Spinner } from "../../components/Spinner";

// 取得先 URL の呼び名は transport で違う (同じ「URL」でも意味が別物)。
const URL_LABEL: Record<string, string> = {
  rss: "フィード URL",
  atom: "フィード URL",
  sitemap: "サイトマップ URL",
  html_scraper: "一覧ページ URL",
};

export function SourceEditForm({
  feedId,
  folders,
  onSaved,
  onCancel,
}: {
  feedId: string;
  folders: string[];
  onSaved: (nextFeedId: string) => void;
  onCancel: () => void;
}) {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["source-editable", feedId],
    queryFn: () => sourcesV2Api.getEditable(feedId),
  });
  const [form, setForm] = useState<EditableSource | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => setForm(data ?? null), [data]);

  const saveMut = useMutation({
    mutationFn: (patch: SourceUpdate) => sourcesV2Api.update(patch),
    onSuccess: (res) => {
      if (res.error) {
        setMsg(`保存に失敗しました: ${res.error}`);
        return;
      }
      qc.invalidateQueries({ queryKey: ["subscriptions"] });
      qc.invalidateQueries({ queryKey: ["source-editable"] });
      onSaved(res.feed_id);
    },
    onError: (e: unknown) =>
      setMsg(`保存に失敗しました: ${e instanceof Error ? e.message : String(e)}`),
  });

  if (isLoading) return <div className="flex items-center gap-2 text-xs text-fg-subtle"><Spinner size="xs" /> 読み込み中…</div>;
  if (error || !data || !form)
    return <div className="text-xs text-critical">編集内容を読み込めませんでした</div>;

  // 変更されたフィールドだけ送る (未指定 = 変更しない)。
  // 表示名はヘッダの「名称変更」が担当 (同じ操作の入口を 2 つ作らない)。
  const patch: SourceUpdate = { feed_id: feedId };
  if (form.url !== data.url) patch.url = form.url;
  if (form.folder !== data.folder) patch.folder = form.folder;
  if (form.enabled !== data.enabled) patch.enabled = form.enabled;
  if (form.article_link_selector !== data.article_link_selector)
    patch.article_link_selector = form.article_link_selector ?? "";
  if (form.url_include_pattern !== data.url_include_pattern)
    patch.url_include_pattern = form.url_include_pattern ?? "";
  if (form.max_posts_per_run !== data.max_posts_per_run && form.max_posts_per_run != null)
    patch.max_posts_per_run = form.max_posts_per_run;
  const dirty = Object.keys(patch).length > 1;
  const urlChanged = patch.url !== undefined;

  const field = "w-full bg-surface-3 border border-border-default rounded px-2 py-1 text-sm text-fg";
  const label = "block text-xs text-fg-subtle mb-1";

  return (
    <div className="bg-surface-2 rounded p-3 space-y-3">
      <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
        取得設定を編集
      </h4>

      <div>
        <label className={label}>{URL_LABEL[form.transport] ?? "URL"}</label>
        <input
          value={form.url}
          onChange={(e) => setForm({ ...form, url: e.target.value })}
          className={`${field} font-mono text-xs`}
          spellCheck={false}
        />
        {urlChanged && form.url_is_identity && (
          <div className="mt-1.5 flex gap-1.5 items-start rounded border border-warning/40 bg-warning-soft p-1.5 text-[11px] text-warning">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            <span>
              RSS は URL がソースの識別子です。変更すると、これまでの記事は旧 URL に
              紐づいたままなので<strong>運用統計 (投稿数・貢献度) は新 URL で数え直し</strong>になります。
            </span>
          </div>
        )}
      </div>

      <div className="flex gap-3 flex-wrap">
        <div className="flex-1 min-w-[140px]">
          <label className={label}>フォルダ (分類)</label>
          <input
            list="source-edit-folders"
            value={form.folder}
            onChange={(e) => setForm({ ...form, folder: e.target.value.toLowerCase() })}
            placeholder="空で未分類"
            className={`${field} font-mono text-xs`}
          />
          <datalist id="source-edit-folders">
            {folders.map((f) => (
              <option key={f} value={f} />
            ))}
          </datalist>
        </div>
        {form.max_posts_per_run != null && (
          <div className="w-36">
            <label className={label}>1 回の取得件数</label>
            <input
              type="number"
              min={1}
              max={50}
              value={form.max_posts_per_run}
              onChange={(e) =>
                setForm({ ...form, max_posts_per_run: Number(e.target.value) || 1 })
              }
              className={field}
            />
          </div>
        )}
      </div>

      {form.article_link_selector != null && (
        <div>
          <label className={label}>記事リンクのセレクタ (CSS)</label>
          <input
            value={form.article_link_selector}
            onChange={(e) => setForm({ ...form, article_link_selector: e.target.value })}
            className={`${field} font-mono text-xs`}
            spellCheck={false}
          />
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">
            一覧ページで記事へのリンクを選ぶ CSS セレクタ。サイト改修で取得が止まったらここを直す
          </p>
        </div>
      )}

      {form.url_include_pattern != null && (
        <div>
          <label className={label}>取り込む URL パターン (正規表現)</label>
          <input
            value={form.url_include_pattern}
            onChange={(e) => setForm({ ...form, url_include_pattern: e.target.value })}
            className={`${field} font-mono text-xs`}
            spellCheck={false}
          />
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">
            サイトマップ内の URL のうち、この正規表現に一致するものだけを記事として取り込む
          </p>
        </div>
      )}

      <label className="flex items-center gap-1.5 text-xs text-fg-muted">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        取得を有効にする
      </label>

      {msg && <div className="text-[11px] text-critical">{msg}</div>}

      <div className="flex items-center gap-2">
        <button
          onClick={() => saveMut.mutate(patch)}
          disabled={!dirty || saveMut.isPending}
          className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white hover:bg-accent-hover disabled:opacity-40"
        >
          {saveMut.isPending ? "保存中…" : "保存"}
        </button>
        <button onClick={onCancel} className="text-xs text-fg-subtle hover:text-fg">
          取消
        </button>
        {dirty && <span className="text-[11px] text-fg-subtle">未保存の変更があります</span>}
      </div>
    </div>
  );
}
