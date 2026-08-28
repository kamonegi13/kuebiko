// 「エンティティ」カードの **共有 SSoT**。記事画面 (ArticleReadView) と
// 事象ニュース (EventNewsDetail) の両方がここを使う (JudgementCard と同じ理由)。
//
// 役割三分割は記事画面の設計をそのまま踏襲する (2026-07-27 B1):
//   主題アクター (記事の主語) / 言及された組織・関係者 / 技術指標
// 言及 (mention) と主題 (subject) を分離しないと、報告機関 (NSA 等) が
// 「脅威アクター」として誤表示される。
//
// 事象は複数記事の集計なので、各値は任意で「何記事が言及したか」を持てる。

import { useState } from "react";
import { vocabLabel } from "../../hooks/useVocab";

export interface EntityValue {
  value: string;
  articles?: number;
}

export interface EntityGroupView {
  type: string;
  values: EntityValue[];
  omitted?: number;
  cvss?: Record<string, { score: number; severity: string }>;
  affected?: Record<string, { vendors: string[]; products: string[] }>;
}

export interface SubjectActorView {
  id: string;
  label: string;
  articles?: number;
}

// 常時表示する entity 種別。CTI の読み手が最初に要る識別子は「誰が (主題アクター)」
// 「何の脆弱性か (CVE)」「何のマルウェアか」。TTP・IOC・製品・国・PIR 等は件数が
// 多く縦に長くなるため **必要なときに開く** (カードが長いと本文へ辿り着けない)。
const PRIMARY_ENTITY_TYPES: readonly string[] = ["cve", "malware_family"];

// entity chip → 逆引き。**事象ニュース** を既定の着地点にする (2026-08-24)。
// 事象ニュースを主導線にしていく方針のため、chip から記事一覧へ落とすと読み手が
// 2 つの画面を行き来することになる。記事単位で見たいときは事象の「原記事」から入る。
export function pivotHref(type: string, value: string): string {
  return `/app/eventnews?${new URLSearchParams({ pivot_type: type, pivot_value: value })}`;
}

// コピー対象: ioc_* (IP/ドメイン/URL/ハッシュ) + CVE。TTP は IoC ではないため対象外。
function isCopyableIocType(type: string): boolean {
  return type.startsWith("ioc_") || type === "cve";
}

function defangValue(type: string, value: string): string {
  if (type === "ioc_domain") return value.replace(/\./g, "[.]");
  if (type === "ioc_ip") return value.replace(/\./g, "[.]");
  if (type === "ioc_url") return value.replace(/^http/i, "hxxp").replace(/\./g, "[.]");
  return value;
}

function buildIocClipboard(groups: EntityGroupView[], defang: boolean): string {
  return groups
    .filter((g) => isCopyableIocType(g.type) && g.values.length > 0)
    .map((g) => {
      const vals = g.values.map((v) => (defang ? defangValue(g.type, v.value) : v.value));
      return `# ${vocabLabel("entity_type", g.type)}\n${vals.join("\n")}`;
    })
    .join("\n\n");
}

const BTN =
  "bg-surface-2 border border-border-default rounded px-2 py-0.5 text-[13px] text-fg-muted hover:text-accent hover:border-accent-soft transition-colors";

function EntityActions({
  groups,
  stixArticleId,
}: {
  groups: EntityGroupView[];
  stixArticleId?: string | null;
}) {
  const [copied, setCopied] = useState<"plain" | "defang" | "error" | null>(null);
  const hasIoc = groups.some((g) => isCopyableIocType(g.type) && g.values.length > 0);
  if (groups.length === 0) return null;

  const copy = async (mode: "plain" | "defang") => {
    try {
      await navigator.clipboard.writeText(buildIocClipboard(groups, mode === "defang"));
      setCopied(mode);
    } catch {
      setCopied("error");
    }
    setTimeout(() => setCopied(null), 1500);
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {copied === "error" && <span className="text-[13px] text-critical">コピー失敗</span>}
      {(copied === "plain" || copied === "defang") && (
        <span className="text-[13px] text-accent">コピーしました</span>
      )}
      {hasIoc && (
        <>
          <button onClick={() => copy("defang")} className={BTN} title="IoC を defang 表記 (hxxp / [.]) で一括コピー">
            IoC コピー (defang)
          </button>
          <button onClick={() => copy("plain")} className={BTN} title="IoC をそのままの表記で一括コピー">
            plain
          </button>
        </>
      )}
      {/* STIX は 1 記事単位の出力。事象 (複数記事) では出さない。 */}
      {stixArticleId && (
        <a
          href={`/api/v1/articles/${encodeURIComponent(stixArticleId)}/stix`}
          className={BTN}
          title="この記事の STIX 2.1 bundle をダウンロード"
        >
          STIX
        </a>
      )}
    </div>
  );
}

function cvssTone(score: number): string {
  if (score >= 9) return "text-critical";
  if (score >= 7) return "text-warning";
  return "text-fg-subtle";
}

