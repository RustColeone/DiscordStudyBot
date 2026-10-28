"use strict";
// What is DiscordStudyBot's own on the architecture page: the top map (the core, and the modules in the slots they
// fill), the guide "How a mod goes in", and how a module looks among the folders. app.js, diagram.js and style.css are
// the same viewer in every repository that has one (mneme-llm's too); this file is what differs.
window.SITE = (() => {
  const SLOT = { interfaces: "Interface slot", engines: "Engine slot", capabilities: "Capability slot" };
  const CORE_KIND = { GW: "gateway", MG: "manager", PR: "providers", BOT: "store", TANK: "store", START: "startup" };
  const GUIDE = "guide/mods";

  const sizeOf = (h, folder) => h.filesUnder(folder).reduce((sum, path) => sum + h.files[path].lines, 0);
  const smallest = (h, slot) => Object.keys(h.A.modules).filter((folder) => h.A.modules[folder].slots.includes(slot))
    .sort((a, b) => sizeOf(h, a) - sizeOf(h, b))[0];

  function topChart(h) {
    const { A, files, link, plural, firstLine, filesUnder } = h;
    const chart = new h.Chart("DOWN", { overview: true });
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
    chart.node("How a mod goes in", { kind: "guide", sub: "add a module of your own", target: link(GUIDE) });
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

  function topPane(h) {
    const { A, make, link, linkList, heading } = h;
    const pane = [...heading("Architecture", "DiscordStudyBot")];
    pane.push(make("div", "doc", "Every box is a real folder, file, class or function, read from the code by " +
      "tools/architecture.py. Click a box (or search above) to go deeper: the system, a module, a file (its classes " +
      "drawn around their methods), and one function, whose code shows here. A click keeps your zoom."));
    pane.push(make("p", "meta", h.counts()));
    pane.push(make("h3", "", "Read first"));
    pane.push(linkList([["How a mod goes in", link(GUIDE)],
                        ["Core architecture (the rules)", h.github("docs/core/ARCHITECTURE.md")],
                        ["Writing a module", h.github("docs/core/EXTENSIONS.md")]]));
    for (const slot of Object.keys(SLOT)) {
      const modules = Object.entries(A.modules).filter(([, module]) => module.slots.includes(slot));
      pane.push(make("h3", "", SLOT[slot]), linkList(modules.map(([folder, module]) => [folder, link(folder), module.description])));
    }
    return pane;
  }

  // ---- the guide: how a mod (a module) goes in ------------------------------------------------------------
  function guideChart(h) {
    const { link, fileLink, symbolLink } = h;
    const chart = new h.Chart("DOWN", { overview: true });
    const example = smallest(h, "capabilities");
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
    const setup = chart.node("setup(context)", { kind: "function", sub: "name · config · data_dir · log → Contributions",
                                                 target: symbolLink("core/loader.py", "load_extension") });
    const slots = chart.group("Contributions, wired in by the core", () => {
      chart.node("interfaces", { kind: "gateway", sub: "Interface slot: start(port), deliver(reply)", target: fileLink("contracts/interface.py") });
      chart.node("engines", { kind: "manager", sub: "Engine slot: turns through a Session", target: fileLink("contracts/engine.py") });
      chart.node("capabilities", { kind: "providers", sub: "Capability slot: one action, a ToolResult", target: fileLink("contracts/capability.py") });
      chart.node("personas · profiles · providers · prompt parts", { kind: "contracts", sub: "who answers, on which models",
                                                                     target: symbolLink("contracts/extension.py", "Contributions") });
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

  function guidePane(h) {
    const { A, files, make, link, linkList, heading, code } = h;
    const pane = [...heading("Guide", "How a mod goes in")];
    pane.push(make("div", "doc", "A mod is a module: one folder with an extension.py. The core loads every module at " +
      "startup and wires in what its setup returns, so adding one changes nothing else in the code. The boxes " +
      "open the real code; the full reference is “Writing modules” (docs/core/EXTENSIONS.md)."));
    const steps = [
      ["1. Make a folder", "Built-in modules live in extensions/<name>/ and ship with the bot. Your own go in " +
        "plugins/<name>/ (git-ignored, apart from the tracked examples), or in any folder listed under modules.paths " +
        "in core.yml. The name is lowercase letters, digits and _, unique across the modules."],
      ["2. Declare it in extension.py", "EXTENSION = Extension(\"my_module\", setup, contracts=\"1\", version=\"1\", " +
        "description=\"…\", settings=(…)). contracts is the major version of the contracts it targets; a mismatch " +
        "refuses to load. The folder's other files are imported with relative imports (from .engine import MyEngine)."],
      ["3. Return what it adds from setup(context)", "setup gets context.name, context.config (its config.yml, with " +
        "core.yml's extension_config over it), context.data_dir (a private folder) and context.log(event, **fields). " +
        "It returns Contributions(...): interfaces (only they know a platform), engines (a turn loop through Session " +
        "handles), capabilities (one action each, returning a ToolResult), personas, model profiles and providers, " +
        "prompt parts, and assignments, admission and permissions for its own addresses. It runs once, at startup: " +
        "network loops start in Interface.start, not in setup."],
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
    ];
    for (const [title, text] of steps) pane.push(make("h3", "", title), make("div", "doc", text));
    pane.push(make("h3", "", "Examples to copy, smallest first"));
    const examples = Object.keys(SLOT).map((slot) => [slot, smallest(h, slot)]).filter(([, folder]) => folder);
    pane.push(linkList([...examples.map(([slot, folder]) => [folder, link(folder), `${SLOT[slot]} · ${A.modules[folder].description}`]),
      ...(A.modules["plugins/whale_chan"] ? [["plugins/whale_chan", link("plugins/whale_chan"), "a complete drop-in: her own Discord bot, engine and persona"]] : [])]));
    pane.push(make("h3", "", "Read more"));
    pane.push(linkList([["Writing modules: the full reference", h.github("docs/core/EXTENSIONS.md")],
                        ["Core architecture: the rules", h.github("docs/core/ARCHITECTURE.md")]]));
    const example = smallest(h, "capabilities");
    if (example && files[`${example}/extension.py`]) {
      pane.push(make("h3", "", `The smallest real module: ${example}/extension.py`), code(`${example}/extension.py`, 1, files[`${example}/extension.py`].lines));
    }
    return pane;
  }

  // ---- a module among the folders --------------------------------------------------------------------------
  function folderBox(h, folder) {  // a module's box in a folder chart: the slots it fills
    const module = h.A.modules[folder];
    if (!module) return null;
    return { kind: "module", tip: module.description, sub: module.slots.map((slot) => slot.replace(/s$/, "")).join(" · ") || "module" };
  }
  function folderPane(h, folder) {  // what a module's pane says first: its description and its slots
    const module = h.A.modules[folder];
    if (!module) return null;
    const chips = h.make("div", "chips");
    for (const slot of module.slots) chips.append(h.make("span", "chip", SLOT[slot]));
    return { kind: "Module", nodes: [h.make("p", "doc", module.description), chips] };
  }

  return {
    name: "DiscordStudyBot",
    top: { chart: topChart, pane: topPane, title: "The whole system",
           hint: "The core in the middle, the modules in its three slots. Click a box to go in." },
    guides: [{ path: GUIDE, label: "How a mod goes in", keywords: "how a mod goes in module plugin extension guide",
               chart: guideChart, pane: guidePane, title: "How a mod goes in",
               hint: "From one folder to the core's slots. Click a box for the real code; the steps are in the details." }],
    // Folders whose files are grouped two levels deep when another folder imports them (core/manager, extensions/web).
    deep: ["core", "extensions", "plugins"],
    folderBox, folderPane,
  };
})();
