"use strict";
// The places bar: every place's views, from what each interface publishes in the core's directory (`views` in its
// status: name, icon, a path relative to its link, and an `order`), sorted by order, so each page links to the
// others without knowing them. `places` is {here, others}; `current` names this page's view. It sits in the page's
// header between the project's name and the stamina (then your tab, `renderYou`); on a phone it is the bar along the
// bottom. (The Classroom serves
// the same file: keep the two alike.)
(() => {
  // Drawn icons for the views this project knows; any other view shows the emoji its place published.
  const DRAWN = {
    Cafe: '<path d="M4 9h13v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V9z"/><path d="M17 11h1.5a2.5 2.5 0 0 1 0 5H17"/><path d="M8 3v3M12 3v3"/>',
    MomoChat: '<path d="M4 5h16v11H9l-5 4V5z"/>',
    Classroom: '<path d="M5 4h11l3 3v13H5z"/><path d="M9 10h6M9 14h6M9 18h3"/>',
    Students: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><circle cx="17" cy="9" r="2.5"/>' +
              '<path d="M15.5 14.2A5 5 0 0 1 21.5 19"/>',
    Logbook: '<path d="M3 5c3-1 6-1 9 1 3-2 6-2 9-1v14c-3-1-6-1-9 1-3-2-6-2-9-1V5z"/><path d="M12 6v14"/>',
  };
  function icon(name, fallback) {
    const holder = document.createElement("span");
    holder.className = "places-icon";
    holder.setAttribute("aria-hidden", "true");
    if (DRAWN[name]) {  // (a constant of this file, never anything a place sent)
      holder.innerHTML = `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" ` +
        `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${DRAWN[name]}</svg>`;
    } else {
      holder.textContent = fallback;
    }
    return holder;
  }

  // ---- stamina: each student with her own account, in the header -------------------------------------------------
  const CURRENCY = { USD: "$", CNY: "¥", EUR: "€" };
  const amount = (value, currency = "USD") => value == null ? "–"
    : `${CURRENCY[currency] ?? `${currency} `}${value > 0 && value < 0.1 ? value.toFixed(3) : value.toFixed(2)}`;
  // Her account (how full: a budget against its total, a reported balance against the fullest it was seen) and today.
  window.staminaOf = (stamina) => {
    const account = stamina?.account || null, today = stamina?.today || null;
    const full = account ? (account.kind === "budget" ? account.total : account.peak || account.left) : null;
    return {
      account: account && account.left != null && full ? Math.max(0, Math.min(1, account.left / full)) : null,
      accountText: !account ? "" : account.kind === "budget"
        ? `${amount(account.left)} of a ${amount(account.total)} budget` : account.left == null ? "balance not read yet"
        : `${amount(account.left, account.currency)} in her account`,
      today: today && today.allowance ? Math.max(0, Math.min(1, today.left / today.allowance)) : null,
      todayText: today && today.allowance ? `${amount(today.left)} of ${amount(today.allowance)}` : "",
      amount,
    };
  };
  // The stamina pill in the header: everyone's spending today against the day's total, which you set. `total` is
  // `port.stamina()` (`{today: {spent, allowance, left}}`); `set(dollars)`, when given, makes it editable (a click
  // opens a field), else `href` is where a click goes.
  window.renderStaminaTotal = (holder, total, { set = null, href = "" } = {}) => {
    if (!holder) return;
    const today = total?.today;
    holder.hidden = !today;
    if (!today) { holder.replaceChildren(); fitHeader(); return; }
    const fraction = today.allowance ? Math.max(0, Math.min(1, today.left / today.allowance)) : null;
    const text = today.allowance ? `${amount(today.spent)} / ${amount(today.allowance)}` : `${amount(today.spent)}`;
    const opener = document.createElement(set ? "button" : "a");
    opener.className = "stamina-open";
    if (set) opener.type = "button"; else opener.href = href || "#";
    const said = today.allowance ? `Stamina: ${amount(today.spent)} of today's ${amount(today.allowance)} spent, everyone together`
                                 : `Stamina: ${amount(today.spent)} spent today (no daily total set)`;
    opener.setAttribute("aria-label", said);
    opener.title = said;
    // The bolt, in a ring of what is left today: the ring is all the pill shows when the header is short of room
    // (`fitHeader`).
    const bolt = document.createElement("span");
    bolt.className = "stamina-bolt";
    bolt.setAttribute("aria-hidden", "true");
    const left = Math.round((fraction ?? 0) * 100);
    bolt.innerHTML = `<svg class="stamina-ring${fraction == null ? " is-unknown" : ""}" viewBox="0 0 36 36">` +
                     '<circle cx="18" cy="18" r="15.5" pathLength="100"/>' +
                     (left > 0 ? `<circle class="stamina-ring-fill" cx="18" cy="18" r="15.5" pathLength="100" ` +
                                 `stroke-dasharray="${left} 100"/>` : "") + "</svg>" +
                     '<svg width="20" height="20" viewBox="0 0 24 24" fill="#f0a04b" stroke="#a8601c" stroke-width="1.5" ' +
                     'stroke-linejoin="round"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/></svg>';
    const bars = document.createElement("span");
    bars.className = "stamina-total";
    const label = document.createElement("span");
    label.className = "stamina-label";
    label.textContent = text;
    const track = document.createElement("span");
    track.className = `stamina-track${fraction == null ? " is-unknown" : ""}`;
    const fill = document.createElement("span");
    fill.className = "stamina-fill";
    fill.style.width = `${Math.round((fraction ?? 0) * 100)}%`;
    track.append(fill);
    bars.append(label, track);
    const note = document.createElement("span");
    note.className = "stamina-note";
    note.innerHTML = window.t ? window.t("stamina<br>today") : "stamina<br>today";
    opener.append(bolt, bars, note);
    holder.replaceChildren(opener);
    fitHeader();
    if (!set) return;
    opener.onclick = () => {
      if (holder.querySelector(".stamina-edit")) { holder.querySelector(".stamina-edit").remove(); return; }
      const form = document.createElement("form");
      form.className = "stamina-edit";
      const field = document.createElement("input");
      field.type = "number";
      field.min = "0";
      field.step = "0.10";
      field.value = today.allowance != null ? String(today.allowance) : "2.00";
      field.setAttribute("aria-label", "Today's total for everyone, in dollars");
      const save = document.createElement("button");
      save.type = "submit";
      save.textContent = window.t ? window.t("Set") : "Set";
      const hint = document.createElement("span");
      hint.className = "stamina-hint";
      hint.textContent = window.t ? window.t("Everyone's daily total ($); 0 for none") : "Everyone's daily total ($); 0 for none";
      form.append(hint, field, save);
      form.onsubmit = (event) => { event.preventDefault(); set(Math.max(0, parseFloat(field.value) || 0)); form.remove(); };
      holder.append(form);
      field.focus();
    };
  };

  // ---- room in the header: when the places would run under the stamina (a narrow window, a long name, more places),
  // the pill folds to its ring, then your tab to your initial. (A phone has the places along the bottom.) ------------
  const fitHeader = () => {
    const header = document.querySelector(".site-header");
    const nav = header?.querySelector(".places-nav"), end = header?.querySelector(".site-end");
    if (!nav || !end) return;
    header.classList.remove("is-tight", "is-tighter");
    if (nav.hidden || getComputedStyle(nav).position === "fixed") return;
    const clear = () => nav.getBoundingClientRect().right + 8 <= end.getBoundingClientRect().left;
    for (const step of ["is-tight", "is-tighter"]) {
      if (clear()) return;
      header.classList.add(step);
    }
  };
  // Fitted as soon as the places or the stamina are drawn, and again when the window, a font or a name changes their
  // size (a frame later: the fit itself resizes the end).
  let fitting = 0;
  const refit = () => { if (!fitting) fitting = requestAnimationFrame(() => { fitting = 0; fitHeader(); }); };
  const header = document.querySelector(".site-header");
  if (header && window.ResizeObserver) {
    const watch = new ResizeObserver(refit);
    for (const part of [header, header.querySelector(".places-nav"), header.querySelector(".site-end")]) {
      if (part) watch.observe(part);
    }
  }

  // ---- you: the tab right of the stamina, to your own page ----------------------------------------------------------
  // The web UI publishes it with its place (`you: {path, name}`, the path relative to its link); any page with the
  // directory's places finds it there. `current` is true on your page itself.
  window.renderYou = (holder, places, { current = false } = {}) => {
    if (!holder) return;
    let you = null, href = "";
    if (places?.here?.you) { you = places.here.you; href = String(you.path || "me"); }
    else {
      for (const place of places?.others || []) {
        if (!place?.you) continue;
        try {
          const url = new URL(String(place.you.path || "me"), place.link || "");
          if (/^https?:$/.test(url.protocol)) { you = place.you; href = url.href; break; }
        } catch { /* a place without a link */ }
      }
    }
    holder.hidden = !you;
    if (!you) return;
    const name = String(you.name || "You");
    holder.href = href;
    holder.classList.toggle("is-current", current);
    if (current) holder.setAttribute("aria-current", "page"); else holder.removeAttribute("aria-current");
    holder.title = window.t ? window.t("Your page: settings, and what the students call you") :
      "Your page: settings, and what the students call you";
    const face = document.createElement("span");
    face.className = "user-face";
    face.setAttribute("aria-hidden", "true");
    face.textContent = [...name.trim()][0]?.toUpperCase() || "?";
    const label = document.createElement("span");
    label.className = "user-name";
    label.setAttribute("data-no-i18n", "");
    label.textContent = name;
    holder.replaceChildren(face, label);
  };

  window.renderPlaces = (nav, places, current) => {
    if (!nav) return;
    const entries = [];
    const add = (place, here) => {
      for (const view of (place && Array.isArray(place.views) ? place.views : [])) {
        if (!view || typeof view.name !== "string" || !view.name) continue;
        const path = String(view.path || "");
        let href = "";
        if (here) {
          if (path.includes(":")) continue;  // this page's own views are relative paths
          href = path || "./";
        } else {
          try {
            const url = new URL(path, place.link || "");
            if (!/^https?:$/.test(url.protocol)) continue;
            href = url.href;
          } catch { continue; }  // a place without a link has nowhere to go
        }
        entries.push({ name: view.name, icon: String(view.icon || "📍"), href, order: Number(view.order) || 100,
                       off: view.off === true, note: String(view.note || "") });
      }
    };
    add(places?.here, true);
    for (const place of places?.others || []) add(place, false);
    entries.sort((a, b) => a.order - b.order);
    nav.replaceChildren(...entries.map((entry) => {
      const link = document.createElement("a");
      link.className = `places-tab${entry.name === current ? " is-current" : ""}${entry.off ? " is-off" : ""}`;
      link.href = entry.href;
      if (entry.off) link.title = `${entry.name}: ${entry.note || "not available"}`;  // greyed: nobody is there
      if (entry.name === current) link.setAttribute("aria-current", "page");
      const label = document.createElement("span");
      label.className = "places-label";
      label.textContent = entry.name;
      link.append(icon(entry.name, entry.icon), label);
      return link;
    }));
    nav.hidden = entries.length < 2;
    document.body.classList.toggle("has-places", !nav.hidden);
    // On a phone the header names the page instead of the project.
    const page = document.querySelector(".site-page");
    if (page) page.textContent = current || "";
    window.renderYou(document.getElementById("user-tab"), places, { current: current === "You" });
    fitHeader();
  };
})();
