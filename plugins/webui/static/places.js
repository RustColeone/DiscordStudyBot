"use strict";
// The places bar: every place's views, from what each interface publishes in the core's directory (`views` in its
// status: name, icon, a path relative to its link, and an `order`), sorted by order, so each page links to the
// others without knowing them. `places` is {here, others}; `current` names this page's view.
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
      entries.push({ name: view.name, icon: String(view.icon || "📍"), href, order: Number(view.order) || 100 });
    }
  };
  add(places?.here, true);
  for (const place of places?.others || []) add(place, false);
  entries.sort((a, b) => a.order - b.order);
  nav.replaceChildren(...entries.map((entry) => {
    const link = document.createElement("a");
    link.className = `places-tab${entry.name === current ? " is-current" : ""}`;
    link.href = entry.href;
    if (entry.name === current) link.setAttribute("aria-current", "page");
    const icon = document.createElement("span");
    icon.className = "places-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = entry.icon;
    const label = document.createElement("span");
    label.className = "places-label";
    label.textContent = entry.name;
    link.append(icon, label);
    return link;
  }));
  nav.hidden = entries.length < 2;
};
