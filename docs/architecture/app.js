"use strict";
// The architecture page: the code's own structure (data.js, made by tools/architecture.py) as diagrams to click
// through, from the whole system down to one function, whose code shows in the pane beside it (on a phone, a tap away).
// diagram.js lays the diagrams out and draws them; this file decides what each level shows, and fills the pane, the
// search and the trail. It is the same in every repository with such a page: what one project has of its own (its top
// map, its guides, how a module looks among the folders) is in site.js (window.SITE).
(() => {
  const A = window.ARCH;
  const SITE = window.SITE;
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const files = A.files;

  // ---- the index: every symbol by file, and who calls it (inside its file) -----------------------------
  const symbolOf = new Map();
  const calledBy = new Map();
  for (const [path, file] of Object.entries(files)) {
    for (const symbol of file.symbols) symbolOf.set(`${path}::${symbol.id}`, symbol);
    for (const symbol of file.symbols) {
      for (const callee of symbol.calls || []) {
        const key = `${path}::${callee}`;
        if (!calledBy.has(key)) calledBy.set(key, []);
        calledBy.get(key).push(symbol.id);
      }
    }
  }
  const link = (path, symbol = "") => `#/${path}${symbol ? `::${symbol}` : ""}`;
  const route = () => {
    let raw = "";
    try { raw = decodeURIComponent(location.hash.replace(/^#\/?/, "")); } catch { raw = ""; }
    const [path, symbol] = raw.split("::");
    return { path: path || "", symbol: symbol || "" };
  };
  const filesUnder = (folder) => Object.keys(files).filter((path) => path.startsWith(`${folder}/`));
  const plural = (count, word) => `${count.toLocaleString()} ${word}${count === 1 ? "" : /(s|sh|ch|x)$/.test(word) ? "es" : "s"}`;
  const methodsOf = (path, name) => files[path].symbols.filter((symbol) => symbol.parent === name);
  function sectionsOf(path, name) {  // a class's methods by the file's `# ---- name ----` comments
    const sections = new Map();
    for (const method of methodsOf(path, name)) {
      const key = method.section || "";
      if (!sections.has(key)) sections.set(key, []);
      sections.get(key).push(method);
    }
    return sections;
  }
  const sectionName = (section) => section || "(no section)";
  const github = (path, start, end) => `${A.repo}/blob/${A.branch}/${path}${start ? `#L${start}${end ? `-L${end}` : ""}` : ""}`;
  const firstLine = (text) => String(text || "").split("\n")[0];

  // ---- charts: what each level shows (diagram.js lays them out and draws them) -----------------------------
  const { Chart } = window.Diagram;
  const once = () => { const seen = new Set(); return (key) => (seen.has(key) ? false : (seen.add(key), true)); };
  const symbolNode = (chart, path, symbol, current = "") => chart.node(symbol.kind === "class" ? symbol.name : `${symbol.name}()`, {
    kind: symbol.kind, target: link(path, symbol.id), current: symbol.id === current, tip: firstLine(symbol.doc),
    sub: symbol.kind === "class" ? plural(methodsOf(path, symbol.name).length, "method") : "" });

  function folderChart(path) {
    const chart = new Chart("RIGHT");
    const ids = new Map();
    const childOf = (file) => file.startsWith(`${path}/`) ? `${path}/${file.slice(path.length + 1).split("/")[0]}` : "";
    chart.group(`${path}/`, () => {
      for (const child of A.folders[path].children) {
        const short = child.split("/").pop();
        if (files[child]?.data) {
          ids.set(child, chart.node(short, { kind: "data", target: link(child), tip: firstLine(files[child].doc),
                                             sub: plural(files[child].lines, "line") }));
        } else if (files[child]) {
          const items = files[child].symbols.filter((symbol) => !symbol.parent).length;
          ids.set(child, chart.node(short, { kind: "file", target: link(child), tip: firstLine(files[child].doc),
                                             sub: `${plural(items, "item")} · ${plural(files[child].lines, "line")}` }));
        } else {
          const box = SITE.folderBox?.(helpers, child);
          ids.set(child, chart.node(`${short}/`, { target: link(child), kind: "folder", tip: "",
                                                   sub: plural(filesUnder(child).length, "file"), ...(box || {}) }));
        }
      }
    }, { kind: "folder" });
    const outside = new Map();
    const fresh = once();
    for (const file of filesUnder(path)) {
      const from = ids.get(childOf(file));
      for (const item of files[file].imports) {
        if (!item.path) continue;
        const inside = childOf(item.path);
        if (inside) {
          const to = ids.get(inside);
          if (to && to !== from && fresh(`${from}>${to}`)) chart.edge(from, to);
        } else {
          const parts = item.path.split("/");
          const key = (SITE.deep || []).includes(parts[0]) && parts.length > 2 ? parts.slice(0, 2).join("/") : parts[0];
          if (!outside.has(key)) outside.set(key, chart.node(files[key] ? key : `${key}/`, { kind: "external", target: link(key), sub: "elsewhere" }));
          if (fresh(`${from}>${key}`)) chart.edge(from, outside.get(key), { dashed: true });
        }
      }
    }
    return chart;
  }

  function usesNodes(chart, path, pairs) {  // calls into other files: one box per file, dotted
    const nodes = new Map();
    const counts = new Map();
    for (const [, use] of pairs) counts.set(use.path, (counts.get(use.path) || 0) + 1);
    const kept = new Set([...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([key]) => key));
    const fresh = once();
    for (const [from, use] of pairs) {
      if (!kept.has(use.path) || use.path === path) continue;
      if (!nodes.has(use.path)) nodes.set(use.path, chart.node(use.path.split("/").pop(), { kind: "external", sub: use.path, target: link(use.path) }));
      if (fresh(`${from}>${use.path}`)) chart.edge(from, nodes.get(use.path), { dashed: true });
    }
  }

  // A file: its functions, and each class as a box around its methods (in the file's sections when it has them).
  function fileChart(path, current = "") {
    const chart = new Chart("RIGHT", { compact: true });
    const ids = new Map();      // symbol id → its box (a class: its group)
    const ownClass = new Map(); // method id → its class
    for (const symbol of files[path].symbols.filter((item) => !item.parent)) {
      const methods = symbol.kind === "class" ? methodsOf(path, symbol.name) : [];
      if (symbol.kind !== "class") { ids.set(symbol.id, symbolNode(chart, path, symbol, current)); continue; }
      if (!methods.length) {  // a class with no methods of its own (a dataclass, say) is one box
        ids.set(symbol.id, chart.node(symbol.name, { kind: "class", target: link(path, symbol.id), current: symbol.id === current,
                                                     tip: firstLine(symbol.doc), sub: symbol.bases.length ? `(${symbol.bases.join(", ")})` : "class" }));
        continue;
      }
      const sections = sectionsOf(path, symbol.name);
      ids.set(symbol.id, chart.group(`class ${symbol.name}${symbol.bases.length ? `(${symbol.bases.join(", ")})` : ""}`, () => {
        for (const [section, items] of sections) {
          const drawAll = () => items.forEach((method) => {
            ids.set(method.id, symbolNode(chart, path, method, current));
            ownClass.set(method.id, symbol.name);
          });
          if (sections.size > 1 || section) chart.group(sectionName(section), drawAll, { kind: "section" });
          else drawAll();
        }
      }, { kind: "class", target: link(path, symbol.id), current: symbol.id === current, tip: firstLine(symbol.doc) }));
    }
    const fresh = once();
    const uses = [];
    for (const symbol of files[path].symbols.filter((item) => item.kind !== "class")) {
      const from = ids.get(symbol.id);
      for (const callee of symbol.calls || []) {
        const to = ids.get(callee);
        if (!to || to === from || callee === ownClass.get(symbol.id)) continue;  // (never into its own class's box)
        if (fresh(`${from}>${to}`)) chart.edge(from, to);
      }
      for (const use of symbol.uses || []) uses.push([from, use]);
    }
    usesNodes(chart, path, uses);
    return chart;
  }

  // A data file (settings, a student's prompt): the files it names, in their order, and the files that name it.
  const namedBy = (path) => Object.keys(files).filter((other) => other !== path && files[other].imports.some((item) => item.path === path));
  const near = (path, from) => {  // a path as short as it can be next to another, with its folder: shared/school.md
    const [a, b] = [path.split("/"), from.split("/")];
    let shared = 0;
    while (shared < a.length - 1 && a[shared] === b[shared]) shared += 1;
    return a.slice(Math.min(shared, a.length - 2)).join("/");
  };
  function dataChart(path) {
    const chart = new Chart("RIGHT");
    const box = (other, current = false) => chart.node(current ? other.split("/").pop() : near(other, path), {
      kind: files[other]?.data ? "data" : "file", target: link(other), current, tip: firstLine(files[other]?.doc),
      sub: plural(files[other]?.lines || 0, "line") });
    const here = box(path, true);
    files[path].imports.filter((item) => item.path).forEach((item, index) => chart.edge(here, box(item.path), { label: `${index + 1}` }));
    for (const other of namedBy(path)) chart.edge(box(other), here);
    return chart;
  }

  const fileLink = (path) => (files[path] ? link(path) : "");
  const symbolLink = (path, id) => (symbolOf.has(`${path}::${id}`) ? link(path, id) : fileLink(path));
  const guides = new Map((SITE.guides || []).map((guide) => [guide.path, guide]));

  // ---- drawing, with zoom controls -------------------------------------------------------------------------
  let view = null;
  async function draw(chart) {
    const holder = $("#chart");
    holder.replaceChildren(make("div", "chart-wait", "Laying out…"));
    $("#zoom").hidden = true;
    try {
      const drawn = await window.Diagram.draw(holder, chart, {
        onGo: (target) => { location.hash = target; },
        onZoom: (scale) => { $("#zoom-level").textContent = `${Math.round(scale * 100)}%`; },
      });
      if (!drawn) return;  // a newer chart was asked for meanwhile
      view = drawn;
      $("#zoom").hidden = false;
    } catch (error) {
      holder.replaceChildren(make("div", "chart-wait", /load/.test(String(error?.message))
        ? "The diagrams need their layout library (ELK), loaded from the internet, and it could not be reached. The details on the right still work."
        : `This diagram could not be drawn: ${error?.message || error}`));
    }
  }
  $("#zoom-in").addEventListener("click", () => view?.zoomBy(1.25));
  $("#zoom-out").addEventListener("click", () => view?.zoomBy(0.8));
  $("#zoom-fit").addEventListener("click", () => view?.fit(1.5));
  $("#zoom-level").addEventListener("click", () => view?.zoomBy(1 / (view?.scale || 1)));  // back to 100%

  // ---- the pane on the right ------------------------------------------------------------------------------
  const linkList = (items) => {
    const list = make("ul", "links");
    for (const [label, href, note] of items) {
      const item = make("li");
      const anchor = make("a", "", label);
      anchor.href = href;
      if (note) anchor.append(make("small", "", note));
      item.append(anchor);
      list.append(item);
    }
    return list;
  };
  const heading = (kind, title) => [make("span", "kind", kind), make("h2", "", title)];
  const reflow = (text) => String(text).split(/\n\s*\n/).map((paragraph) => /^\s|^[-*•]|\S {2,}\S/m.test(paragraph)
    ? paragraph : paragraph.replace(/\s*\n\s*/g, " ")).join("\n\n");  // keep indented, listed or aligned lines
  const doc = (text) => text ? make("div", "doc", reflow(text)) : make("p", "meta", "No docstring.");
  const source = (path, start, end, label = "View on GitHub") => {
    const anchor = make("a", "", label);
    anchor.href = github(path, start, end);
    anchor.target = "_blank";
    anchor.rel = "noopener";
    return anchor;
  };
  const escapeHtml = (text) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const TOKENS = /("""[\s\S]*?"""|'''[\s\S]*?'''|\b[rbfuRBFU]{0,2}"(?:\\.|[^"\\\n])*"|\b[rbfuRBFU]{0,2}'(?:\\.|[^'\\\n])*')|(#[^\n]*)|(@[\w.]+)|\b(def|class)(\s+)(\w+)|\b(False|None|True|and|as|assert|async|await|break|continue|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|self|cls)\b|\b(\d+(?:\.\d+)?)\b/g;
  function highlight(text) {
    let html = "", last = 0, match;
    TOKENS.lastIndex = 0;
    while ((match = TOKENS.exec(text))) {
      html += escapeHtml(text.slice(last, match.index));
      if (match[1]) html += `<span class="str">${escapeHtml(match[1])}</span>`;
      else if (match[2]) html += `<span class="com">${escapeHtml(match[2])}</span>`;
      else if (match[3]) html += `<span class="dec">${escapeHtml(match[3])}</span>`;
      else if (match[4]) html += `<span class="kw">${match[4]}</span>${match[5]}<span class="def">${escapeHtml(match[6])}</span>`;
      else if (match[7]) html += `<span class="kw">${match[7]}</span>`;
      else html += `<span class="num">${match[8]}</span>`;
      last = TOKENS.lastIndex;
    }
    return html + escapeHtml(text.slice(last));
  }
  function code(path, start, end) {
    const lines = files[path].text.split("\n").slice(start - 1, end);
    const data = files[path].data;
    const block = make("div", "code");
    const numbers = make("pre", "gutter", lines.map((_, index) => start + index).join("\n"));
    const body = make("pre", "source");
    const inner = make("code");
    if (data && path.endsWith(".md")) inner.textContent = lines.join("\n");
    else if (data) inner.innerHTML = escapeHtml(lines.join("\n")).replace(/(^|\s)(#[^\n]*)/g, '$1<span class="com">$2</span>');
    else inner.innerHTML = highlight(lines.join("\n"));  // escaped above: only our own spans are HTML
    body.append(inner);
    block.append(numbers, body);
    return block;
  }
  const counts = () => {
    const all = Object.values(files);
    const symbols = all.flatMap((file) => file.symbols);
    return `${plural(all.length, "file")} · ${plural(symbols.filter((item) => item.kind === "class").length, "class")} · ` +
      `${plural(symbols.filter((item) => item.kind !== "class").length, "function")} · ${plural(all.reduce((sum, file) => sum + file.lines, 0), "line")}`;
  };

  function paneFolder(path) {
    const own = SITE.folderPane?.(helpers, path);  // (a module: its description, the slots it fills)
    const pane = [...heading(own?.kind || "Folder", `${path}/`), ...(own?.nodes || [])];
    if (A.folders[path].readme) pane.push(make("div", "doc", reflow(A.folders[path].readme)));
    const folderLink = make("a", "", "Folder on GitHub");
    folderLink.href = `${A.repo}/tree/${A.branch}/${path}`;
    folderLink.target = "_blank";
    folderLink.rel = "noopener";
    pane.push(folderLink);
    pane.push(make("h3", "", "Contents"));
    pane.push(linkList(A.folders[path].children.map((child) => files[child]
      ? [child.split("/").pop(), link(child), firstLine(files[child].doc).slice(0, 90)]
      : [`${child.split("/").pop()}/`, link(child), A.modules?.[child]?.description || plural(filesUnder(child).length, "file")])));
    return pane;
  }

  function paneData(path) {
    const file = files[path];
    const pane = [...heading("Data file", path), make("p", "meta", plural(file.lines, "line")), source(path)];
    if (file.doc) pane.push(make("div", "doc", file.doc));
    const named = file.imports.filter((item) => item.path);
    if (named.length) pane.push(make("h3", "", "Names, in this order"), linkList(named.map((item) => [item.module, link(item.path), item.path])));
    const by = namedBy(path);
    if (by.length) pane.push(make("h3", "", "Named by"), linkList(by.map((other) => [near(other, path), link(other), firstLine(files[other].doc).slice(0, 70)])));
    pane.push(make("h3", "", "Contents"), code(path, 1, file.lines));
    return pane;
  }

  function paneFile(path) {
    const file = files[path];
    const pane = [...heading("File", path), make("p", "meta", plural(file.lines, "line")), source(path), doc(file.doc)];
    const own = file.imports.filter((item) => item.path);
    if (own.length) pane.push(make("h3", "", "Imports from this repository"), linkList(own.map((item) => [item.path, link(item.path), item.module])));
    const other = file.imports.filter((item) => !item.path).map((item) => item.module);
    if (other.length) {
      const chips = make("div", "chips");
      for (const name of other) chips.append(make("span", "chip", name));
      pane.push(make("h3", "", "Other imports"), chips);
    }
    const top = file.symbols.filter((symbol) => !symbol.parent);
    if (top.length) {
      pane.push(make("h3", "", "Classes and functions"));
      pane.push(linkList(top.map((symbol) => [symbol.kind === "class" ? `class ${symbol.name}` : `${symbol.name}()`, link(path, symbol.id),
        symbol.kind === "class" ? plural(methodsOf(path, symbol.name).length, "method") : firstLine(symbol.doc).slice(0, 70)])));
    }
    return pane;
  }

  function paneClass(path, symbol) {
    const methods = methodsOf(path, symbol.name);
    const pane = [...heading("Class", symbol.name), make("div", "signature", symbol.signature),
                  make("p", "meta", `${path} · lines ${symbol.line}–${symbol.end}`), source(path, symbol.line, symbol.end), doc(symbol.doc)];
    const groups = new Map();
    for (const method of methods) {
      if (!groups.has(method.section || "")) groups.set(method.section || "", []);
      groups.get(method.section || "").push(method);
    }
    for (const [section, items] of groups) {
      pane.push(make("h3", "", section || "Methods"));
      pane.push(linkList(items.map((method) => [`${method.name}()`, link(path, method.id), firstLine(method.doc).slice(0, 70)])));
    }
    const firstMethod = methods[0]?.line;
    const headEnd = Math.min(firstMethod ? firstMethod - 1 : symbol.end, symbol.line + 80);
    pane.push(make("h3", "", "The class, up to its first method"), code(path, symbol.line, headEnd));
    return pane;
  }

  function paneFunction(path, symbol) {
    const pane = [...heading(symbol.kind === "method" ? "Method" : "Function", symbol.id), make("div", "signature", symbol.signature),
                  make("p", "meta", `${path} · lines ${symbol.line}–${symbol.end}`), source(path, symbol.line, symbol.end)];
    if (symbol.decorators.length) {
      const chips = make("div", "chips");
      for (const name of symbol.decorators) chips.append(make("span", "chip", `@${name}`));
      pane.push(chips);
    }
    pane.push(doc(symbol.doc));
    const named = (id) => { const target = symbolOf.get(`${path}::${id}`); return [target?.kind === "class" ? `class ${id}` : `${id}()`, link(path, id)]; };
    if (symbol.calls.length) pane.push(make("h3", "", "Calls, in this file"), linkList(symbol.calls.map(named)));
    const callers = calledBy.get(`${path}::${symbol.id}`) || [];
    if (callers.length) pane.push(make("h3", "", "Called by, in this file"), linkList(callers.map(named)));
    if (symbol.uses.length) {
      pane.push(make("h3", "", "Uses from other files"));
      pane.push(linkList(symbol.uses.map((use) => [use.name, symbolOf.has(`${use.path}::${use.name}`) ? link(use.path, use.name) : link(use.path), use.path])));
    }
    pane.push(make("h3", "", "Code"), code(path, symbol.line, symbol.end));
    return pane;
  }

  // ---- where you are --------------------------------------------------------------------------------------
  function crumbs(path, symbol) {
    const items = [["Architecture", "#/"]];
    const parts = path && !guides.has(path) ? path.split("/") : [];
    if (guides.has(path)) items.push([guides.get(path).label, link(path)]);
    parts.forEach((part, index) => items.push([part, link(parts.slice(0, index + 1).join("/"))]));
    if (symbol?.parent) items.push([symbol.parent, link(path, symbol.parent)]);
    if (symbol) items.push([symbol.name, link(path, symbol.id)]);
    const nav = $("#crumbs");
    nav.replaceChildren();
    items.forEach(([label, href], index) => {
      if (index) nav.append(make("span", "sep", "›"));
      if (index === items.length - 1) nav.append(make("span", "here", label));
      else { const anchor = make("a", "", label); anchor.href = href; nav.append(anchor); }
    });
    nav.scrollLeft = nav.scrollWidth;  // (on a phone the trail scrolls sideways: where you are stays in view)
  }

  function show() {
    const { path, symbol: name } = route();
    hideResults();
    let chart, pane, title, hint, symbol = null;
    if (!path) {
      [chart, pane, title, hint] = [SITE.top.chart(helpers), SITE.top.pane(helpers), SITE.top.title, SITE.top.hint];
    } else if (guides.has(path)) {
      const guide = guides.get(path);
      [chart, pane, title, hint] = [guide.chart(helpers), guide.pane(helpers), guide.title, guide.hint];
    } else if (files[path]?.data) {
      [chart, pane, title, hint] = [dataChart(path), paneData(path), path.split("/").pop(),
                                    "A data file: the files it names, numbered in their order, and the files that name it."];
    } else if (files[path]) {
      symbol = name ? symbolOf.get(`${path}::${name.split("@")[0]}`) || null : null;  // (an old section link: its class)
      const where = path.split("/").pop();
      chart = fileChart(path, symbol?.id || "");
      if (symbol?.kind === "class") {
        [pane, title, hint] = [paneClass(path, symbol), `class ${symbol.name}`, `In ${where}, outlined; its methods are inside it.`];
      } else if (symbol) {
        [pane, title, hint] = [paneFunction(path, symbol), `${symbol.id}()`, `In ${where}, outlined; its code is in the details.`];
      } else {
        [pane, title, hint] = [paneFile(path), where,
                               "Its functions, and each class as a box around its methods; arrows: who calls whom. Hover a box for its calls into other files."];
      }
    } else if (A.folders[path]) {
      [chart, pane, title, hint] = [folderChart(path), paneFolder(path), `${path}/`,
                                    "Its files and folders, and which import which (dotted: imports from elsewhere). Click one to go in."];
    } else {
      [chart, pane, title, hint] = [SITE.top.chart(helpers), [...heading("Not found", path), make("p", "meta", "Nothing here by that name; the chart shows the whole system.")],
                                    "Not found", ""];
    }
    $("#chart-title").textContent = title;
    $("#chart-hint").textContent = hint;
    document.title = `${title} · ${SITE.name} architecture`;
    crumbs(path, symbol);
    $("#pane").replaceChildren(...pane);
    $("#pane").scrollTop = 0;
    showPanel(symbol || files[path]?.data ? "pane" : "chart");  // (on a phone: the code when there is code to read)
    draw(chart);
  }

  // ---- search -----------------------------------------------------------------------------------------------
  const entries = [
    ...[...guides.values()].map((guide) => ({ label: guide.label, where: "guide", href: link(guide.path),
                                              key: `${guide.label} ${guide.keywords || ""} guide`.toLowerCase() })),
    ...Object.keys(A.folders).map((path) => ({ label: `${path.split("/").pop()}/`, where: path, href: link(path), key: path.toLowerCase() })),
    ...Object.keys(files).map((path) => ({ label: path.split("/").pop(), where: path, href: link(path), key: path.toLowerCase() })),
    ...[...symbolOf.entries()].map(([key, symbol]) => ({ label: symbol.kind === "class" ? `class ${symbol.id}` : `${symbol.id}()`,
                                                         where: key.split("::")[0], href: link(key.split("::")[0], symbol.id), key: symbol.id.toLowerCase() })),
  ];
  const results = $("#results");
  let active = 0;
  function hideResults() { results.hidden = true; }
  function search() {
    const words = $("#search").value.trim().toLowerCase();
    if (!words) { hideResults(); return; }
    const found = entries.filter((entry) => entry.key.includes(words))
      .sort((a, b) => (b.key.endsWith(words) - a.key.endsWith(words)) || a.key.length - b.key.length).slice(0, 40);
    active = 0;
    results.replaceChildren(...found.map((entry, index) => {
      const item = make("li");
      const anchor = make("a", index === 0 ? "is-active" : "", entry.label);
      anchor.href = entry.href;
      anchor.append(make("small", "", entry.where));
      item.append(anchor);
      return item;
    }));
    if (!found.length) results.replaceChildren(make("li", "", "Nothing by that name."));
    results.hidden = false;
  }
  $("#search").addEventListener("input", search);
  $("#search").addEventListener("keydown", (event) => {
    const anchors = [...results.querySelectorAll("a")];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!anchors.length) return;
      anchors[active]?.classList.remove("is-active");
      active = (active + (event.key === "ArrowDown" ? 1 : anchors.length - 1)) % anchors.length;
      anchors[active].classList.add("is-active");
      anchors[active].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && anchors[active]) {
      location.hash = anchors[active].getAttribute("href");
      $("#search").value = "";
    } else if (event.key === "Escape") {
      hideResults();
    }
  });
  document.addEventListener("click", (event) => { if (!event.target.closest(".search")) hideResults(); });

  // ---- on a phone: the diagram or the details, one at a time (side by side on a wide screen) ----------------
  function showPanel(name) {
    document.body.classList.toggle("show-pane", name === "pane");
    for (const button of document.querySelectorAll("#views button")) {
      button.setAttribute("aria-selected", String(button.dataset.view === name));
    }
  }
  for (const button of document.querySelectorAll("#views button")) {
    button.addEventListener("click", () => showPanel(button.dataset.view));
  }

  // ---- start --------------------------------------------------------------------------------------------------
  // What site.js may use to draw its own charts and panes.
  const helpers = { A, files, Chart, link, make, linkList, heading, code, doc, source, plural, firstLine, filesUnder,
                    symbolOf, fileLink, symbolLink, counts, github: (path, start, end) => github(path, start, end) };
  $(".brand").replaceChildren(document.createTextNode(`${SITE.name} `), make("span", "", "architecture"));
  const firstGuide = (SITE.guides || [])[0];
  if (firstGuide) Object.assign($(".guide-link"), { href: link(firstGuide.path), textContent: firstGuide.label });
  else $(".guide-link").remove();
  window.addEventListener("hashchange", show);
  show();
})();
