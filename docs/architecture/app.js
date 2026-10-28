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
  const SLOT = { interfaces: "Interface slot", engines: "Engine slot", capabilities: "Capability slot", personas: "Persona slot",
                 watchers: "Watcher slot" };
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
  const CORE_KIND = { GW: "gateway", MG: "manager", PR: "providers", BOT: "store", TANK: "store", START: "startup" };
  const symbolNode = (chart, path, symbol, current = "") => chart.node(symbol.kind === "class" ? symbol.name : `${symbol.name}()`, {
    kind: symbol.kind, target: link(path, symbol.id), current: symbol.id === current, tip: firstLine(symbol.doc),
    sub: symbol.kind === "class" ? plural(methodsOf(path, symbol.name).length, "method") : "" });

  // A module's description, then what it declares it builds on (contracts 1.10).
  function moduleTip(folder) {
    const module = A.modules[folder];
    const declared = [["requires", module.requires], ["uses", module.uses]]
      .filter(([, names]) => names && names.length).map(([key, names]) => `${key} ${names.join(", ")}`);
    return [module.description, ...declared].filter(Boolean).join(" · ");
  }

  function topChart() {
    const chart = new Chart("DOWN", { overview: true });
    const supervisor = chart.node("Supervisor", { kind: "supervisor", sub: "parent process · heartbeat · restart",
                                                  target: link("core/supervisor.py"), tip: firstLine(files["core/supervisor.py"]?.doc) });
    const inSlot = (key) => Object.keys(A.modules).filter((folder) => A.modules[folder].slots.includes(key));
    const module = (folder) => chart.node(folder.split("/").pop(), { kind: "module", target: link(folder), tip: moduleTip(folder),
                                                                    sub: folder.startsWith("plugins/") ? "plugin" : "built-in" });
    const slots = {};
    slots.interfaces = chart.group(SLOT.interfaces, () => inSlot("interfaces").forEach(module), { kind: "interfaces", pack: true });
    const parts = {};
    const core = chart.group("Core · child process", () => {
      for (const part of A.core) parts[part.id] = chart.node(part.label, { kind: CORE_KIND[part.id] || "startup", sub: part.note, target: link(part.target) });
    }, { kind: "core" });
    slots.engines = chart.group(SLOT.engines, () => inSlot("engines").forEach(module), { kind: "engines", pack: true });
    slots.capabilities = chart.group(SLOT.capabilities, () => inSlot("capabilities").forEach(module), { kind: "capabilities", pack: true });
    slots.personas = chart.group(SLOT.personas, () => inSlot("personas").forEach(module), { kind: "personas", pack: true });
    slots.watchers = chart.group(SLOT.watchers, () => inSlot("watchers").forEach(module), { kind: "watchers", pack: true });
    chart.node("How a mod goes in", { kind: "guide", sub: "add a module of your own", target: link(GUIDE) });
    chart.node("contracts", { kind: "contracts", sub: "the only code every part shares", target: link("contracts"),
                              tip: firstLine(files["contracts/__init__.py"]?.doc) });
    for (const root of A.roots.filter((item) => !["contracts", "core", "extensions", "plugins"].includes(item))) {
      chart.node(`${root}/`, { kind: "folder", sub: plural(filesUnder(root).length, "file"), target: link(root) });
    }
    chart.edge(supervisor, core, { dashed: true, label: "runs · watches" });
    chart.edge(slots.interfaces, parts.GW, { arrow: "both", label: "Request / Reply · personas · facts" });
    chart.edge(parts.GW, parts.BOT, { arrow: "none" });
    chart.edge(parts.GW, parts.MG, { arrow: "both" });
    chart.edge(parts.MG, slots.engines, { arrow: "both", label: "Session" });
    chart.edge(parts.MG, slots.capabilities, { arrow: "both", label: "ToolContext" });
    chart.edge(slots.personas, parts.MG, { label: "Persona" });
    chart.edge(slots.personas, parts.PR, { dashed: true, label: "profiles · providers" });
    chart.edge(parts.MG, slots.watchers, { label: "Exchange" });
    chart.edge(slots.watchers, parts.MG, { dashed: true, label: "facts" });
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
        if (files[child]?.data) {
          ids.set(child, chart.node(short, { kind: "data", target: link(child), tip: firstLine(files[child].doc),
                                             sub: plural(files[child].lines, "line") }));
        } else if (files[child]) {
          const items = files[child].symbols.filter((symbol) => !symbol.parent).length;
          ids.set(child, chart.node(short, { kind: "file", target: link(child), tip: firstLine(files[child].doc),
                                             sub: `${plural(items, "item")} · ${plural(files[child].lines, "line")}` }));
        } else {
          const module = A.modules[child];
          ids.set(child, chart.node(`${short}/`, { kind: module ? "module" : "folder", target: link(child), tip: module ? moduleTip(child) : "",
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

  // ---- the guide: how a mod (a module) goes in ------------------------------------------------------------
  const GUIDE = "guide/mods";
  const fileLink = (path) => (files[path] ? link(path) : "");
  const symbolLink = (path, id) => (symbolOf.has(`${path}::${id}`) ? link(path, id) : fileLink(path));
  const sizeOf = (folder) => filesUnder(folder).reduce((sum, path) => sum + files[path].lines, 0);
  const smallest = (slot) => Object.keys(A.modules).filter((folder) => A.modules[folder].slots.includes(slot))
    .sort((a, b) => sizeOf(a) - sizeOf(b))[0];

  function guideChart() {
    const chart = new Chart("DOWN", { overview: true });
    const example = smallest("capabilities");
    const where = chart.group("Where it goes", () => {
      chart.node("extensions/", { kind: "folder", sub: "built-in: ships with the bot", target: link("extensions") });
      chart.node("plugins/", { kind: "folder", sub: "external: drop it in, restart", target: link("plugins") });
      chart.node("modules.paths", { kind: "folder", sub: "core.yml: a folder anywhere" });
    }, { kind: "plain", pack: true });
    const folder = chart.group("Your module: one folder", () => {
      chart.node("extension.py", { kind: "file", sub: "EXTENSION = Extension(name, setup, contracts=\"1\")",
                                   target: example ? fileLink(`${example}/extension.py`) : "" });
      chart.node("your code", { kind: "file", sub: "relative imports: from .engine import …" });
      chart.node("config.example.yml → config.yml", { kind: "file", sub: "its settings; config.yml stays out of git" });
    }, { kind: "plain" });
    const loader = chart.node("Loader", { kind: "startup", sub: "a private package; the contracts version checked",
                                          target: symbolLink("core/loader.py", "import_extension") });
    const setup = chart.node("setup(context)", { kind: "function", sub: "name · config · data_dir · facts · log → Contributions",
                                                 target: symbolLink("core/loader.py", "load_extension") });
    const slots = chart.group("Contributions, wired in by the core", () => {
      chart.node("interfaces", { kind: "gateway", sub: "Interface slot: start(port), deliver(reply)", target: fileLink("contracts/interface.py") });
      chart.node("engines", { kind: "manager", sub: "Engine slot: turns through a Session", target: fileLink("contracts/engine.py") });
      chart.node("capabilities", { kind: "providers", sub: "Capability slot: one action, a ToolResult", target: fileLink("contracts/capability.py") });
      chart.node("personas · profiles · providers · prompt parts", { kind: "contracts", sub: "who answers, on which models",
                                                                     target: symbolLink("contracts/extension.py", "Contributions") });
      chart.node("watchers", { kind: "startup", sub: "Watcher slot: learns from what personas say; publishes facts",
                               target: fileLink("contracts/watchers.py") });
      chart.node("assignments · admission · permissions", { kind: "store", sub: "defaults for its own addresses only" });
    }, { kind: "core", pack: true });
    const check = chart.node("python -m core --check", { kind: "supervisor", sub: "loads every module, lists what it adds",
                                                         target: fileLink("core/__main__.py") });
    chart.edge(where, folder, { label: "put it in" });
    chart.edge(folder, loader, { label: "at startup" });
    chart.edge(loader, setup);
    chart.edge(setup, slots, { label: "returns" });
    chart.edge(slots, check, { dashed: true, label: "see it" });
    return chart;
  }

  function paneGuide() {
    const pane = [...heading("Guide", "How a mod goes in")];
    pane.push(make("div", "doc", "A mod is a module: one folder with an extension.py. The core loads every module at " +
      "startup and wires in what its setup returns, so adding one changes nothing else in the code. The boxes on " +
      "the left open the real code; the full reference is “Writing modules” (docs/core/EXTENSIONS.md)."));
    const steps = [
      ["1. Make a folder", "Built-in modules live in extensions/<name>/ and ship with the bot. Your own go in " +
        "plugins/<name>/ (git-ignored, apart from the tracked examples), or in any folder listed under modules.paths " +
        "in core.yml. The name is lowercase letters, digits and _, unique across the modules."],
      ["2. Declare it in extension.py", "EXTENSION = Extension(\"my_module\", setup, contracts=\"1\", version=\"1\", " +
        "description=\"…\", settings=(…)). contracts is the major version of the contracts it targets; a mismatch " +
        "refuses to load. The folder's other files are imported with relative imports (from .engine import MyEngine)."],
      ["3. Return what it adds from setup(context)", "setup gets context.name, context.config (its config.yml, with " +
        "core.yml's extension_config over it), context.data_dir (a private folder), context.facts (what it publishes " +
        "for pages) and context.log(event, **fields). It returns Contributions(...): interfaces (only they know a " +
        "platform), engines (a turn loop through Session handles), capabilities (one action each, returning a " +
        "ToolResult), personas, model profiles and providers, prompt parts, watchers, and assignments, admission and " +
        "permissions for its own addresses. It runs once, at startup: network loops start in Interface.start, not in " +
        "setup."],
      ["4. Settings", "Ship a documented config.example.yml; the machine's own copy is config.yml beside it " +
        "(git-ignored). Declared Settings can be set with python -m core setup, or $setup in a chat. A secret goes " +
        "to .env only (read it with os.getenv) and is never typed in a chat; only secrets belong in .env."],
      ["5. Keep to the rules", "A module imports only contracts and its own code (tests/test_core_boundaries.py " +
        "checks it). Interfaces never call capabilities; engines never start detached work (routines do that); a " +
        "capability does one action and talks to no one."],
      ["6. Check it, test it, see it here", "python -m core --config core.yml --check loads every module and lists " +
        "what each adds, without starting anything. Add tests under tests/. Then run python tools/architecture.py " +
        "and commit docs/architecture/data.js: the module shows up on this page by itself, in the slots it fills."],
      ["Turning one off", "In core.yml: modules: {exclude: [whale_chan]} (a folder name or a path ending). An " +
        "excluded module is never imported."],
      ["A new student needs no code", "Add a folder to extensions/students/ with a student.yml (name, display_name, " +
        "models, the prompt files in order; aliases, pictures and formerly if she has them) and her prompt.md. The " +
        "students module reads every such folder at startup; her prompt is the files' lines joined with nothing " +
        "between them. Every page shows her picture (pictures: {avatar: avatar.png}) through the core, and a renamed " +
        "student keeps what was hers (formerly: [old name]). See extensions/students/README.md."],
      ["Learning from what is said: watchers and facts", "A module that remembers (the students' memory) adds " +
        "Contributions(watchers=[Watcher(name, run)]): run(exchange, context) is awaited after each message a persona " +
        "says, whichever engine she spoke in, one exchange at a time per persona, beside the conversation (a failure " +
        "is only logged). exchange holds the persona, what she said and the message she answered; context gives " +
        "models (counted in the run's usage, not its budget), the conversation's history, the module's stores and " +
        "facts. A fact is a small public value about a persona, or a persona and one person: " +
        "context.facts.set(persona, \"bond\", {...}, person=exchange.person). Pages read them with port.facts(persona, " +
        "sender_id) and list the students with port.personas() (names, aliases, pictures), so MomoChat shows your " +
        "name for her, her bond and mood without knowing which module keeps them."],
    ];
    for (const [title, text] of steps) pane.push(make("h3", "", title), make("div", "doc", text));
    pane.push(make("h3", "", "Examples to copy, smallest first"));
    const examples = Object.keys(SLOT).map((slot) => [slot, smallest(slot)]).filter(([, folder]) => folder);
    pane.push(linkList([...examples.map(([slot, folder]) => [folder, link(folder), `${SLOT[slot]} · ${A.modules[folder].description}`]),
      ...(A.modules["plugins/whale_chan"] ? [["plugins/whale_chan", link("plugins/whale_chan"), "a complete drop-in: her own Discord bot, engine and persona"]] : [])]));
    pane.push(make("h3", "", "Read more"));
    pane.push(linkList([["Writing modules: the full reference", `${A.repo}/blob/${A.branch}/docs/core/EXTENSIONS.md`],
                        ["Core architecture: the rules", `${A.repo}/blob/${A.branch}/docs/core/ARCHITECTURE.md`]]));
    const example = smallest("capabilities");
    if (example && files[`${example}/extension.py`]) {
      pane.push(make("h3", "", `The smallest real module: ${example}/extension.py`), code(`${example}/extension.py`, 1, files[`${example}/extension.py`].lines));
    }
    return pane;
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

  function paneTop() {
    const pane = [...heading("Architecture", "DiscordStudyBot")];
    pane.push(make("div", "doc", "Every box is a real folder, file, class or function, read from the code by " +
      "tools/architecture.py. Click a box (or search above) to go deeper: the system, a module, a file (its classes " +
      "drawn around their methods), and one function, whose code shows here. A click keeps your zoom."));
    pane.push(make("p", "meta", counts()));
    pane.push(make("h3", "", "Read first"));
    pane.push(linkList([["How a mod goes in", link(GUIDE)],
                        ["Core architecture (the rules)", `${A.repo}/blob/${A.branch}/docs/core/ARCHITECTURE.md`],
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
    const parts = path && path !== GUIDE ? path.split("/") : [];
    if (path === GUIDE) items.push(["How a mod goes in", link(GUIDE)]);
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
  }

  function show() {
    const { path, symbol: name } = route();
    hideResults();
    let chart, pane, title, hint, symbol = null;
    if (!path) {
      [chart, pane, title, hint] = [topChart(), paneTop(), "The whole system", "The core in the middle, the modules around it in the slots they fill. Click a box to go in."];
    } else if (path === GUIDE) {
      [chart, pane, title, hint] = [guideChart(), paneGuide(), "How a mod goes in",
                                    "From one folder to the core's slots. Click a box for the real code; the steps are on the right."];
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
        [pane, title, hint] = [paneFunction(path, symbol), `${symbol.id}()`, `In ${where}, outlined; its code is on the right.`];
      } else {
        [pane, title, hint] = [paneFile(path), where,
                               "Its functions, and each class as a box around its methods; arrows: who calls whom. Hover a box for its calls into other files."];
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
    { label: "How a mod goes in", where: "guide: adding a module, plugin or extension", href: link(GUIDE), key: "how a mod goes in module plugin extension guide" },
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
