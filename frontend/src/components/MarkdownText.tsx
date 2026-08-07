// 共通 Markdown renderer。synthesis 系の各 section / context bar で再利用。
// markdown-to-jsx を Tailwind スタイル mapping 付きで wrap。

import Markdown from "markdown-to-jsx";

const MARKDOWN_OPTS = {
  // security-review M5: LLM synthesis 由来の文字列を描画する。生 HTML タグは解釈せず
  // テキストとして扱い、feed→LLM 経由の HTML 注入を構造的に排除する (prose のみ想定)。
  disableParsingRawHTML: true,
  overrides: {
    p: { props: { className: "leading-relaxed text-sm text-fg mb-2 last:mb-0" } },
    ul: { props: { className: "list-disc pl-5 space-y-1 my-1.5 text-sm text-fg" } },
    ol: { props: { className: "list-decimal pl-5 space-y-1 my-1.5 text-sm text-fg" } },
    li: { props: { className: "leading-relaxed" } },
    strong: { props: { className: "font-semibold text-fg" } },
    em: { props: { className: "italic text-fg-muted" } },
    code: { props: { className: "bg-surface-2 text-accent-hover px-1 py-0.5 rounded text-[12px] font-mono" } },
    a: { props: { className: "text-accent hover:underline", target: "_blank", rel: "noopener noreferrer" } },
    h1: { props: { className: "text-md font-bold text-fg mt-2 mb-1.5" } },
    h2: { props: { className: "text-sm font-bold text-fg mt-2 mb-1.5" } },
    h3: { props: { className: "text-sm font-semibold text-fg mt-1.5 mb-1" } },
    blockquote: { props: { className: "border-l-2 border-border-subtle pl-3 italic text-fg-muted my-2" } },
  },
};

interface MarkdownTextProps {
  children: string | null | undefined;
  className?: string;
}

export function MarkdownText({ children, className }: MarkdownTextProps) {
  if (!children) return null;
  const content = <Markdown options={MARKDOWN_OPTS}>{children}</Markdown>;
  if (!className) return content;
  return <div className={className}>{content}</div>;
}
