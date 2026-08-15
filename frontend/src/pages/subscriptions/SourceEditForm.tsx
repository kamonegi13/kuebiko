// 購読ソース 1 件の取得設定を編集するフォーム (2026-08-15)。
// 登録ウィザードと lifecycle 操作 (有効/無効・削除・フォルダ一括) の間に空いていた
// 「後から直す」経路。サイト移転で URL が変わった / HTML 改修でセレクタが壊れた、を
// 削除→再登録せずに直せるようにする。backend: src/ui/api/_source_editor.py。
//
// 設計:
// - 取得に関わる値を変えたら **保存前に取得テストを通す** (壊れた設定を保存すると
//   次の定期実行まで無音で気付けないため、成功するまで保存ボタンを開けない)
// - セレクタは手入力させない。登録ウィザードと同じ部品 (ビジュアル選択 / AI 検出) を
//   そのまま再利用する
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, MousePointerClick, Sparkles } from "lucide-react";
import {
  sourcesV2Api,
  type EditableSource,
  type PreviewArticle,
  type SourceUpdate,
} from "../../api/sources_v2";
import { Spinner } from "../../components/Spinner";
import { VisualSelectorPicker } from "../../components/VisualSelectorPicker";
import { FolderSelect } from "../../components/FolderSelect";

// 取得先 URL の呼び名は transport で違う (同じ「URL」でも意味が別物)。
const URL_LABEL: Record<string, string> = {
  rss: "フィード URL",
  atom: "フィード URL",
  sitemap: "サイトマップ URL",
  html_scraper: "一覧ページ URL",
};

type TestState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "ok"; items: PreviewArticle[] }
  | { status: "error"; error: string };

