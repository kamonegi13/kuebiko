// Phase F: 統合 6-step Source 登録 wizard (Inoreader 風 preview gate)。
//
// Step 1: URL 入力
// Step 2: discover → 候補 list (preview articles 付き)
// Step 3: user pick OR "違う、 HTML 解析 mode へ"
// Step 4a: (RSS/sitemap/scraper picked) name + folder + confirm → register
// Step 4b: (HTML mode) listing URL 入力
// Step 5b: LLM CSS selector preview + 承認
// Step 6: done

import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Drawer } from "./Drawer";
import { Spinner, ButtonSpinner, LLMProgress, PulseSkeleton } from "./Spinner";
import { VisualSelectorPicker } from "./VisualSelectorPicker";
import { FolderSelect } from "./FolderSelect";
import { Check, AlertTriangle, Sparkles } from "lucide-react";
import {
  sourcesV2Api,
  type SourceCandidate,
  type PreviewArticle,
  type DiscoverResponse,
} from "../api/sources_v2";
import { vocabLabel } from "../hooks/useVocab";

type Step =
  | { kind: "url" }
  | { kind: "discovering" }
  | { kind: "candidates"; result: DiscoverResponse }
  | { kind: "html_url_input"; reason: "user_intent" | "no_candidates" }
  | { kind: "html_previewing" }
  | { kind: "html_preview"; candidate: SourceCandidate }
  | { kind: "html_picker"; listingUrl: string; initialSelector?: string }
  | { kind: "confirm"; candidate: SourceCandidate }
  | { kind: "registering"; candidate: SourceCandidate }
  | {
      kind: "done";
      result: {
        name: string;
        commit: string | null;
        target_yaml: string;
        transport: string;
        appended: boolean;
        // 取得方式の変更で旧設定を置き換えたときだけセット
        replaced?: { title: string; removed: boolean; error?: string };
      };
    };

// detected_via (候補の検出経路) の日本語ラベルは backend vocab "detected_via" (vocabLabel) を
// SSoT に解決する (静的 value→label マップは持たない)。

interface Props {
  isOpen: boolean;
  onClose: () => void;
  /** Discovery panel 等から起動するとき URL pre-fill */
  initialUrl?: string;
  /** 取得方式の変更 (RSS ⇄ サイトマップ ⇄ スクレイパー) で置き換える既存ソース。
   *  登録が成功したあとに旧設定を削除する (削除は登録成功後 = 取りこぼしを作らない)。 */
  replacing?: { feedId: string; title: string; folder: string };
}

