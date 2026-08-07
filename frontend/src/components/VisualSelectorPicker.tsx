// Visual selector picker: 対象ページを same-origin proxy で iframe 描画し、
// ユーザのクリックから記事リスト selector を決定的に導出する (Inoreader 風)。
//
// iframe 内の picker-overlay.js が postMessage で selector / samples を送ってくる。
// 受信後 explicit preview API を叩いて正式な SourceCandidate を得る。

import { useEffect, useRef, useState } from "react";
import { sourcesV2Api, type SourceCandidate } from "../api/sources_v2";
import { Check, AlertTriangle } from "lucide-react";

type RenderMode = "auto" | "js";

interface PickerMessage {
  source?: string;
  type: "ready" | "selected" | "no_anchor";
  selector?: string;
  titleSelector?: string;
  matchCount?: number;
  samples?: { title: string; url: string }[];
}

interface Props {
  listingUrl: string;
  initialSelector?: string;
  onConfirm: (candidate: SourceCandidate) => void;
  onBack: () => void;
}

export function VisualSelectorPicker({
  listingUrl,
  initialSelector,
  onConfirm,
  onBack,
}: Props) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [renderMode, setRenderMode] = useState<RenderMode>("auto");
  const [selector, setSelector] = useState<string>(initialSelector ?? "");
  const [matchCount, setMatchCount] = useState<number | null>(null);
  const [iframeReady, setIframeReady] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [candidate, setCandidate] = useState<SourceCandidate | null>(null);
  const [error, setError] = useState<string | null>(null);

  const proxyUrl = sourcesV2Api.proxyPageUrl(listingUrl, renderMode);

  // iframe からの selector 受信
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      // iframe は sandbox="allow-scripts" (allow-same-origin なし) で opaque origin のため、
      // origin 照合は使えない。送信元が自分の iframe の contentWindow であることで認証する
      // (security-review M4: allow-same-origin を外し sandbox escape を構造的に不能化)。
      if (!iframeRef.current || e.source !== iframeRef.current.contentWindow) return;
      const data = e.data as PickerMessage;
      if (!data || data.source !== "cti-picker") return;
      if (data.type === "ready") {
        setIframeReady(true);
        if (initialSelector && iframeRef.current.contentWindow) {
          // opaque origin 宛なので targetOrigin は "*"。受信側は e.source で親を検証する。
          iframeRef.current.contentWindow.postMessage(
            { type: "highlight", selector: initialSelector },
            "*",
          );
        }
      } else if (data.type === "no_anchor") {
        setError("クリックした位置にリンクが見つかりません。記事タイトルをクリックしてください。");
      } else if (data.type === "selected" && data.selector) {
        setError(null);
        setSelector(data.selector);
        setMatchCount(data.matchCount ?? null);
        void runPreview(data.selector);
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSelector, listingUrl]);

  async function runPreview(sel: string) {
    if (!sel.trim()) return;
    setPreviewing(true);
    setError(null);
    try {
      const res = await sourcesV2Api.previewHtmlListingExplicit(listingUrl, sel.trim());
      if (res.error || !res.candidate) {
        setError(res.error ?? "サンプル取得に失敗しました");
        setCandidate(null);
      } else {
        setCandidate(res.candidate);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "サンプル取得に失敗しました");
      setCandidate(null);
    } finally {
      setPreviewing(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="bg-accent-subtle border border-accent/30 rounded p-3 space-y-1">
        <div className="text-sm font-semibold text-fg">記事リストのビジュアル選択</div>
        <p className="text-xs text-fg-muted m-0">
          下のページ内で<strong>記事タイトルのリンクをクリック</strong>すると、同種の記事
          すべてを抽出するルールを自動生成します。崩れて見えても選択は機能します。
        </p>
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-fg-subtle font-mono break-all">{listingUrl}</div>
        <label className="flex items-center gap-1.5 text-xs text-fg-muted whitespace-nowrap">
          <input
            type="checkbox"
            checked={renderMode === "js"}
            onChange={(e) => {
              setRenderMode(e.target.checked ? "js" : "auto");
              setIframeReady(false);
            }}
          />
          JavaScript で再描画（動的表示サイト用）
        </label>
      </div>

      <div className="border border-border-subtle rounded overflow-hidden bg-surface-1">
        {!iframeReady && (
          <div className="px-3 py-2 text-[11px] text-fg-subtle">ページ読み込み中…</div>
        )}
        {/* sandbox は allow-scripts のみ (allow-same-origin なし)。proxied ページは opaque
            origin となり、親 DOM / cookie へ到達できず frameElement で自身の sandbox を外す
            escape も不能 (security-review M4)。CSS / 画像は <base href> 経由で従来どおり読み込む。 */}
        <iframe
          ref={iframeRef}
          src={proxyUrl}
          title="ソースページのプレビュー"
          sandbox="allow-scripts"
          className="w-full"
          style={{ height: "420px", border: "none" }}
        />
      </div>

      <div className="space-y-1.5">
        <label className="block text-xs text-fg-muted">
          抽出ルール (クリックで自動入力 / 手動編集可)
        </label>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={selector}
            onChange={(e) => setSelector(e.target.value)}
            placeholder="例: .views-row .field--name-title a"
            className="flex-1 bg-surface-2 border border-border-subtle rounded px-3 py-2 text-sm font-mono"
          />
          <button
            onClick={() => void runPreview(selector)}
            disabled={!selector.trim() || previewing}
            className="bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-fg-subtle text-white px-3 py-2 rounded text-sm font-semibold whitespace-nowrap"
          >
            {previewing ? "…" : "サンプル取得"}
          </button>
        </div>
        {matchCount !== null && (
          <div className="text-[11px] text-fg-subtle">
            ページ内で一致: {matchCount} 件
          </div>
        )}
      </div>

      {error && (
        <div className="bg-warning-soft border border-warning/40 rounded p-2.5 text-xs text-fg inline-flex items-start gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
          <span>{error}</span>
        </div>
      )}

      {candidate && (
        <div className="bg-surface-1 border border-border-subtle rounded">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wider text-fg-muted">
            抽出結果 ({candidate.preview_articles.length} 件)
          </div>
          <ul className="divide-y divide-border-subtle">
            {candidate.preview_articles.map((a, i) => (
              <li key={i} className="px-3 py-2 text-xs">
                <span className="text-fg font-medium">{a.title}</span>
                <div className="text-[10px] text-fg-subtle font-mono break-all">{a.url}</div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center justify-between">
        <button onClick={onBack} className="text-fg-subtle hover:text-fg text-sm">
          ← 戻る
        </button>
        <button
          onClick={() => candidate && onConfirm(candidate)}
          disabled={!candidate || candidate.preview_articles.length === 0}
          className="bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-fg-subtle text-white px-4 py-1.5 rounded text-sm font-semibold inline-flex items-center gap-1.5"
        >
          <Check className="h-3.5 w-3.5" />
          この抽出ルールで登録へ
        </button>
      </div>
    </div>
  );
}
