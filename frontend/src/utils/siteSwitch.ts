// 「標準 (公開サイト)」⇄「アドバンスド (写し)」の切替リンク組み立て。
//
// 公開サイトの記事は事象ニュース (EventItemRecord) 単位の id、写しの
// `/app/article/{id}` は構成記事 (生記事) 単位の別 id 空間で、同一実体を指さない。
// 写し自体にも事象ニュース詳細への URL ルートが無い (2026-10-04 時点、EventNewsPage は
// openId という画面内 state のみで開く — path に id を持たない)。そのため現時点では
// **詳細同士の対応付けができず**、常に相手サイトのトップへ落とす。公開サイトは
// 事象ニュース中心 (CLAUDE.md §13) なので、写し側のトップはダッシュボードでなく
// `/app/eventnews` を選ぶ。

/** 写し (アドバンスド) の事象ニュース一覧 — 公開サイトの等価トップ。 */
export const MIRROR_EVENTNEWS_PATH = "/app/eventnews";

/** 公開サイト (標準) の固定配信パス (Cloudflare Pages 配信物は常にこのパス)。 */
export const PUBLIC_NEWS_PATH = "/news";

function trimOrigin(origin: string): string {
  return origin.trim().replace(/\/$/, "");
}

/** 公開サイトのヘッダから出す「アドバンスド」への href。
 *  mirrorOrigin が未設定なら導線を出さない (null)。 */
export function mirrorSwitchHref(mirrorOrigin: string): string | null {
  const origin = trimOrigin(mirrorOrigin);
  if (!origin) return null;
  return `${origin}${MIRROR_EVENTNEWS_PATH}`;
}

/** 写しのヘッダから出す「標準」への href。
 *  publicOrigin が未設定なら導線を出さない (null)。 */
export function publicSwitchHref(publicOrigin: string): string | null {
  const origin = trimOrigin(publicOrigin);
  if (!origin) return null;
  return `${origin}${PUBLIC_NEWS_PATH}`;
}
