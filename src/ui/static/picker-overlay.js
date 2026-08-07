/**
 * Visual selector picker overlay — proxy 配信ページ (iframe) 内で動作。
 *
 * - hover で要素をハイライト
 * - クリックした記事リンクから「繰り返し構造」を汎化した CSS selector を導出
 * - 親 (React VisualSelectorPicker) と postMessage で通信
 *
 * このファイルは src/ui/api/_source_proxy.py が proxied HTML にインライン注入する。
 * 元サイトの <script> は除去済みで、これが唯一実行される JS。
 */
(function () {
  "use strict";

  var HIGHLIGHT_ID = "__cti_picker_highlight__";
  var lastHover = null;

  // ---- スタイル注入 (ハイライト枠) ---------------------------------------
  var style = document.createElement("style");
  style.textContent =
    "." + HIGHLIGHT_ID + "{outline:2px solid #2563eb !important;" +
    "outline-offset:1px !important;background:rgba(37,99,235,0.08) !important;" +
    "cursor:pointer !important;}";
  document.head.appendChild(style);

  // ---- class が安定しているか (動的ハッシュ class を除外) -----------------
  function isStableClass(c) {
    if (!c) return false;
    if (c.indexOf(HIGHLIGHT_ID) !== -1) return false;
    // css-1a2b3c / hash 風 / 数字主体の class は不安定とみなす
    if (/^(css|sc|jsx)-/.test(c)) return false;
    if (/^[a-z]*[0-9a-f]{6,}$/i.test(c)) return false;
    return true;
  }

  function classSelector(el) {
    var tag = el.tagName.toLowerCase();
    if (!el.classList || el.classList.length === 0) return tag;
    var classes = [];
    for (var i = 0; i < el.classList.length; i++) {
      if (isStableClass(el.classList[i])) classes.push(el.classList[i]);
    }
    return classes.length ? tag + "." + classes.join(".") : tag;
  }

  // ---- クリック地点から最も近い記事 <a> を得る ----------------------------
  function nearestAnchor(el) {
    var cur = el;
    while (cur && cur !== document.body) {
      if (cur.tagName === "A" && cur.getAttribute("href")) return cur;
      cur = cur.parentElement;
    }
    // 子孫に <a> があれば最初のもの
    if (el.querySelector) {
      var inner = el.querySelector("a[href]");
      if (inner) return inner;
    }
    return null;
  }

  // セマンティックな (記事リストらしい) class 名
  var SEMANTIC = /title|headline|field--name|post|entry|teaser|card|story|article|views-row|item/i;

  // selector を評価し count / distinct href / target 包含を返す
  function evalSelector(sel, targetHref) {
    var els;
    try {
      els = document.querySelectorAll(sel);
    } catch (e) {
      return null;
    }
    var hrefs = {};
    var hasTarget = false;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var a = el.tagName === "A" ? el : el.querySelector && el.querySelector("a[href]");
      if (a && a.getAttribute("href")) {
        hrefs[a.href] = 1;
        if (a.href === targetHref) hasTarget = true;
      }
    }
    return { count: els.length, distinct: Object.keys(hrefs).length, hasTarget: hasTarget };
  }

  // ---- selector 導出 ------------------------------------------------------
  // クリックした anchor に「最も近い」階層で、複数記事に汎化しつつ最も具体的な
  // 単一 class selector を選ぶ。広すぎる selector ('a' で全リンク等) は除外。
  // 旧版は distinct 最大を選び 'a' (ページ全リンク) が常勝するバグがあった。
  function deriveSelector(anchor) {
    var targetHref = anchor.href;
    var totalAnchors = document.querySelectorAll("a[href]").length;
    var maxReasonable = Math.min(60, Math.floor(totalAnchors * 0.6));
    var cur = anchor;

    for (var depth = 0; depth < 9 && cur && cur !== document.body; depth++) {
      var classes = [];
      if (cur.classList) {
        for (var i = 0; i < cur.classList.length; i++) {
          if (isStableClass(cur.classList[i])) classes.push(cur.classList[i]);
        }
      }
      var cands = [];
      for (var k = 0; k < classes.length; k++) {
        var c = classes[k];
        var sel = cur.tagName === "A" ? "a." + c : "." + c + " a";
        var r = evalSelector(sel, targetHref);
        if (!r || !r.hasTarget) continue;
        if (r.distinct < 2) continue; // 記事が複数取れない
        if (r.count > maxReasonable) continue; // 広すぎ (nav/footer 全体)
        cands.push({
          selector: sel,
          distinct: r.distinct,
          count: r.count,
          semantic: SEMANTIC.test(c),
        });
      }
      if (cands.length) {
        // anchor に最も近い階層で確定。semantic 優先 → 具体的 (distinct 小) 優先
        cands.sort(function (a, b) {
          if (a.semantic !== b.semantic) return a.semantic ? -1 : 1;
          return a.distinct - b.distinct;
        });
        return cands[0];
      }
      cur = cur.parentElement;
    }

    // フォールバック: anchor 自身の class selector
    var fb = classSelector(anchor);
    var fbR = evalSelector(fb, targetHref);
    return { selector: fb, count: fbR ? fbR.count : 0, distinct: fbR ? fbR.distinct : 0 };
  }

  // ---- preview サンプル抽出 (親に渡す確認用) ------------------------------
  function sampleArticles(selector, limit) {
    var out = [];
    var seen = {};
    var els;
    try { els = document.querySelectorAll(selector); } catch (e) { return out; }
    for (var i = 0; i < els.length && out.length < limit; i++) {
      var el = els[i];
      var a = el.tagName === "A" ? el : el.querySelector && el.querySelector("a[href]");
      if (!a) continue;
      var href = a.href;
      if (!href || seen[href]) continue;
      seen[href] = 1;
      var title = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120);
      out.push({ title: title || "(no title)", url: href });
    }
    return out;
  }

  function postToParent(payload) {
    try {
      // この iframe は opaque origin (sandbox allow-scripts) のため targetOrigin は "*"。
      // 受信側 (親) は e.source がこの iframe であることで認証する (security-review M4)。
      window.parent.postMessage(
        Object.assign({ source: "cti-picker" }, payload),
        "*"
      );
    } catch (e) {}
  }

  // ---- イベント -----------------------------------------------------------
  document.addEventListener(
    "mouseover",
    function (e) {
      if (lastHover) lastHover.classList.remove(HIGHLIGHT_ID);
      lastHover = e.target;
      if (lastHover && lastHover.classList) lastHover.classList.add(HIGHLIGHT_ID);
    },
    true
  );

  document.addEventListener(
    "click",
    function (e) {
      e.preventDefault();
      e.stopPropagation();
      var anchor = nearestAnchor(e.target);
      if (!anchor) {
        postToParent({ type: "no_anchor" });
        return;
      }
      var result = deriveSelector(anchor);
      postToParent({
        type: "selected",
        selector: result.selector,
        titleSelector: "",
        matchCount: result.distinct,
        samples: sampleArticles(result.selector, 5),
      });
    },
    true
  );

  // 親からの初期 selector ハイライト要求などを受ける (任意)
  window.addEventListener("message", function (e) {
    // opaque origin のため origin 照合は使えない。送信元が親 window であることで認証する。
    if (e.source !== window.parent) return;
    var data = e.data || {};
    if (data.type === "highlight" && data.selector) {
      try {
        var els = document.querySelectorAll(data.selector);
        for (var i = 0; i < els.length; i++) els[i].classList.add(HIGHLIGHT_ID);
      } catch (err) {}
    }
  });

  postToParent({ type: "ready" });
})();
