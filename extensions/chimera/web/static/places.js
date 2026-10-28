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
