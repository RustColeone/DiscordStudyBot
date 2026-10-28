"use strict";
// The places bar: every place's views, from what each interface publishes in the core's directory (`views` in its
// status: name, icon, a path relative to its link, and an `order`), sorted by order, so each page links to the
// others without knowing them. `places` is {here, others}; `current` names this page's view. It sits in the page's
// header between the project's name and the stamina; on a phone it is the bar along the bottom. (The Classroom serves
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
  // `students`: [{name, href, picture, plate, stamina}]; only those with an account or a daily allowance show.
  window.renderStamina = (holder, students) => {
    if (!holder) return;
    const shown = (students || []).filter((item) => item.stamina && (item.stamina.account || item.stamina.today?.allowance));
    holder.hidden = !shown.length;
    if (!shown.length) { holder.replaceChildren(); return; }
    const bolt = document.createElement("span");
    bolt.className = "stamina-bolt";
    bolt.setAttribute("aria-hidden", "true");
    bolt.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="#f0a04b" stroke="#a8601c" stroke-width="1.5" ' +
                     'stroke-linejoin="round"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/></svg>';
    const people = shown.map((item) => {
      const view = window.staminaOf(item.stamina);
      const link = document.createElement("a");
      link.className = "stamina-who";
      link.href = item.href || "#";
      link.setAttribute("aria-label", `${item.name}: ${[view.accountText, view.todayText && `today ${view.todayText} left`]
        .filter(Boolean).join("; ")}`);
      link.title = link.getAttribute("aria-label");
      const face = document.createElement(item.picture ? "img" : "span");
      face.className = "stamina-face";
      if (item.picture) { face.src = item.picture; face.alt = ""; } else face.textContent = item.name.charAt(0);
      const bars = document.createElement("span");
      bars.className = "stamina-bars";
      for (const [fraction, kind] of [[view.account, "account"], [view.today, "today"]]) {
        const track = document.createElement("span");
        track.className = `stamina-track is-${kind}`;
        const fill = document.createElement("span");
        fill.className = "stamina-fill";
        fill.style.width = `${Math.round((fraction ?? 0) * 100)}%`;
        if (kind === "today") fill.style.background = item.plate || "#5a5296";
        if (fraction == null) track.classList.add("is-unknown");
        track.append(fill);
        bars.append(track);
      }
      link.append(face, bars);
      return link;
    });
    const note = document.createElement("span");
    note.className = "stamina-note";
    note.innerHTML = "stamina<br>and today";
    holder.replaceChildren(bolt, ...people, note);
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
  };
})();