export function EntitySection({
  subjectActors,
  subjectActorSource,
  subjectActorRationale,
  groups,
  stixArticleId,
  note,
}: {
  subjectActors: SubjectActorView[];
  /** 主題が空のとき「未評価」と「評価済み・主題なし」を区別するための出所。 */
  subjectActorSource?: string | null;
  subjectActorRationale?: string | null;
  groups: EntityGroupView[];
  stixArticleId?: string | null;
  /** 呼び手固有の注記 (事象では「構成記事の抽出結果を集計」)。 */
  note?: string;
}) {
  const subjectIds = new Set(subjectActors.map((s) => s.id));
  // 言及 actor は主題を除くと空になりうる。空の群は種別ごと出さない。
  const visible = groups.filter(
    (g) =>
      (g.type === "actor" ? g.values.filter((v) => !subjectIds.has(v.value)) : g.values).length > 0,
  );
  const primary = visible.filter((g) => PRIMARY_ENTITY_TYPES.includes(g.type));
  const rest = visible.filter((g) => !PRIMARY_ENTITY_TYPES.includes(g.type));
  const restCount = rest.reduce((n, g) => n + g.values.length, 0);
  const renderGroup = (g: EntityGroupView) => (
    <GroupChips key={g.type} g={g} subjectIds={subjectIds} />
  );

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-fg-muted text-xs uppercase">エンティティ (クリックで逆引き)</div>
        <EntityActions groups={groups} stixArticleId={stixArticleId} />
      </div>
      {note && <div className="text-fg-subtle text-[13px] -mt-1">{note}</div>}

      {/* 主題アクター = 記事の主語 (攻撃実行主体)。未帰属なら明示する。 */}
      <div>
        <div className="text-fg-subtle text-xs mb-1">主題アクター</div>
        {subjectActors.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {subjectActors.map((sa) => (
              <a
                key={sa.id}
                href={pivotHref("actor", sa.id)}
                title={`${sa.label} で逆引き`}
                className="inline-flex items-center gap-1 bg-accent/10 border border-accent-soft rounded px-2 py-0.5 text-xs font-medium text-accent hover:bg-accent/20 transition-colors"
              >
                {sa.label}
                {sa.articles != null && (
                  <span className="tnum text-[12px] text-accent/70">{sa.articles}</span>
                )}
              </a>
            ))}
          </div>
        ) : (
          <div className="text-fg-subtle text-xs">
            {subjectActorSource ? "未帰属（評価済み・特定の攻撃主体なし）" : "未評価"}
          </div>
        )}
        {/* 主題判定の根拠 (2026-08-13): 「正しい未帰属」を取りこぼしと区別できるようにする。
            title 層確定時は出さない (根拠文は LLM 層の判定説明のため齟齬しうる)。 */}
        {subjectActorRationale && subjectActorSource !== "title" && (
          <div className="text-fg-subtle text-xs mt-1 leading-relaxed">
            └ 判定根拠: {subjectActorRationale}
          </div>
        )}
      </div>

      {visible.length === 0 && (
        <div className="text-fg-subtle text-sm">抽出されたエンティティはありません</div>
      )}
      {primary.map(renderGroup)}
      {rest.length > 0 && (
        <details className="group">
          <summary className="text-fg-subtle text-xs cursor-pointer select-none hover:text-accent">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
            <span className="group-open:hidden">
              その他のエンティティを表示 ({rest.length} 種別・{restCount} 件)
            </span>
            <span className="hidden group-open:inline">その他のエンティティを閉じる</span>
          </summary>
          <div className="space-y-3 mt-2">{rest.map(renderGroup)}</div>
        </details>
      )}
    </div>
  );
}


// 1 種別ぶんの chip 群。常時表示ぶんと折りたたみぶんで **同じ描画** を使う。
function GroupChips({ g, subjectIds }: { g: EntityGroupView; subjectIds: Set<string> }) {
  // actor (言及) 群は主題 id を除外し「言及された組織・関係者」として表示。
  // subject は上の主題アクター欄で既出のため二重表示しない。
  const isMentionActor = g.type === "actor";
  const values = isMentionActor ? g.values.filter((v) => !subjectIds.has(v.value)) : g.values;
  if (values.length === 0) return null;
  const groupLabel = isMentionActor ? "言及された組織・関係者" : vocabLabel("entity_type", g.type);
  return (

          <div key={g.type}>
            <div className="text-fg-subtle text-xs mb-1">
              {groupLabel}
              {g.omitted != null && g.omitted > 0 && <span className="ml-1">（他 {g.omitted} 件）</span>}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {values.map((v) => {
                const cvss = g.cvss?.[v.value];
                return (
                  <a
                    key={v.value}
                    href={pivotHref(g.type, v.value)}
                    title={cvss ? `${v.value} — CVSS ${cvss.score} ${cvss.severity}` : `${v.value} で逆引き`}
                    className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs font-mono text-fg-muted hover:text-accent hover:border-accent-soft transition-colors"
                  >
                    {v.value}
                    {cvss && (
                      <span className={`tnum font-semibold ${cvssTone(cvss.score)}`}>
                        {cvss.score.toFixed(1)}
                      </span>
                    )}
                    {v.articles != null && v.articles > 1 && (
                      <span className="tnum text-[12px] text-fg-subtle">{v.articles}</span>
                    )}
                  </a>
                );
              })}
            </div>
            {g.type === "cve" && g.affected && (() => {
              const vendors = Array.from(
                new Set(Object.values(g.affected).flatMap((a) => a.vendors)),
              ).sort();
              if (vendors.length === 0) return null;
              return (
                <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                  <span className="text-fg-subtle text-[13px]">影響ベンダ:</span>
                  {vendors.map((vd) => (
                    <a
                      key={vd}
                      href={`/app/news?affected_vendor=${encodeURIComponent(vd)}`}
                      title={`${vd} の脆弱性に言及する記事を絞り込む`}
                      className="inline-flex items-center bg-warning-soft border border-warning/40 rounded px-1.5 py-0.5 text-[13px] text-warning hover:bg-warning/20 transition-colors"
                    >
                      {vd}
                    </a>
                  ))}
                </div>
              );
            })()}
          </div>
  );
}