export function AddSourceWizardV2({ isOpen, onClose, initialUrl = "", replacing }: Props) {
  const qc = useQueryClient();
  const [step, setStep] = useState<Step>({ kind: "url" });
  const [url, setUrl] = useState("");
  const [htmlListingUrl, setHtmlListingUrl] = useState("");
  const [htmlHint, setHtmlHint] = useState("");
  const [error, setError] = useState<string | null>(null);
  // confirm form
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState(""); // 購読一覧に表示される名称
  const [folder, setFolder] = useState("");
  const [enabled, setEnabled] = useState(true);

  // 取り直し (取得方式の変更) では既存の分類・表示名を初期値にする。
  useEffect(() => {
    if (!isOpen || !replacing) return;
    setFolder((cur) => cur || replacing.folder);
    setDisplayName((cur) => cur || replacing.title);
  }, [isOpen, replacing]);

  // initialUrl 注入 (Discovery → openSourceWizard 経路)
  useEffect(() => {
    if (isOpen && initialUrl && !url) setUrl(initialUrl);
  }, [isOpen, initialUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  function reset() {
    setStep({ kind: "url" });
    setUrl("");
    setHtmlListingUrl("");
    setHtmlHint("");
    setError(null);
    setName("");
    setDisplayName("");
    setFolder("news_en");
    setEnabled(true);
  }
  function handleClose() {
    reset();
    onClose();
  }

  const discoverMut = useMutation({
    mutationFn: (u: string) => sourcesV2Api.discover(u, 5),
    onMutate: () => setStep({ kind: "discovering" }),
    onSuccess: (res) => {
      setError(null);
      setStep({ kind: "candidates", result: res });
    },
    onError: (e: Error) => {
      setError(e.message);
      setStep({ kind: "url" });
    },
  });

  const previewMut = useMutation({
    mutationFn: (req: { listing_url: string; hint: string }) =>
      sourcesV2Api.previewHtmlListing(req.listing_url, req.hint),
    onMutate: () => setStep({ kind: "html_previewing" }),
    onSuccess: (res) => {
      if (res.error || !res.candidate) {
        setError(res.error || "LLM preview 失敗");
        setStep({ kind: "html_url_input", reason: "user_intent" });
        return;
      }
      setError(null);
      setStep({ kind: "html_preview", candidate: res.candidate });
    },
    onError: (e: Error) => {
      setError(e.message);
      setStep({ kind: "html_url_input", reason: "user_intent" });
    },
  });

  const registerMut = useMutation({
    mutationFn: (req: { candidate: SourceCandidate; name: string; folder: string; enabled: boolean }) =>
      sourcesV2Api.register(req),
    onSuccess: async (res) => {
      setError(null);
      // 取得方式の変更なら、新設定の登録に成功したあとで旧設定を消す
      // (先に消すと登録に失敗したときソースが丸ごと消えてしまう)。
      let replaced: { title: string; removed: boolean; error?: string } | undefined;
      if (replacing) {
        try {
          const del = await sourcesV2Api.deleteSource(replacing.feedId);
          replaced = {
            title: replacing.title,
            removed: del.removed,
            error: del.error ?? undefined,
          };
        } catch (e: unknown) {
          replaced = {
            title: replacing.title,
            removed: false,
            error: e instanceof Error ? e.message : String(e),
          };
        }
      }
      setStep({
        kind: "done",
        result: {
          name,
          commit: res.commit,
          target_yaml: res.target_yaml,
          transport: res.transport,
          appended: res.appended_to_cluster,
          replaced,
        },
      });
      qc.invalidateQueries({ queryKey: ["subscriptions"] });
    },
    onError: (e: Error) => {
      setError(e.message);
      if (step.kind === "registering") setStep({ kind: "confirm", candidate: step.candidate });
    },
  });

  return (
    <Drawer
      isOpen={isOpen}
      onClose={handleClose}
      title={replacing ? "取得方式を変更" : "ソースを追加"}
      widthClass="md:w-[48rem]"
    >
      <div className="space-y-4">
        {replacing && (
          <div className="rounded border border-accent/40 bg-accent-subtle px-3 py-2 text-xs text-fg-muted">
            <strong className="text-fg">{replacing.title}</strong> を別の方式で取り直します。
            新しい設定の登録に成功したら、古い設定は自動で削除します。
          </div>
        )}
        <StepIndicator step={step} />

        {error && (
          <div className="bg-critical-soft border border-critical rounded-md px-3 py-2 text-critical text-sm inline-flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {step.kind === "url" && (
          <Step1Url url={url} setUrl={setUrl} onSubmit={() => discoverMut.mutate(url.trim())} />
        )}

        {step.kind === "discovering" && (
          <div className="space-y-2">
            <div className="inline-flex items-center gap-2 text-sm text-fg-muted">
              <Spinner size="sm" />
              <span>RSS / Atom / sitemap を並行して確認中...（記事サンプルの取得を含む）</span>
            </div>
            <PulseSkeleton lines={4} />
          </div>
        )}

        {step.kind === "candidates" && (
          <Step3Candidates
            result={step.result}
            onPick={(c) => {
              suggestNameFromCandidate(c, setName, setDisplayName);
              setStep({ kind: "confirm", candidate: c });
            }}
            onHtmlMode={() =>
              setStep({
                kind: "html_url_input",
                reason: step.result.candidates.length === 0 ? "no_candidates" : "user_intent",
              })
            }
            onBack={() => setStep({ kind: "url" })}
          />
        )}

        {step.kind === "html_url_input" && (
          <Step4bHtmlUrl
            originalUrl={url}
            listingUrl={htmlListingUrl}
            setListingUrl={setHtmlListingUrl}
            hint={htmlHint}
            setHint={setHtmlHint}
            reason={step.reason}
            onSubmit={(u) =>
              previewMut.mutate({ listing_url: u, hint: htmlHint.trim() })
            }
            onVisualPick={(u) => setStep({ kind: "html_picker", listingUrl: u })}
            onBack={() =>
              step.reason === "no_candidates"
                ? setStep({ kind: "url" })
                : setStep({ kind: "candidates", result: discoverMut.data! })
            }
          />
        )}

        {step.kind === "html_previewing" && (
          <LLMProgress label="LLM で抽出ルール + サンプルを抽出中" />
        )}

        {step.kind === "html_preview" && (
          <Step5bHtmlPreview
            candidate={step.candidate}
            onPick={() => {
              suggestNameFromCandidate(step.candidate, setName, setDisplayName);
              setStep({ kind: "confirm", candidate: step.candidate });
            }}
            onRefine={() => setStep({ kind: "html_url_input", reason: "user_intent" })}
            onVisualPick={() =>
              setStep({
                kind: "html_picker",
                listingUrl: step.candidate.fetch_url,
                initialSelector: step.candidate.article_link_selector ?? undefined,
              })
            }
          />
        )}

        {step.kind === "html_picker" && (
          <VisualSelectorPicker
            listingUrl={step.listingUrl}
            initialSelector={step.initialSelector}
            onConfirm={(candidate) => {
              suggestNameFromCandidate(candidate, setName, setDisplayName);
              setStep({ kind: "confirm", candidate });
            }}
            onBack={() => setStep({ kind: "html_url_input", reason: "user_intent" })}
          />
        )}

        {step.kind === "confirm" && (
          <Step6Confirm
            candidate={step.candidate}
            name={name}
            setName={setName}
            displayName={displayName}
            setDisplayName={setDisplayName}
            folder={folder}
            setFolder={setFolder}
            enabled={enabled}
            setEnabled={setEnabled}
            loading={registerMut.isPending}
            onRegister={() =>
              registerMut.mutate({
                // 表示名はユーザ編集値を candidate.source_name に上書きして保存
                // (backend は全 transport で source_name を表示名に使う)
                candidate: {
                  ...step.candidate,
                  source_name: displayName.trim() || step.candidate.source_name,
                },
                name: name.trim(),
                folder: folder.trim(), enabled,
              })
            }
            onBack={() =>
              step.candidate.transport === "html_scraper"
                ? setStep({ kind: "html_preview", candidate: step.candidate })
                : discoverMut.data
                ? setStep({ kind: "candidates", result: discoverMut.data })
                : setStep({ kind: "url" })
            }
          />
        )}

        {step.kind === "registering" && (
          <div className="inline-flex items-center gap-2 text-sm text-fg-muted">
            <Spinner size="sm" />
            <span>登録処理中...</span>
          </div>
        )}

        {step.kind === "done" && (
          <Step7Done result={step.result} onClose={handleClose} onAddAnother={reset} />
        )}
      </div>
    </Drawer>
  );
}

// ---------- Helpers ----------

function slugifyKebab(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

function suggestNameFromCandidate(
  c: SourceCandidate,
  setName: (v: string) => void,
  setDisplayName?: (v: string) => void,
) {
  let host = "";
  try {
    host = new URL(c.fetch_url).hostname.replace(/^www\./, "");
  } catch {
    /* fetch_url が URL でない場合は source_name から生成 */
  }
  // 内部 ID は host → source_name → "source" の順で必ず妥当な kebab を生成
  let base = slugifyKebab(host) || slugifyKebab(c.source_name) || "source";
  if (!/^[a-z0-9]/.test(base)) base = `s-${base}`;
  setName(base);
  // 表示名は自動検出した source_name を初期値に (ユーザが編集可能)
  setDisplayName?.(c.source_name || host || base);
}

function StepIndicator({ step }: { step: Step }) {
  const labels = [
    { key: "url", label: "URL" },
    { key: "discover", label: "候補" },
    { key: "confirm", label: "登録" },
    { key: "done", label: "完了" },
  ];
  const activeIndex =
    step.kind === "url"
      ? 0
      : step.kind === "discovering" ||
        step.kind === "candidates" ||
        step.kind === "html_url_input" ||
        step.kind === "html_previewing" ||
        step.kind === "html_preview"
      ? 1
      : step.kind === "confirm" || step.kind === "registering"
      ? 2
      : 3;
  return (
    <ol className="flex items-center gap-1.5 text-xs flex-wrap">
      {labels.map((l, i) => {
        const isActive = i === activeIndex;
        const isDone = i < activeIndex;
        return (
          <li key={l.key} className="flex items-center gap-1">
            <span
              className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold ${
                isActive
                  ? "bg-accent text-white"
                  : isDone
                  ? "bg-success-soft text-success border border-success/40"
                  : "bg-surface-2 text-fg-subtle border border-border-subtle"
              }`}
            >
              {isDone ? <Check className="h-3.5 w-3.5" /> : i + 1}
            </span>
            <span className={isActive ? "text-fg font-semibold" : "text-fg-subtle"}>{l.label}</span>
            {i < labels.length - 1 && <span className="text-fg-subtle">→</span>}
          </li>
        );
      })}
    </ol>
  );
}

// ---------- Step 1: URL ----------

function Step1Url({
  url,
  setUrl,
  onSubmit,
}: {
  url: string;
  setUrl: (v: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-muted">
        トップページ / カテゴリページ / 記事一覧など任意の URL を入力してください。
        RSS / Atom / sitemap を確認して、 各候補に <strong>サンプル 5 件</strong> を添えて提示します。
      </p>
      <input
        type="url"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://www.example.com/news/"
        className="w-full bg-surface-2 border border-border-subtle rounded px-3 py-2 text-sm"
        onKeyDown={(e) => {
          if (e.key === "Enter" && url.trim()) onSubmit();
        }}
      />
      <div className="flex justify-end">
        <button
          onClick={onSubmit}
          disabled={!url.trim()}
          className="bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-fg-subtle text-white px-4 py-1.5 rounded text-sm font-semibold"
        >
          候補を探す
        </button>
      </div>
      <details className="text-xs text-fg-subtle">
        <summary className="cursor-pointer hover:text-fg">どんな URL を入力すれば良いか</summary>
        <ul className="mt-1.5 space-y-0.5 pl-4 list-disc">
          <li>サイトのトップ URL: 標準的な feed があれば即発見</li>
          <li>カテゴリページ URL: サブカテゴリ専用 feed が見つかる場合多 (例 ITmedia /enterprise/)</li>
          <li>記事一覧ページ URL: RSS が無くても HTML 解析モードへ移行可能</li>
        </ul>
      </details>
    </div>
  );
}

// ---------- Step 3: candidates ----------

function Step3Candidates({
  result,
  onPick,
  onHtmlMode,
  onBack,
}: {
  result: DiscoverResponse;
  onPick: (c: SourceCandidate) => void;
  onHtmlMode: () => void;
  onBack: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <p className="text-sm text-fg-muted m-0">
          {result.candidates.length > 0
            ? `${result.candidates.length} 件の候補。 サンプルを見て CTI に適合するものを選ぶか、 HTML 解析モードに移行してください。`
            : "RSS / sitemap 候補が見つかりませんでした。 記事一覧の URL を指定して HTML 解析モードに移行してください。"}
        </p>
      </div>

      {/* 0 候補時、 fetch 失敗 (到達不可 / bot 対策) の明確な理由を目立たせる。
          collapsed な probe notes だと bot ブロックを見落として HTML mode で再度 timeout するため。 */}
      {result.candidates.length === 0 && result.notes.length > 0 && (
        <div className="bg-surface-1 border border-warning/50 rounded p-3 text-sm text-warning flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" aria-hidden />
          <span>{result.notes[0]}</span>
        </div>
      )}

      {result.candidates.length > 0 && (
        <ul className="space-y-2">
          {result.candidates.map((c, i) => (
            <CandidateCard key={`${c.transport}-${c.fetch_url}-${i}`} candidate={c} onPick={() => onPick(c)} />
          ))}
        </ul>
      )}

      {/* HTML scrape mode への移行 button (常時表示、 0 候補時は default action 風) */}
      <div className={`bg-surface-1 border ${result.candidates.length === 0 ? "border-accent/40 bg-accent-subtle" : "border-border-subtle"} rounded p-3 space-y-2`}>
        <div className="text-sm font-semibold text-fg">
          RSS じゃなくて、 記事一覧ページを直接解析したい
        </div>
        <p className="text-xs text-fg-muted m-0">
          上の候補が意図と違う、 もしくは {result.candidates.length === 0 ? "候補が無い" : "他に取り込みたい記事一覧がある"} 場合、
          記事一覧ページの URL を指定して LLM に抽出ルールを提案させます。
        </p>
        <button
          onClick={onHtmlMode}
          className="bg-accent hover:bg-accent-hover text-white px-3 py-1.5 rounded text-sm font-semibold"
        >
          HTML 解析モードに移行 →
        </button>
      </div>

      {result.notes.length > 0 && (
        <details className="text-xs text-fg-subtle">
          <summary className="cursor-pointer hover:text-fg">確認メモ ({result.notes.length})</summary>
          <ul className="mt-1 pl-4 list-disc">
            {result.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </details>
      )}

      <div className="flex justify-start">
        <button onClick={onBack} className="text-fg-subtle hover:text-fg text-sm">
          ← URL を変更
        </button>
      </div>
    </div>
  );
}

function CandidateCard({
  candidate: c,
  onPick,
}: {
  candidate: SourceCandidate;
  onPick: () => void;
}) {
  const transportLabel = vocabLabel("transport", c.transport);

  const fresh = c.quality.freshness_days;
  const freshLabel =
    fresh === null
      ? "更新不明"
      : fresh === 0
      ? "今日更新"
      : fresh < 7
      ? `${fresh} 日前`
      : fresh < 30
      ? `${Math.floor(fresh / 7)} 週間前`
      : fresh < 365
      ? `${Math.floor(fresh / 30)} ヶ月前`
      : `${Math.floor(fresh / 365)} 年前`;
  const freshTone =
    fresh === null
      ? "text-fg-subtle"
      : fresh <= 7
      ? "text-success"
      : fresh <= 30
      ? "text-fg-muted"
      : fresh <= 90
      ? "text-warning"
      : "text-critical";

  const healthTone =
    c.quality.health_score >= 70
      ? "text-success bg-success-soft border-success/40"
      : c.quality.health_score >= 40
      ? "text-warning bg-warning-soft border-warning/40"
      : "text-critical bg-critical-soft border-critical/40";

  return (
    <li>
      <div className="border border-border-subtle hover:border-accent rounded-lg overflow-hidden">
        <div className="p-3 space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-3 font-mono uppercase text-fg-muted">
              {transportLabel}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded border ${healthTone} font-semibold tnum`}>
              Q {c.quality.health_score}
            </span>
            <span className="text-sm text-fg font-semibold">{c.source_name}</span>
          </div>
          <div className="text-xs text-accent font-mono break-all">{c.path_display}</div>
          <div className="flex items-center gap-3 flex-wrap text-[11px]">
            <span className="text-fg-muted tnum">
              <span className="text-fg font-semibold">{c.quality.entries_count}</span> 件
            </span>
            <span className={freshTone}>最終更新: {freshLabel}</span>
            <span className="text-fg-subtle">
              {vocabLabel("detected_via", c.detected_via)}
            </span>
          </div>
        </div>

        {/* preview articles (Inoreader 風 user 意図確認 gate) */}
        {c.preview_articles.length > 0 && (
          <div className="bg-surface-2 border-t border-border-subtle p-2.5">
            <div className="text-[10px] uppercase tracking-wider text-fg-muted mb-1.5">
              直近 {c.preview_articles.length} 記事 (このソースから取り込まれる例)
            </div>
            <ul className="space-y-0.5">
              {c.preview_articles.map((a, i) => (
                <PreviewArticleRow key={i} article={a} />
              ))}
            </ul>
          </div>
        )}

        <div className="p-2 bg-surface-1 flex justify-end border-t border-border-subtle">
          <button
            onClick={onPick}
            className="bg-accent hover:bg-accent-hover text-white px-3 py-1 rounded text-xs font-semibold inline-flex items-center gap-1"
          >
            <Check className="h-3.5 w-3.5" />
            これを使う
          </button>
        </div>
      </div>
    </li>
  );
}

function PreviewArticleRow({ article: a }: { article: PreviewArticle }) {
  const pubLabel = a.published ? new Date(a.published).toLocaleDateString("ja-JP", {
    month: "short", day: "numeric",
  }) : "";
  return (
    <li className="text-xs leading-tight">
      <span className="text-fg-subtle tnum mr-2">{pubLabel}</span>
      <a
        href={a.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-fg hover:text-accent hover:underline line-clamp-1"
      >
        {a.title}
      </a>
    </li>
  );
}

// ---------- Step 4b: HTML URL input ----------

function Step4bHtmlUrl({
  originalUrl,
  listingUrl,
  setListingUrl,
  hint,
  setHint,
  reason,
  onSubmit,
  onVisualPick,
  onBack,
}: {
  originalUrl: string;
  listingUrl: string;
  setListingUrl: (v: string) => void;
  hint: string;
  setHint: (v: string) => void;
  reason: "user_intent" | "no_candidates";
  onSubmit: (url: string) => void;
  onVisualPick: (url: string) => void;
  onBack: () => void;
}) {
  // initialize listing URL with originalUrl if empty
  useEffect(() => {
    if (!listingUrl && originalUrl) setListingUrl(originalUrl);
  }, [originalUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3">
      <div className="bg-accent-subtle border border-accent/30 rounded p-3 space-y-1.5">
        <div className="text-sm font-semibold text-fg">HTML 解析モード</div>
        <p className="text-xs text-fg-muted m-0">
          {reason === "no_candidates"
            ? "RSS / sitemap が見つからなかったので、 記事一覧ページの URL を指定してください。 LLM が抽出ルールを提案しサンプルを抽出します。LLM が失敗 (アクセス拒否など) しても「ビジュアルで選ぶ」で直接選択できます。"
            : "記事一覧ページの URL を指定してください。 LLM が抽出ルールを提案しサンプルを抽出します。LLM が失敗 (アクセス拒否など) しても「ビジュアルで選ぶ」で直接選択できます。"}
        </p>
      </div>

      <div className="space-y-2.5">
        <div>
          <label className="block text-xs text-fg-muted mb-1">記事一覧ページの URL</label>
          <input
            type="url"
            value={listingUrl}
            onChange={(e) => setListingUrl(e.target.value)}
            placeholder="例: https://www.example.com/news/ や /blog/ や /advisories/"
            className="w-full bg-surface-2 border border-border-subtle rounded px-3 py-2 text-sm"
          />
          {originalUrl && originalUrl !== listingUrl && (
            <div className="text-[11px] text-fg-subtle mt-1">
              元の URL を使う場合: <code className="text-accent">{originalUrl}</code>{" "}
              <button
                onClick={() => setListingUrl(originalUrl)}
                className="text-accent hover:underline ml-1"
              >
                ← 採用
              </button>
            </div>
          )}
        </div>
        <div>
          <label className="block text-xs text-fg-muted mb-1">ヒント (任意)</label>
          <input
            type="text"
            value={hint}
            onChange={(e) => setHint(e.target.value)}
            placeholder="例: 'advisory のみ' / '最新記事一覧を対象に'"
            className="w-full bg-surface-2 border border-border-subtle rounded px-3 py-2 text-sm"
          />
        </div>
      </div>

      <div className="flex items-center justify-between gap-2">
        <button onClick={onBack} className="text-fg-subtle hover:text-fg text-sm">
          ← 戻る
        </button>
        {/* LLM 解析が失敗 (403 等) しても手動選択へ進めるよう、ビジュアル選択を常時前面に置く。
            LLM をスキップしてページを直接表示し、クリックで selector を決める。 */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => onVisualPick(listingUrl.trim())}
            disabled={!listingUrl.trim()}
            className="border border-accent/50 text-accent hover:bg-accent-subtle disabled:opacity-40 disabled:cursor-not-allowed px-3 py-1.5 rounded text-sm font-semibold"
            title="LLM を使わず、ページを直接表示してクリックで記事リストを選択します"
          >
            ビジュアルで選ぶ
          </button>
          <button
            onClick={() => onSubmit(listingUrl.trim())}
            disabled={!listingUrl.trim()}
            className="bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-fg-subtle text-white px-4 py-1.5 rounded text-sm font-semibold inline-flex items-center gap-1.5"
          >
            <Sparkles className="h-3.5 w-3.5" />
            LLM に解析させる
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- Step 5b: HTML preview ----------

function Step5bHtmlPreview({
  candidate,
  onPick,
  onRefine,
  onVisualPick,
}: {
  candidate: SourceCandidate;
  onPick: () => void;
  onRefine: () => void;
  onVisualPick: () => void;
}) {
  const ok = candidate.preview_articles.length >= 3;
  return (
    <div className="space-y-3">
      <div
        className={`rounded p-3 ${
          ok ? "bg-success-soft border border-success/40" : "bg-warning-soft border border-warning/40"
        }`}
      >
        <div className="flex items-baseline gap-3">
          <span className="text-2xl text-fg font-bold tnum">{candidate.preview_articles.length}</span>
          <span className="text-fg-muted text-sm">記事抽出 (サンプル)</span>
        </div>
        <div className="text-xs text-fg-muted mt-1 inline-flex items-start gap-1.5">
          {ok ? (
            <>
              <Check className="h-3.5 w-3.5 shrink-0 mt-px" />
              <span>取得できます。 サンプルの記事が CTI に適合するなら次へ。</span>
            </>
          ) : (
            <>
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
              <span>抽出件数が少なめ。 記事一覧の URL / ヒントを見直すか、 そのまま次へ進む。</span>
            </>
          )}
        </div>
      </div>

      {candidate.rationale && (
        <div className="bg-surface-2 rounded p-2.5 text-xs text-fg italic">
          LLM の判断理由: {candidate.rationale}
        </div>
      )}

      <div className="bg-surface-2 rounded p-2.5 text-xs space-y-1">
        <div>
          <span className="text-fg-muted">リンクの抽出ルール: </span>
          <code className="text-fg font-mono break-all">{candidate.article_link_selector}</code>
        </div>
        {candidate.title_selector && (
          <div>
            <span className="text-fg-muted">タイトルの抽出ルール: </span>
            <code className="text-fg font-mono break-all">{candidate.title_selector}</code>
          </div>
        )}
      </div>

      {/* preview articles (user 意図確認 gate) */}
      <div className="bg-surface-1 border border-border-subtle rounded">
        <div className="px-3 py-2 text-[10px] uppercase tracking-wider text-fg-muted">
          LLM が抽出した記事
        </div>
        <ul className="divide-y divide-border-subtle">
          {candidate.preview_articles.map((a, i) => (
            <li key={i} className="px-3 py-2 text-xs">
              <a
                href={a.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-fg hover:text-accent hover:underline font-medium"
              >
                {a.title}
              </a>
              <div className="text-[10px] text-fg-subtle font-mono break-all">{a.url}</div>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex items-center justify-between gap-2">
        <button onClick={onRefine} className="text-fg-subtle hover:text-fg text-sm">
          ← URL / ヒント変更
        </button>
        <div className="flex items-center gap-2">
          <button
            onClick={onVisualPick}
            className="border border-accent/50 text-accent hover:bg-accent-subtle px-3 py-1.5 rounded text-sm font-semibold"
          >
            ビジュアルで選び直す
          </button>
          <button
            onClick={onPick}
            className="bg-accent hover:bg-accent-hover text-white px-4 py-1.5 rounded text-sm font-semibold inline-flex items-center gap-1.5"
          >
            <Check className="h-3.5 w-3.5" />
            これで OK、 登録へ
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------- Step 6: confirm + register ----------

function Step6Confirm({
  candidate,
  name,
  setName,
  displayName,
  setDisplayName,
  folder,
  setFolder,
  enabled,
  setEnabled,
  loading,
  onRegister,
  onBack,
}: {
  candidate: SourceCandidate;
  name: string;
  setName: (v: string) => void;
  displayName: string;
  setDisplayName: (v: string) => void;
  folder: string;
  setFolder: (v: string) => void;
  enabled: boolean;
  setEnabled: (v: boolean) => void;
  loading: boolean;
  onRegister: () => void;
  onBack: () => void;
}) {
  const nameOk = /^[a-z0-9][a-z0-9\-]*$/.test(name);
  const displayOk = displayName.trim().length > 0;
  const [showAdvanced, setShowAdvanced] = useState(false);

  return (
    <div className="space-y-3">
      <div className="bg-surface-2 rounded p-3 space-y-1.5 text-xs">
        <div>
          <span className="text-fg-muted">種別: </span>
          <span className="text-fg font-semibold">
            {vocabLabel("transport", candidate.transport)}
          </span>
        </div>
        <div>
          <span className="text-fg-muted">取得 URL: </span>
          <code className="text-fg-subtle break-all font-mono">{candidate.fetch_url}</code>
        </div>
        <div>
          <span className="text-fg-muted">自動検出名: </span>
          <span className="text-fg">{candidate.source_name}</span>
        </div>
      </div>

      <div className="space-y-2.5">
        <div>
          <label className="block text-xs text-fg-muted mb-1">
            表示名 <span className="text-fg-subtle">(購読一覧に表示される名称)</span>
          </label>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="例: CISA Advisories"
            className={`w-full bg-surface-2 border rounded px-3 py-2 text-sm ${
              !displayOk ? "border-critical" : "border-border-subtle"
            }`}
          />
          {!displayOk && <div className="text-critical text-xs mt-1">表示名は必須です</div>}
        </div>
        {/* 内部 ID (kebab) は自動生成。通常は触れないため詳細設定に折りたたむ。 */}
        <div>
          <button
            type="button"
            onClick={() => setShowAdvanced((v) => !v)}
            className="text-xs text-fg-subtle hover:text-fg inline-flex items-center gap-1"
          >
            <span>{showAdvanced ? "▾" : "▸"}</span>
            <span>
              詳細設定 — 内部 ID:{" "}
              <code className="font-mono text-fg-muted">{name || "(自動生成)"}</code>
            </span>
          </button>
          {showAdvanced && (
            <div className="mt-1.5">
              <label className="block text-xs text-fg-muted mb-1">
                内部 ID (英小文字・数字・ハイフン){" "}
                <span className="text-fg-subtle">(URL から自動生成。通常変更不要)</span>
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase())}
                placeholder="例: cisa-advisories"
                className={`w-full bg-surface-2 border rounded px-3 py-2 text-sm font-mono ${
                  name && !nameOk ? "border-critical" : "border-border-subtle"
                }`}
              />
              {name && !nameOk && (
                <div className="text-critical text-xs mt-1">英小文字・数字・ハイフンのみ</div>
              )}
            </div>
          )}
        </div>

        {/* フォルダは transport に依らず共通 (RSS だけに出していたため sitemap /
            スクレイパーが未分類のまま登録されていた)。候補は実データ由来。 */}
        <div>
          <label className="block text-xs text-fg-muted mb-1">フォルダ (分類)</label>
          <FolderSelect
            value={folder}
            onChange={setFolder}
            className="w-full bg-surface-2 border border-border-subtle rounded px-3 py-2 text-sm text-fg"
          />
        </div>

        <label className="flex items-center gap-2 text-sm text-fg">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span>有効化 (すぐに取得対象に含める)</span>
        </label>
      </div>

      <div className="bg-critical-soft border border-critical/40 rounded px-3 py-2 text-xs text-fg inline-flex items-center gap-1.5">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        <span><strong>確認</strong>: この内容で登録します。</span>
      </div>

      <div className="flex justify-between">
        <button onClick={onBack} className="text-fg-subtle hover:text-fg text-sm">
          ← 候補を選び直す
        </button>
        <button
          onClick={onRegister}
          disabled={!nameOk || !displayOk || loading}
          className="bg-critical hover:bg-critical/80 disabled:bg-surface-2 disabled:text-fg-subtle text-white px-4 py-1.5 rounded text-sm font-semibold inline-flex items-center justify-center gap-1.5"
        >
          {loading ? (
            <ButtonSpinner label="登録中..." />
          ) : (
            <>
              <Check className="h-3.5 w-3.5" />
              登録する
            </>
          )}
        </button>
      </div>
    </div>
  );
}

// ---------- Step 7: done ----------

function Step7Done({
  result,
  onClose,
  onAddAnother,
}: {
  result: {
    name: string;
    commit: string | null;
    target_yaml: string;
    transport: string;
    appended: boolean;
    replaced?: { title: string; removed: boolean; error?: string };
  };
  onClose: () => void;
  onAddAnother: () => void;
}) {
  return (
    <div className="space-y-4">
      <div className="bg-success-soft border border-success/40 rounded p-4 text-center">
        <div className="flex justify-center mb-2"><Check className="h-8 w-8 text-success" /></div>
        <div className="text-md font-bold text-fg">
          <code>{result.name}</code> ({vocabLabel("transport", result.transport)}) を追加しました
        </div>
        {result.appended && (
          <div className="text-xs text-success mt-1 inline-flex items-center gap-1">
            <Check className="h-3.5 w-3.5 shrink-0" />
            スクレイパー監視ジョブに自動追加しました
          </div>
        )}
        {result.replaced?.removed && (
          <div className="text-xs text-success mt-1 inline-flex items-center gap-1">
            <Check className="h-3.5 w-3.5 shrink-0" />
            古い設定「{result.replaced.title}」を削除しました
          </div>
        )}
        {result.replaced && !result.replaced.removed && (
          <div className="text-xs text-warning mt-1">
            ⚠ 古い設定「{result.replaced.title}」を削除できませんでした
            {result.replaced.error ? `: ${result.replaced.error}` : ""}。
            二重取得になるため購読一覧から手動で削除してください
          </div>
        )}
      </div>
      <p className="text-xs text-fg-muted">
        次回の自動取得から取り込み対象になります。
      </p>
      <div className="flex justify-end gap-2">
        <button
          onClick={onAddAnother}
          className="border border-border-default text-fg hover:bg-surface-2 px-4 py-1.5 rounded text-sm"
        >
          別のソースを追加
        </button>
        <button
          onClick={onClose}
          className="bg-accent hover:bg-accent-hover text-white px-4 py-1.5 rounded text-sm font-semibold"
        >
          閉じる
        </button>
      </div>
    </div>
  );
}
