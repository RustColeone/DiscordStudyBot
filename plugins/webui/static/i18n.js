"use strict";
// The pages in another language: the interface's own words (never messages, names or what you type), translated as
// they are drawn. `i18n-zh.js` holds the dictionary (tools/translate_ui.py asks DeepSeek, in the style of an anime
// gacha game's interface); an entry is the English text, whole, or a pattern with {0}, {1} where numbers and names go
// ("{0} classmates"). The language is the one picked on your page (prefs.js), or the browser's (Chinese when it
// prefers Chinese). Pages call `t(text, values)` for words they build themselves. (The Classroom serves the same
// file: keep the two alike.)
(() => {
  const DICTIONARIES = { zh: window.I18N_ZH || {} };
  const LANGUAGES = { en: "English", zh: "中文" };
  const saved = window.prefs ? window.prefs.get("lang") : null;
  const preferred = (navigator.languages || [navigator.language || "en"]).some((item) => /^zh\b/i.test(item)) ? "zh" : "en";
  const lang = saved in LANGUAGES ? saved : preferred;
  const words = DICTIONARIES[lang] || {};
  // Patterns: "{0} of {1}" → /^(.+?) of (.+?)$/, the captured parts put back where the translation has them. A hole
  // right after a letter is a plural ending ("{0} classmate{1}": "s" or nothing, three letters at most), which Chinese
  // leaves out.
  const holes = (key) => {
    try {
      return new RegExp(`^${key.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/(^|[A-Za-z])?\{(\d+)\}/g,
        (all, before, n) => before ? `${before}(?<p${n}>[A-Za-z]{0,3})` : `(?<p${n}>[\\s\\S]+?)`)}$`);
    } catch { return null; }  // (one hole twice: left out)
  };
  // ("{0} and {1}" would match any sentence with an "and" in it: a hole at both ends needs two words between.)
  const loose = (key) => /^\{\d+\}/.test(key) && /[^A-Za-z]\{\d+\}$/.test(key) && !/[A-Za-z]+\W+[A-Za-z]+/.test(key);
  const patterns = Object.keys(words).filter((key) => /\{\d+\}/.test(key) && !loose(key) && holes(key)).map((key) => ({
    test: holes(key),
    endings: new Set([...key.matchAll(/[A-Za-z]\{(\d+)\}/g)].map((match) => match[1])),
    to: words[key],
    fixed: key.replace(/\{\d+\}/g, "").length,
  })).sort((a, b) => b.fixed - a.fixed);  // the most particular first: "Stage {0} at Lv {1}" before "Stage {0}"
  const fill = (template, values) => String(template).replace(/\{(\w+)\}/g, (all, key) => (values && key in values ? values[key] : all));
  const latin = (text) => (String(text).match(/[A-Za-z]/g) || []).length;
  function translate(text) {
    if (lang === "en" || !text) return text;
    const trimmed = text.trim();
    // (Mostly Chinese: translated already, by `t()` or before, its English words names and code: call_owner. A
    // Chinese name in English words, "鲸酱 is working", is still translated.)
    if (!trimmed || !/[A-Za-z]/.test(trimmed) || (trimmed.match(/[\u3400-\u9fff]/g) || []).length > latin(trimmed)) return text;
    const found = lookup(trimmed);
    return found == null ? text : text.replace(trimmed, found);
  }
  function lookup(text) {
    if (words[text] != null) return words[text];
    let best = null;
    for (const pattern of patterns) {
      const match = pattern.test.exec(text);
      if (!match) continue;
      const parts = {};
      for (const [name, value] of Object.entries(match.groups || {})) {
        parts[name.slice(1)] = pattern.endings.has(name.slice(1)) ? "" : translate(value);
      }
      best = fill(pattern.to, parts);
      break;
    }
    if (text.includes(" · ")) {  // pieces joined with " · ": each on its own, when that leaves less English
      const pieces = text.split(" · ").map(translate).join(" · ");
      if (pieces !== text && (best == null || latin(pieces) < latin(best))) best = pieces;
    }
    return best;
  }
  // `t("Bond with {name} rose", {name})`: a page's own words, with values put in after translating.
  window.t = (text, values) => fill(translate(String(text)), values);
  window.uiLanguage = lang;
  window.uiLanguages = LANGUAGES;
  if (lang === "en") return;
  document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;

  // Never translated: messages, names, what people type (a field's placeholder is), code.
  const SKIP = ".markdown, .bubble, .chat-last, .chat-name, .chat-title, .card-name, .plate-name, .seat-name, .pick-plate," +
               " .voice-bubble, .task-text, .file-box, .folder, pre, code, option, [data-no-i18n], .chat-real, .member-name," +
               " .chibi-name, .session-title, .report-title, .log-title, .strip-title, .task-title, .student-description";
  const FIELDS = "input, textarea, select";
  const ATTRIBUTES = ["placeholder", "aria-label", "title", "alt"];
  const done = new WeakSet();
  function walk(root) {
    if (root.nodeType === Node.TEXT_NODE) return text(root);
    if (root.nodeType !== Node.ELEMENT_NODE || root.closest(SKIP) || root.matches("script, style")) return;
    for (const name of ATTRIBUTES) {
      const value = root.getAttribute(name);
      if (value) {
        const translated = translate(value);
        if (translated !== value) root.setAttribute(name, translated);
      }
    }
    if (!root.matches(FIELDS)) for (const child of root.childNodes) walk(child);
  }
  function text(node) {
    if (done.has(node) || node.parentElement?.closest(`${SKIP}, ${FIELDS}`)) return;
    const translated = translate(node.nodeValue);
    done.add(node);
    if (translated !== node.nodeValue) node.nodeValue = translated;
  }
  function start() {
    walk(document.body);
    document.title = translate(document.title);
    new MutationObserver((changes) => {
      for (const change of changes) {
        if (change.type === "characterData") { done.delete(change.target); text(change.target); }
        else if (change.type === "attributes") walk(change.target);
        else for (const node of change.addedNodes) walk(node);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true,
                                attributeFilter: ATTRIBUTES });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
