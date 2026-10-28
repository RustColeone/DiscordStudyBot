"use strict";
// Your own settings for this browser, from your page (/me): `theme` (system, light, dark), `lang` (auto, en, zh) and
// `voice` (on or off) with its `rate`. They are kept in a cookie, which every port of this host shares, so the web UI
// and the Classroom next door read the same; localStorage keeps a copy for a browser that refuses cookies. Loaded
// first and not deferred: the theme is set before the page is drawn. (The Classroom serves the same file: keep the
// two alike.)
(() => {
  const NAME = "ui_prefs";
  const DEFAULTS = { theme: "system", lang: "auto", voice: false, rate: 1 };
  const read = () => {
    try {
      const found = document.cookie.split("; ").find((item) => item.startsWith(`${NAME}=`));
      if (found) return JSON.parse(decodeURIComponent(found.slice(NAME.length + 1)));
    } catch { /* a cookie from elsewhere */ }
    try {
      const kept = JSON.parse(localStorage.getItem(NAME) || "null");
      if (kept) return kept;
      const lang = localStorage.getItem("ui.lang");  // (the header's language switch, before this page)
      if (lang) return { lang };
    } catch { /* private window */ }
    return {};
  };
  let values = { ...DEFAULTS, ...read() };
  const apply = () => {
    const root = document.documentElement;
    if (values.theme === "light" || values.theme === "dark") root.dataset.theme = values.theme;
    else delete root.dataset.theme;
  };
  window.prefs = {
    get: (key) => values[key],
    all: () => ({ ...values }),
    set(key, value) {
      values = { ...values, [key]: value };
      const text = encodeURIComponent(JSON.stringify(values));
      document.cookie = `${NAME}=${text}; path=/; max-age=31536000; SameSite=Lax`;
      try { localStorage.setItem(NAME, JSON.stringify(values)); } catch { /* private window */ }
      apply();
    },
  };
  apply();
})();