export function SourceEditForm({
  feedId,
  onSaved,
  onCancel,
}: {
  feedId: string;
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
  const [picking, setPicking] = useState(false);
  const [test, setTest] = useState<TestState>({ status: "idle" });
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

  // hooks は早期 return より前にまとめる (条件付き呼び出しは React で不正)。
  const detectMut = useMutation({
    mutationFn: () => sourcesV2Api.previewHtmlListing(form?.url ?? ""),
    onSuccess: (res) => {
      if (res.error || !res.candidate?.article_link_selector) {
        setTest({ status: "error", error: res.error ?? "セレクタを検出できませんでした" });
        return;
      }
      const sel = res.candidate.article_link_selector;
      setForm((f) => (f ? { ...f, article_link_selector: sel } : f));
      // 検出時に実際の記事も取れているので、そのままテスト成功として扱う。
      setTest({ status: "ok", items: res.candidate.preview_articles });
    },
    onError: (e: unknown) =>
      setTest({ status: "error", error: e instanceof Error ? e.message : String(e) }),
  });

  if (isLoading)
    return (
      <div className="flex items-center gap-2 text-xs text-fg-subtle">
        <Spinner size="xs" /> 読み込み中…
      </div>
    );
  if (error || !data || !form)
    return <div className="text-xs text-critical">編集内容を読み込めませんでした</div>;

  const isScraper = form.article_link_selector != null;

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
  // 取得結果が変わりうる項目を触ったら、取得テストの成功を保存の条件にする。
  const needsTest =
    urlChanged ||
    patch.article_link_selector !== undefined ||
    patch.url_include_pattern !== undefined;
  const canSave = dirty && (!needsTest || test.status === "ok");

  // 取得に関わる値を変えたらテスト結果は無効化する (古い成功で保存できてしまわないように)。
  const editFetching = (next: EditableSource) => {
    setForm(next);
    setTest({ status: "idle" });
  };

  async function runTest() {
    if (!form) return;
    setTest({ status: "running" });
    try {
      if (isScraper) {
        const res = await sourcesV2Api.previewHtmlListingExplicit(
          form.url,
          form.article_link_selector ?? "",
        );
        if (res.error || !res.candidate)
          setTest({ status: "error", error: res.error ?? "記事を取得できませんでした" });
        else setTest({ status: "ok", items: res.candidate.preview_articles });
        return;
      }
      const kind = form.transport === "sitemap" ? "sitemap" : "rss";
      const res = await sourcesV2Api.previewUrl(
        form.url,
        kind,
        form.url_include_pattern ?? "",
      );
      if (!res.ok) setTest({ status: "error", error: res.error ?? "記事を取得できませんでした" });
      else setTest({ status: "ok", items: res.items });
    } catch (e: unknown) {
      setTest({ status: "error", error: e instanceof Error ? e.message : String(e) });
    }
  }

  const field = "w-full bg-surface-3 border border-border-default rounded px-2 py-1 text-sm text-fg";
  const label = "block text-xs text-fg-subtle mb-1";

  // ビジュアル選択中はフォームを隠して picker に画面を渡す (登録ウィザードと同じ部品)。
  if (picking)
    return (
      <VisualSelectorPicker
        listingUrl={form.url}
        initialSelector={form.article_link_selector ?? undefined}
        onConfirm={(candidate) => {
          setForm({
            ...form,
            article_link_selector: candidate.article_link_selector ?? form.article_link_selector,
          });
          setTest({ status: "ok", items: candidate.preview_articles });
          setPicking(false);
        }}
        onBack={() => setPicking(false)}
      />
    );

  return (
    <div className="bg-surface-2 rounded p-3 space-y-3">
      <h4 className="m-0 text-[10.5px] uppercase tracking-wider text-fg-muted font-semibold">
        取得設定を編集
      </h4>

      <div>
        <label className={label}>{URL_LABEL[form.transport] ?? "URL"}</label>
        <input
          value={form.url}
          onChange={(e) => editFetching({ ...form, url: e.target.value })}
          className={`${field} font-mono text-xs`}
          spellCheck={false}
        />
        {urlChanged && form.url_is_identity && (
          <div className="mt-1.5 flex gap-1.5 items-start rounded border border-warning/40 bg-warning-soft p-1.5 text-[11px] text-warning">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            <span>
              RSS は URL がソースの識別子です。変更すると、これまでの記事は旧 URL に
              紐づいたままなので<strong>運用統計 (投稿数・貢献度) は新 URL で数え直し</strong>
              になります。
            </span>
          </div>
        )}
      </div>

      <div className="flex gap-3 flex-wrap">
        <div className="flex-1 min-w-[140px]">
          <label className={label}>フォルダ (分類)</label>
          {/* 候補・新規作成の扱いは登録ウィザードと同じ部品を共用する。 */}
          <FolderSelect value={form.folder} onChange={(v) => setForm({ ...form, folder: v })} />
        </div>
        {form.max_posts_per_run != null && (
          <div className="w-36">
            <label className={label}>1 回の取得件数</label>
            <input
              type="number"
              min={1}
              max={50}
              value={form.max_posts_per_run}
              onChange={(e) => setForm({ ...form, max_posts_per_run: Number(e.target.value) || 1 })}
              className={field}
            />
          </div>
        )}
      </div>

      {isScraper && (
        <div>
          <label className={label}>記事リンクの選び方</label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => setPicking(true)}
              className="inline-flex items-center gap-1 rounded border border-accent/60 px-2 py-1 text-xs text-accent hover:bg-accent-subtle"
            >
              <MousePointerClick className="h-3.5 w-3.5" /> ページから選び直す
            </button>
            <button
              onClick={() => detectMut.mutate()}
              disabled={detectMut.isPending}
              className="inline-flex items-center gap-1 rounded border border-border-default px-2 py-1 text-xs text-fg-muted hover:text-fg disabled:opacity-40"
            >
              {detectMut.isPending ? (
                <Spinner size="xs" />
              ) : (
                <Sparkles className="h-3.5 w-3.5" />
              )}
              AI に検出させる
            </button>
          </div>
          <div className="mt-1.5 rounded bg-surface-3 px-2 py-1 font-mono text-[11px] text-fg-muted break-all">
            {form.article_link_selector || "(未設定)"}
          </div>
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">
            一覧ページで記事へのリンクを選ぶルール。サイト改修で取得が止まったら選び直す
          </p>
        </div>
      )}

      {form.url_include_pattern != null && (
        <div>
          <label className={label}>取り込む URL パターン (正規表現)</label>
          <input
            value={form.url_include_pattern}
            onChange={(e) => editFetching({ ...form, url_include_pattern: e.target.value })}
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

      {/* 取得テスト — 変更が「実際に取れる」ことを保存前に確かめる */}
      <div className="rounded border border-border-subtle bg-surface-1 p-2 space-y-1.5">
        <div className="flex items-center gap-2">
          <button
            onClick={() => void runTest()}
            disabled={test.status === "running" || !form.url.trim()}
            className="rounded border border-accent/60 px-2.5 py-1 text-xs font-semibold text-accent hover:bg-accent-subtle disabled:opacity-40"
          >
            {test.status === "running" ? "取得中…" : "取得テスト"}
          </button>
          {needsTest && test.status !== "ok" && (
            <span className="text-[11px] text-warning">
              取得に関わる変更があります。テストに成功すると保存できます
            </span>
          )}
          {test.status === "ok" && (
            <span className="inline-flex items-center gap-1 text-[11px] text-success">
              <Check className="h-3.5 w-3.5" /> {test.items.length} 件取得できました
            </span>
          )}
        </div>
        {test.status === "error" && (
          <div className="text-[11px] text-critical">取得できませんでした: {test.error}</div>
        )}
        {test.status === "ok" && test.items.length > 0 && (
          <ul className="m-0 list-disc space-y-0.5 pl-4 text-[11px] text-fg-muted">
            {test.items.slice(0, 5).map((it) => (
              <li key={it.url} className="truncate">
                {it.title || it.url}
              </li>
            ))}
          </ul>
        )}
      </div>

      {msg && <div className="text-[11px] text-critical">{msg}</div>}

      <div className="flex items-center gap-2">
        <button
          onClick={() => saveMut.mutate(patch)}
          disabled={!canSave || saveMut.isPending}
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
