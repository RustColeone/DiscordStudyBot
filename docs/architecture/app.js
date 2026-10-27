"use strict";
// The architecture page: the code's own structure (data.js, made by tools/architecture.py) as diagrams to click
// through, from the whole system down to one function, whose code shows in the pane on the right. diagram.js lays the
// diagrams out and draws them; this file decides what each level shows, and fills the pane, the search and the trail.
(() => {
  const A = window.ARCH;
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const SLOT = { interfaces: "Interface slot", engines: "Engine slot", capabilities: "Capability slot" };
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
  const BIG = 24;  // a class with more methods than this, in more than one section, is shown section by section
  function sectionsOf(path, name) {
    const sections = new Map();
    for (const method of methodsOf(path, name)) {
      const key = method.section || "";
      if (!sections.has(key)) sections.set(key, []);
      sections.get(key).push(method);
    }
    return sections;
  }
  const big = (path, name) => methodsOf(path, name).length > BIG && sectionsOf(path, name).size > 1;
  const sectionKey = (section) => section || "-";
  const sectionName = (key) => (key === "-" || !key ? "(no section)" : key);
  const github = (path, start, end) => `${A.repo}/blob/${A.branch}/${path}${start ? `#L${start}${end ? `-L${end}` : ""}` : ""}`;
  const firstLine = (text) => String(text || "").split("\n")[0];

  // ---- charts: what each level shows (diagram.js lays them out and draws them) -----------------------------
  const { Chart } = window.Diagram;
  const once = () => { const seen = new Set(); return (key) => (seen.has(key) ? false : (seen.add(key), true)); };
  const CORE_KIND = { GW: "gateway", MG: "manager", PR: "providers", BOT: "store", TANK: "store", START: "startup" };
  const symbolNode = (chart, path, symbol, current = "") => chart.node(symbol.kind === "class" ? symbol.name : `${symbol.name}()`, {
    kind: symbol.kind, target: link(path, symbol.id), current: symbol.id === current, tip: firstLine(symbol.doc),
    sub: symbol.kind === "class" ? plural(methodsOf(path, symbol.name).length, "method") : "" });

  function topChart() {
    const chart = new Chart("DOWN");
    const supervisor = chart.node("Supervisor", { kind: "supervisor", sub: "parent process · heartbeat · restart",
                                                  target: link("core/supervisor.py"), tip: firstLine(files["core/supervisor.py"]?.doc) });
    const inSlot = (key) => Object.keys(A.modules).filter((folder) => A.modules[folder].slots.includes(key));
    const module = (folder) => chart.node(folder.split("/").pop(), { kind: "module", target: link(folder), tip: A.modules[folder].description,
                                                                    sub: folder.startsWith("plugins/") ? "plugin" : "built-in" });
    const slots = {};
    slots.interfaces = chart.group(SLOT.interfaces, () => inSlot("interfaces").forEach(module), { kind: "interfaces", pack: true });
    const parts = {};
    const core = chart.group("Core · child process", () => {
      for (const part of A.core) parts[part.id] = chart.node(part.label, { kind: CORE_KIND[part.id] || "startup", sub: part.note, target: link(part.target) });
    }, { kind: "core" });
    slots.engines = chart.group(SLOT.engines, () => inSlot("engines").forEach(module), { kind: "engines", pack: true });
    slots.capabilities = chart.group(SLOT.capabilities, () => inSlot("capabilities").forEach(module), { kind: "capabilities", pack: true });
    chart.node("contracts", { kind: "contracts", sub: "the only code every part shares", target: link("contracts"),
                              tip: firstLine(files["contracts/__init__.py"]?.doc) });
    for (const root of A.roots.filter((item) => !["contracts", "core", "extensions", "plugins"].includes(item))) {
      chart.node(`${root}/`, { kind: "folder", sub: plural(filesUnder(root).length, "file"), target: link(root) });
    }
    chart.edge(supervisor, core, { dashed: true, label: "runs · watches" });
    chart.edge(slots.interfaces, parts.GW, { arrow: "both", label: "Request / Reply" });
    chart.edge(parts.GW, parts.BOT, { arrow: "none" });
    chart.edge(parts.GW, parts.MG, { arrow: "both" });
    chart.edge(parts.MG, slots.engines, { arrow: "both", label: "Session" });
    chart.edge(parts.MG, slots.capabilities, { arrow: "both", label: "ToolContext" });
    chart.edge(parts.MG, parts.PR, { arrow: "none" });
    chart.edge(parts.MG, parts.TANK, { arrow: "none" });
    return chart;
  }

  function folderChart(path) {
    const chart = new Chart("RIGHT");
    const ids = new Map();
    const childOf = (file) => file.startsWith(`${path}/`) ? `${path}/${file.slice(path.length + 1).split("/")[0]}` : "";
    chart.group(`${path}/`, () => {
      for (const child of A.folders[path].children) {
        const short = child.split("/").pop();
        if (files[child]) {
          const items = files[child].symbols.filter((symbol) => !symbol.parent).length;
          ids.set(child, chart.node(short, { kind: "file", target: link(child), tip: firstLine(files[child].doc),
                                             sub: `${plural(items, "item")} · ${plural(files[child].lines, "line")}` }));
        } else {
          const module = A.modules[child];
          ids.set(child, chart.node(`${short}/`, { kind: module ? "module" : "folder", target: link(child), tip: module?.description || "",
            sub: module ? module.slots.map((slot) => slot.replace(/s$/, "")).join(" · ") || "module" : plural(filesUnder(child).length, "file") }));
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
          const key = ["core", "extensions", "plugins"].includes(parts[0]) && parts.length > 2 ? parts.slice(0, 2).join("/") : parts[0];
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

  function fileChart(path, current = "") {
    const chart = new Chart("RIGHT");
    const ids = new Map();
    for (const symbol of files[path].symbols.filter((item) => !item.parent)) ids.set(symbol.id, symbolNode(chart, path, symbol, current));
    const fresh = once();
    const uses = [];
    for (const symbol of files[path].symbols) {
      const from = ids.get(symbol.parent || symbol.id);
      for (const callee of symbol.calls || []) {
        const to = ids.get(callee.split(".")[0]) || ids.get(callee);
        if (from && to && from !== to && fresh(`${from}>${to}`)) chart.edge(from, to);
      }
      for (const use of symbol.uses || []) if (from) uses.push([from, use]);
    }
    usesNodes(chart, path, uses);
    return chart;
  }

  function sectionsChart(path, name) {
    const chart = new Chart("RIGHT");
    const sections = sectionsOf(path, name);
    const ids = new Map();
    const where = new Map();  // method id → its section
    chart.group(`class ${name}`, () => {
      for (const [section, items] of sections) {
        ids.set(section, chart.node(sectionName(section), { kind: "section", sub: plural(items.length, "method"),
                                                             target: link(path, `${name}@${sectionKey(section)}`),
                                                             tip: items.map((method) => method.name).join(", ") }));
        for (const method of items) where.set(method.id, section);
      }
    }, { kind: "class" });
    const fresh = once();  // calls between sections only: a section's own chart shows the calls into other files
    for (const [section, items] of sections) {
      for (const method of items) {
        for (const callee of method.calls || []) {
          const other = where.get(callee);
          if (other !== undefined && other !== section && fresh(`${section}>${other}`)) chart.edge(ids.get(section), ids.get(other));
        }
      }
    }
    return chart;
  }

  function classChart(path, name, current = "", only = null) {
    if (only === null && big(path, name)) return sectionsChart(path, name);
    const chart = new Chart("RIGHT");
    const everyMethod = methodsOf(path, name);
    const methods = only === null ? everyMethod : everyMethod.filter((method) => (method.section || "") === only);
    const ids = new Map();
    const sections = new Map();
    for (const method of methods) {
      const key = method.section || "";
      if (!sections.has(key)) sections.set(key, []);
      sections.get(key).push(method);
    }
    chart.group(only === null ? `class ${name}` : `${name} · ${sectionName(only)}`, () => {
      for (const [section, items] of sections) {
        const drawAll = () => items.forEach((method) => ids.set(method.id, symbolNode(chart, path, method, current)));
        if (only === null && (sections.size > 1 || section)) chart.group(sectionName(section), drawAll, { kind: "section" });
        else drawAll();
      }
    }, { kind: "class" });
    const otherSections = new Map();  // in one section: the class's other sections it calls into, one box each
    if (only !== null) {
      for (const method of methods) {
        for (const callee of method.calls || []) {
          const target = everyMethod.find((item) => item.id === callee);
          const key = target ? target.section || "" : null;
          if (key !== null && key !== only && !otherSections.has(key)) {
            otherSections.set(key, chart.node(sectionName(key), { kind: "section", sub: `another section of ${name}`,
                                                                   target: link(path, `${name}@${sectionKey(key)}`) }));
          }
        }
      }
    }
    const local = new Map();  // the file's own functions and classes these methods call
    for (const method of methods) {
      for (const callee of method.calls || []) {
        if (!ids.has(callee) && !callee.startsWith(`${name}.`) && symbolOf.has(`${path}::${callee}`)) local.set(callee, null);
      }
    }
    if (local.size) {
      chart.group("in this file", () => {
        for (const callee of local.keys()) local.set(callee, symbolNode(chart, path, symbolOf.get(`${path}::${callee}`)));
      }, { kind: "plain" });
    }
    const fresh = once();
    const uses = [];
    for (const method of methods) {
      const from = ids.get(method.id);
      for (const callee of method.calls || []) {
        const target = everyMethod.find((item) => item.id === callee);
        const elsewhere = target && !ids.has(callee) ? otherSections.get(target.section || "") : undefined;
        const to = ids.get(callee) || local.get(callee) || elsewhere;
        if (to && to !== from && fresh(`${from}>${to}`)) chart.edge(from, to, { dashed: Boolean(elsewhere) });
      }
      for (const use of method.uses || []) uses.push([from, use]);
    }
    usesNodes(chart, path, uses);
    return chart;
  }

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
    const block = make("div", "code");
    const numbers = make("pre", "gutter", lines.map((_, index) => start + index).join("\n"));
    const body = make("pre", "source");
    const inner = make("code");
    inner.innerHTML = highlight(lines.join("\n"));  // escaped above: only our own spans are HTML
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

  function paneTop() {
    const pane = [...heading("Architecture", "DiscordStudyBot")];
    pane.push(make("div", "doc", "Every box is a real folder, file, class or function, read from the code by " +
      "tools/architecture.py. Click a box (or search above) to go deeper: the system, a module, a file, a class, " +
      "and finally one function, whose code shows here."));
    pane.push(make("p", "meta", counts()));
    pane.push(make("h3", "", "Read first"));
    pane.push(linkList([["Core architecture (the rules)", `${A.repo}/blob/${A.branch}/docs/core/ARCHITECTURE.md`],
                        ["Writing a module", `${A.repo}/blob/${A.branch}/docs/core/EXTENSIONS.md`]]));
    for (const slot of Object.keys(SLOT)) {
      const modules = Object.entries(A.modules).filter(([, module]) => module.slots.includes(slot));
      pane.push(make("h3", "", SLOT[slot]), linkList(modules.map(([folder, module]) => [folder, link(folder), module.description])));
    }
    return pane;
  }

  function paneFolder(path) {
    const module = A.modules[path];
    const pane = [...heading(module ? "Module" : "Folder", `${path}/`)];
    if (module) {
      pane.push(make("p", "doc", module.description));
      const chips = make("div", "chips");
      for (const slot of module.slots) chips.append(make("span", "chip", SLOT[slot]));
      pane.push(chips);
    }
    if (A.folders[path].readme) pane.push(make("div", "doc", reflow(A.folders[path].readme)));
    const folderLink = make("a", "", "Folder on GitHub");
    folderLink.href = `${A.repo}/tree/${A.branch}/${path}`;
    folderLink.target = "_blank";
    folderLink.rel = "noopener";
    pane.push(folderLink);
    pane.push(make("h3", "", "Contents"));
    pane.push(linkList(A.folders[path].children.map((child) => files[child]
      ? [child.split("/").pop(), link(child), firstLine(files[child].doc).slice(0, 90)]
      : [`${child.split("/").pop()}/`, link(child), A.modules[child]?.description || plural(filesUnder(child).length, "file")])));
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
      const title = make("h3", "", section || "Methods");
      if (big(path, symbol.name)) { title.textContent = ""; const anchor = make("a", "", sectionName(section)); anchor.href = link(path, `${symbol.name}@${sectionKey(section)}`); title.append(anchor); }
      pane.push(title);
      pane.push(linkList(items.map((method) => [`${method.name}()`, link(path, method.id), firstLine(method.doc).slice(0, 70)])));
    }
    const firstMethod = methods[0]?.line;
    const headEnd = Math.min(firstMethod ? firstMethod - 1 : symbol.end, symbol.line + 80);
    pane.push(make("h3", "", "The class, up to its first method"), code(path, symbol.line, headEnd));
    return pane;
  }

  function paneSection(path, name, section) {
    const items = sectionsOf(path, name).get(section) || [];
    const pane = [...heading("Section", `${name} · ${sectionName(section)}`),
                  make("p", "meta", `${plural(items.length, "method")} of class ${name}, in ${path}`)];
    pane.push(make("h3", "", "Methods"));
    pane.push(linkList(items.map((method) => [`${method.name}()`, link(path, method.id), firstLine(method.doc).slice(0, 70)])));
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
    const parts = path ? path.split("/") : [];
    parts.forEach((part, index) => items.push([part, link(parts.slice(0, index + 1).join("/"))]));
    if (symbol?.parent) items.push([symbol.parent, link(path, symbol.parent)]);
    if (symbol?.section !== undefined && symbol.parent && big(path, symbol.parent)) {
      items.push([sectionName(symbol.section), link(path, `${symbol.parent}@${sectionKey(symbol.section)}`)]);
    }
    if (symbol?.sectionOnly) items.push([symbol.name, ""]);
    else if (symbol) items.push([symbol.name, link(path, symbol.id)]);
    const nav = $("#crumbs");
    nav.replaceChildren();
    items.forEach(([label, href], index) => {
      if (index) nav.append(make("span", "sep", "›"));
      if (index === items.length - 1) nav.append(make("span", "here", label));
      else { const anchor = make("a", "", label); anchor.href = href; nav.append(anchor); }
    });
  }

  function show() {
    const { path, symbol: name } = route();
    hideResults();
    let chart, pane, title, hint, symbol = null;
    if (!path) {
      [chart, pane, title, hint] = [topChart(), paneTop(), "The whole system", "The core in the middle, the modules in its three slots. Click a box to go in."];
    } else if (files[path] && name.includes("@")) {
      const [className, key] = name.split("@");
      const section = key === "-" ? "" : key;
      [chart, pane, title, hint] = [classChart(path, className, "", section), paneSection(path, className, section),
                                    `${className} · ${sectionName(section)}`, "Its methods, and which call which; dotted: into the class's other sections."];
      symbol = { parent: className, name: sectionName(section), sectionOnly: true };
    } else if (files[path]) {
      symbol = name ? symbolOf.get(`${path}::${name}`) || null : null;
      if (symbol?.kind === "class") {
        [chart, pane, title, hint] = [classChart(path, symbol.name), paneClass(path, symbol), `class ${symbol.name}`,
                                      big(path, symbol.name) ? "Its sections (from the file's own section comments), and which call into which. Click one to see its methods."
                                        : "Its methods, grouped by the file's sections, and which call which. Click one for its code."];
      } else if (symbol?.parent) {
        const only = big(path, symbol.parent) ? symbol.section || "" : null;
        [chart, pane, title, hint] = [classChart(path, symbol.parent, symbol.id, only), paneFunction(path, symbol), `${symbol.id}()`,
                                      `In class ${symbol.parent}; its code is on the right.`];
      } else if (symbol) {
        [chart, pane, title, hint] = [fileChart(path, symbol.id), paneFunction(path, symbol), `${symbol.name}()`,
                                      `In ${path.split("/").pop()}; its code is on the right.`];
      } else {
        [chart, pane, title, hint] = [fileChart(path), paneFile(path), path.split("/").pop(),
                                      "Its classes and functions, and which call which (dotted: calls into other files). Click one to go in."];
      }
    } else if (A.folders[path]) {
      [chart, pane, title, hint] = [folderChart(path), paneFolder(path), `${path}/`,
                                    "Its files and folders, and which import which (dotted: imports from elsewhere). Click one to go in."];
    } else {
      [chart, pane, title, hint] = [topChart(), [...heading("Not found", path), make("p", "meta", "Nothing here by that name; the chart shows the whole system.")],
                                    "Not found", ""];
    }
    $("#chart-title").textContent = title;
    $("#chart-hint").textContent = hint;
    document.title = `${title} · DiscordStudyBot architecture`;
    crumbs(path, symbol);
    $("#pane").replaceChildren(...pane);
    $("#pane").scrollTop = 0;
    draw(chart);
  }

  // ---- search -----------------------------------------------------------------------------------------------
  const entries = [
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

  // ---- start --------------------------------------------------------------------------------------------------
  window.addEventListener("hashchange", show);
  show();
})();
