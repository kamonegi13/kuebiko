// 「標準 (公開サイト)」⇄「アドバンスド (旧写し)」の切替リンク組み立て。
//
// 2026-10-05: アドバンスドは公開サイトと同じドメインの `/app/` 配下に統合した
// (旧: 別ドメインに匿名公開していた「写し」)。**同一オリジン**になったので、
// リンクは常に相対パスで組み、未設定で無効表示にする分岐も無くなった。
//
// 公開サイトの記事は事象ニュース (EventItemRecord) 単位の id、アドバンスドの
// `/app/article/{id}` は構成記事 (生記事) 単位の別 id 空間で、同一実体を指さない。
// アドバンスド自体にも事象ニュース詳細への URL ルートが無い (2026-10-04 時点、
// EventNewsPage は openId という画面内 state のみで開く — path に id を持たない)。
// そのため現時点では **詳細同士の対応付けができず**、常に相手サイトのトップへ落とす。
// 拡張側の行き先はダッシュボード (`/app`) — 2026-10-05 利用者指示 (以前は事象ニュース)。

/** 拡張 (旧アドバンスド) のトップ = ダッシュボード。 */
export const MIRROR_HOME_PATH = "/app";

/** 公開サイト (標準) の固定配信パス。 */
export const PUBLIC_NEWS_PATH = "/news";

/** 公開サイトのヘッダから出す「アドバンスド」への href (常に相対パス)。 */
export function mirrorSwitchHref(): string {
  return MIRROR_HOME_PATH;
}

/** アドバンスドのヘッダから出す「標準」への href (常に相対パス)。 */
export function publicSwitchHref(): string {
  return PUBLIC_NEWS_PATH;
}
