// 国別ニュース ドリルダウン (脅威マップページの RightPanel「country」モードと同じ API・同じ
// 行表示を dashboard widget から再利用するための共有部品)。
//
// MapPage.tsx は自前の入れ子パネル状態 (idle/country/article) を持つため、そちらは今回
// 手を入れない。dashboard 側 (mini_map / geo_ranking widget) は選択国の記事一覧だけを
// 表示すれば十分なので、この部品は「選択中の 1 国のニュースを表示する」最小形に絞る。
// 記事クリックはグローバルな記事サイドピーク (ArticlePeek.tsx のクリックインターセプト) に
// 委ねる — 通常の <a href="/app/article/...">リンクにするだけで良い。
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import {
  fetchCountryArticles,
  type CountryArticle,
  type GeoDomain,
  type MinImportance,
  type PmesiiAxis,
  type SourceStatus,
  type ThreatClass,
} from "../../api/geo";
import { sectorColor } from "./sectorColors";
import { intentHex, intentLabel } from "../../utils/diamond";
import { formatJst } from "../../utils/date";
import { vocabLabel } from "../../hooks/useVocab";

function ArticleRow({ a }: { a: CountryArticle }) {
  // MapPage.tsx の ArticleRow と同じ行表示 (セクターの縦バー + 台帳タグ + 動機チップ)。
  // onClick での入れ子パネル遷移ではなく通常リンクにし、グローバルなサイドピークに任せる。
  return (
    <li className="py-1.5" title={a.victim_sector_label || a.victim_sector || "未分類"}>
      <a
        href={`/app/article/${encodeURIComponent(a.article_id)}`}
        className="flex w-full items-stretch gap-2.5 text-left"
      >
        <span
          className="w-[3px] shrink-0 self-stretch rounded-full"
          style={{ background: sectorColor(a.victim_sector) }}
        />
        <span className="min-w-0 flex-1">
          <span className="block text-sm text-fg hover:text-accent line-clamp-2">
            {a.title || a.url || a.article_id}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-fg-subtle">
            {a.status === "collected" && (
              <span
                className="rounded-sm border border-border-subtle px-1 text-[12px] text-fg-subtle"
                title="台帳: ransomware.live 等の未加工リスト由来 (未投稿・地図専用)"
              >
                台帳
              </span>
            )}
            {a.socio_political_intent && a.socio_political_intent !== "unknown" && (
              <span
                className="inline-flex items-center gap-1 rounded-sm px-1 text-[12px]"
                style={{
                  color: intentHex(a.socio_political_intent),
                  border: `1px solid ${intentHex(a.socio_political_intent)}55`,
                }}
                title="動機（統一した意図の分類）"
              >
                {intentLabel(a.socio_political_intent)}
              </span>
            )}
            {a.category && <span>{vocabLabel("category", a.category)}</span>}
            {(a.victim_sector_label || a.victim_sector) && (
              <span>· {a.victim_sector_label || a.victim_sector}</span>
            )}
            {a.created_at && <span>· {formatJst(a.created_at)}</span>}
          </span>
        </span>
      </a>
    </li>
  );
}

export function CountryNewsList({
  articles,
  loading,
}: {
  articles: CountryArticle[] | undefined;
  loading: boolean;
}) {
  if (loading) return <p className="px-1 py-2 text-xs text-fg-subtle">読み込み中…</p>;
  if (!articles || articles.length === 0)
    return <p className="px-1 py-2 text-xs text-fg-subtle">該当する記事がありません。</p>;
  return (
    <ul className="divide-y divide-border-subtle">
      {articles.map((a) => (
        <ArticleRow key={a.article_id} a={a} />
      ))}
    </ul>
  );
}

export interface CountryNewsPanelProps {
  iso: string;
  domain: GeoDomain;
  days: number;
  threatClass: ThreatClass;
  sourceStatus: SourceStatus;
  minImportance: MinImportance;
  pmesii: PmesiiAxis;
  onClose: () => void;
  // data 到着前・取得失敗時のラベル fallback (地図/ランキングが既に持つ label を渡す)。
  fallbackLabel?: string;
}

// ヘッダ (国名 + 件数 + 閉じる) + 記事一覧。脅威マップの RightPanel 「country」モードの
// 最小形 (戻る導線は無く、閉じるのみ — dashboard widget は入れ子パネルを持たないため)。
export function CountryNewsPanel({
  iso,
  domain,
  days,
  threatClass,
  sourceStatus,
  minImportance,
  pmesii,
  onClose,
  fallbackLabel,
}: CountryNewsPanelProps) {
  const { data, isFetching } = useQuery({
    queryKey: ["geo-country-widget", iso, days, threatClass, domain, sourceStatus, minImportance, pmesii],
    queryFn: () => fetchCountryArticles(iso, days, threatClass, domain, sourceStatus, minImportance, pmesii),
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-border-subtle pb-1.5">
        <button onClick={onClose} className="text-fg-subtle hover:text-fg" title="閉じる (選択を解除)">
          <X className="h-3.5 w-3.5" />
        </button>
        <span className="min-w-0 flex-1 truncate text-sm font-bold text-fg">{data?.label ?? fallbackLabel ?? iso}</span>
        <span className="shrink-0 text-xs text-fg-subtle">{data?.count ?? 0} 件</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <CountryNewsList articles={data?.articles} loading={isFetching} />
      </div>
    </div>
  );
}
