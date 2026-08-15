// 購読の「取り込む範囲」を決める欄 (サイトマップ / スクレイパー共通)。
//
// どちらも「サイトの URL 群から記事だけを選ぶ」問題を持つ。絞り込まないと採用情報・
// タグページ・対象者リンクまで CTI として取り込まれる (ENISA は sitemap 2,945 URL の
// 最新 40 件の 1/4 が recruitment、一覧ページは /topics/ /audience/ が混入)。
// どの区分が対象かは機械的に決められないので、区分を選択肢として出し、選んだ結果を
// 実サンプルで確認できるようにする。
import { useState } from "react";
import { sourcesV2Api, type PreviewArticle } from "../api/sources_v2";
import { Check, Spline } from "lucide-react";
import { Spinner } from "./Spinner";

function patternFor(host: string, segments: string[]): string {
  const h = host.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (segments.length === 0) return `^https?://${h}/.+`;
  return `^https?://${h}/(${segments.join("|")})/.+`;
}

export function ScopeField({
  mode,
  url,
  hints,
  value,
  onChange,
  articleLinkSelector = "",
}: {
  mode: "sitemap" | "scraper";
  /** サイトマップ URL または一覧ページ URL */
  url: string;
  hints: string[];
  value: string;
  onChange: (pattern: string) => void;
  /** scraper のサンプル取得に必要 (sitemap では未使用) */
  articleLinkSelector?: string;
}) {
  const host = (() => {
    try {
      return new URL(url).host;
    } catch {
      return "";
    }
  })();
  const [picked, setPicked] = useState<string[]>([]);
  const [items, setItems] = useState<PreviewArticle[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(seg: string) {
    const next = picked.includes(seg) ? picked.filter((s) => s !== seg) : [...picked, seg];
    setPicked(next);
    setItems(null);
    onChange(patternFor(host, next));
  }

  async function sample() {
    setBusy(true);
    setError(null);
    try {
      if (mode === "scraper") {
        const res = await sourcesV2Api.previewHtmlListingExplicit(
          url,
          articleLinkSelector,
          "",
          value,
        );
        if (res.error || !res.candidate) setError(res.error ?? "この条件では記事が取れません");
        else setItems(res.candidate.preview_articles);
        return;
      }
      const res = await sourcesV2Api.previewUrl(url, "sitemap", value);
      if (!res.ok) setError(res.error ?? "この条件では記事が取れません");
      else setItems(res.items);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded border border-border-subtle bg-surface-2 p-3 space-y-2">
      <div className="text-xs font-semibold text-fg inline-flex items-center gap-1.5">
        <Spline className="h-3.5 w-3.5" /> 取り込む範囲
      </div>
      <p className="m-0 text-[11px] text-fg-muted">
        {mode === "sitemap"
          ? "サイトマップはサイト全体の URL を含みます。"
          : "一覧ページには記事以外のリンク (タグ・対象者) も並びます。"}
        区分を選ばないと<strong>記事でないページまで取り込まれます</strong>。
      </p>
      {hints.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {hints.map((seg) => (
            <button
              key={seg}
              onClick={() => toggle(seg)}
              className={`rounded border px-2 py-0.5 font-mono text-[11px] ${
                picked.includes(seg)
                  ? "border-accent bg-accent-subtle text-accent"
                  : "border-border-subtle text-fg-muted hover:text-fg"
              }`}
            >
              /{seg}
            </button>
          ))}
          {picked.length === 0 && (
            <span className="text-[11px] text-warning">未選択 = サイト全体</span>
          )}
        </div>
      )}
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setItems(null);
        }}
        spellCheck={false}
        className="w-full rounded border border-border-subtle bg-surface-1 px-2 py-1 font-mono text-[11px] text-fg-muted"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={() => void sample()}
          disabled={busy}
          className="rounded border border-accent/60 px-2 py-0.5 text-[11px] text-accent hover:bg-accent-subtle disabled:opacity-40"
        >
          {busy ? <Spinner size="xs" /> : "この条件で確認"}
        </button>
        {items && (
          <span className="inline-flex items-center gap-1 text-[11px] text-success">
            <Check className="h-3.5 w-3.5" /> {items.length} 件
          </span>
        )}
        {error && <span className="text-[11px] text-critical">{error}</span>}
      </div>
      {items && items.length > 0 && (
        <ul className="m-0 list-disc space-y-0.5 pl-4 text-[11px] text-fg-muted">
          {items.slice(0, 5).map((it) => (
            <li key={it.url} className="truncate">
              {it.title || it.url}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
