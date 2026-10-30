"use strict";
// What is DiscordStudyBot's own on the architecture page: the top map (the core, and the modules in the slots they
// fill), the guides "The three layers", "A student's personality" and "How a mod goes in", and how a module looks among
// the folders. app.js, diagram.js and style.css are
// the same viewer in every repository that has one (mneme-llm's too); this file is what differs.
window.SITE = (() => {
  const SLOT = { interfaces: "Interface slot", engines: "Engine slot", capabilities: "Capability slot", personas: "Persona slot",
                 watchers: "Watcher slot" };
  const CORE_KIND = { GW: "gateway", MG: "manager", PR: "providers", BOT: "store", TANK: "store", START: "startup" };
  const GUIDE = "guide/mods";
  const LAYERS = "guide/layers";
  const PERSONALITY = "guide/personality";

  // A module's description, then what it declares it builds on (contracts 1.10) and the names it had (1.11).
  function moduleTip(h, folder) {
    const module = h.A.modules[folder];
    const declared = [["requires", module.requires], ["uses", module.uses], ["formerly", module.formerly]]
      .filter(([, names]) => names && names.length).map(([key, names]) => `${key} ${names.join(", ")}`);
    return [module.description, ...declared].filter(Boolean).join(" · ");
  }
  const sizeOf = (h, folder) => h.filesUnder(folder).reduce((sum, path) => sum + h.files[path].lines, 0);
  const smallest = (h, slot) => Object.keys(h.A.modules).filter((folder) => h.A.modules[folder].slots.includes(slot))
    .sort((a, b) => sizeOf(h, a) - sizeOf(h, b))[0];

  function topChart(h) {
    const { A, files, link, plural, firstLine, filesUnder } = h;
    const chart = new h.Chart("DOWN", { overview: true });
    const supervisor = chart.node("Supervisor", { kind: "supervisor", sub: "parent process · heartbeat · restart",
                                                  target: link("core/supervisor.py"), tip: firstLine(files["core/supervisor.py"]?.doc) });
    const inSlot = (key) => Object.keys(A.modules).filter((folder) => A.modules[folder].slots.includes(key));
    const module = (folder) => chart.node(folder.split("/").pop(), { kind: "module", target: link(folder), tip: moduleTip(h, folder),
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
    chart.node("The three layers", { kind: "guide", sub: "interface → engine → agent or student", target: link(LAYERS) });
    chart.node("A student's personality", { kind: "guide", sub: "who she becomes, kept apart from what she remembers",
                                            target: link(PERSONALITY) });
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

  function topPane(h) {
    const { A, make, link, linkList, heading } = h;
    const pane = [...heading("Architecture", "DiscordStudyBot")];
    pane.push(make("div", "doc", "Every box is a real folder, file, class or function, read from the code by " +
      "tools/architecture.py. Click a box (or search above) to go deeper: the system, a module, a file (its classes " +
      "drawn around their methods), and one function, whose code shows here. A click keeps your zoom."));
    pane.push(make("p", "meta", h.counts()));
    pane.push(make("h3", "", "Read first"));
    pane.push(linkList([["The three layers: interface, engine, agent or student", link(LAYERS)],
                        ["How a mod goes in", link(GUIDE)],
                        ["Design: students, agents, harnesses", h.github("docs/DESIGN.md")],
                        ["Core architecture (the rules)", h.github("docs/core/ARCHITECTURE.md")],
                        ["Writing a module", h.github("docs/core/EXTENSIONS.md")]]));
    for (const slot of Object.keys(SLOT)) {
      const modules = Object.entries(A.modules).filter(([, module]) => module.slots.includes(slot));
      pane.push(make("h3", "", SLOT[slot]), linkList(modules.map(([folder, module]) => [folder, link(folder), module.description])));
    }
    return pane;
  }

  // ---- the guide: the three layers (docs/DESIGN.md), beside ChatGPT's ----------------------------------------------
  function layersChart(h) {
    const { A, link, fileLink } = h;
    const chart = new h.Chart("DOWN", { overview: true });
    const has = (folder) => Boolean(A.modules[folder]);
    const mod = (folder, label, sub) => chart.node(label, { kind: "module", sub, target: has(folder) ? link(folder) : "",
                                                            tip: has(folder) ? moduleTip(h, folder) : "" });
    // ChatGPT beside it, a column of its own: each part is one of the layers
    const page = chart.node("ChatGPT: chatgpt.com", { kind: "external", sub: "a web page = an interface" });
    const harness = chart.node("ChatGPT: its harness", { kind: "external", sub: "tools, memory, turns = an engine" });
    const model = chart.node("ChatGPT: a GPT model", { kind: "external", sub: "the model = an agent" });
    const interfaces = chart.group("Interfaces: frontends (render, collect; never decide a reply)", () => {
      mod("plugins/webui", "web UI", "Cafe · MomoChat · Students · You");
      mod("extensions/discord", "discord", "a Discord bot");
      mod("extensions/cli", "cli", "a terminal chat");
      mod("plugins/whale_chan", "whale_chan", "DeepSeek's own bot, her voice");
      mod("plugins/wechat", "wechat", "WeChat");
      mod("extensions/chimera", "Classroom page", "sessions · Logbook · club");
    }, { kind: "interfaces", pack: true });
    const engines = chart.group("Engines: harnesses (what speakers see, tools, who speaks, when a turn ends)", () => {
      mod("extensions/momochat", "momochat", "converses: direct and group chats");
      mod("extensions/chimera", "chimera", "works: routes, review, tests, approvals");
      mod("extensions/terminal", "terminal", "the creator's shell, no model");
    }, { kind: "engines", pack: true });
    const people = chart.group("Agents and students: a persona with her models", () => {
      chart.node("agents", { kind: "module", sub: "core.yml agents: · a module's personas · no memory" });
      mod("extensions/students", "students", "GPT · Gemini · Claude · DeepSeek · Grok: agents with their memory, always");
      chart.node("her memory", { kind: "store", sub: "one mneme-llm file each: notes · mood · bond · people",
                                 target: fileLink("extensions/students/memory.py") });
    }, { kind: "personas", pack: true });
    const core = chart.group("The core, under every layer", () => {
      chart.node("models", { kind: "providers", sub: "profiles · fallback · adapters", target: link("core/providers") });
      chart.node("prices", { kind: "providers", sub: "every answer priced", target: fileLink("core/providers/prices.py") });
      chart.node("stamina", { kind: "manager", sub: "a day's allowance each, everyone's total", target: fileLink("core/manager/stamina.py") });
    }, { kind: "core", pack: true });
    chart.edge(page, harness, { dashed: true });
    chart.edge(harness, model, { dashed: true });
    chart.edge(interfaces, engines, { arrow: "both", label: "Request / Reply (through the gateway)" });
    chart.edge(engines, people, { label: "a turn for one of them" });
    chart.edge(people, core, { label: "her model, priced, within her stamina" });
    return chart;
  }

  function layersPane(h) {
    const { make, link, linkList, heading } = h;
    const pane = [...heading("Guide", "The three layers")];
    pane.push(make("div", "doc", "ChatGPT is a web page, a harness and a model. Here it is an interface, an engine " +
      "and an agent (or a student). A model alone does nothing until a harness runs the conversation; the harness is " +
      "an engine module, the page is an interface module, and who answers is an agent: a persona with her models. " +
      "The full record is docs/DESIGN.md."));
    const steps = [
      ["Interface", "A frontend: renders replies (a student's voice included) and collects messages. One module per " +
        "platform or page: the web UI (Cafe, MomoChat, Students, your page), Discord, the cli, DeepSeek's own bot, " +
        "WeChat, the Classroom page. An interface never decides a reply and never calls a capability; it loses a tab " +
        "when an engine is missing, and never stops the core."],
      ["Engine", "A harness: what the speakers see, which tools they get, who speaks, when a turn ends. momochat " +
        "converses (direct and group chats; a speaker can pass a classmate a task with a tool) and chimera works (real " +
        "coding: routes, review, tests, two approvals, all the students together). One engine per job; engines keep " +
        "no memory of people. Anyone may bring a harness: an engine module that declares what it needs."],
      ["Agent", "A persona (name, prompt, pictures, a one-line description) with her model profile: how people usually " +
        "define an agent. core.yml agents: gives your own; a module may add its own. An agent remembers no one."],
      ["Student", "A special agent, one of the five, with her memory always: one mneme-llm file each (notes, mood, " +
        "bond, the people she knows, what you call her and what she calls you), and her model tiers (own, strong). " +
        "The students module adds the memory through the core's hooks: a prompt part each turn, a watcher after each " +
        "message, facts for the pages. So every engine reaches the same student and the same memory."],
      ["Declared dependencies", "Every module declares what it requires (left out without it, saying why) and what " +
        "it uses (a feature turns off without it). The core starts with no module at all; its $ commands still work."],
      ["Former names", "A renamed module or engine keeps its data and settings: momochat was assistant and room, " +
        "chimera was workbench, the students' memory was the companion. A core.yml line naming the old name keeps " +
        "working, with a warning."],
      ["Under every layer: models, prices, stamina", "The core prices every answer (core/providers/prices.py, from " +
        "Artificial Analysis or core.yml prices:) and counts it against the speaker's daily allowance and everyone's " +
        "total for the day (core/manager/stamina.py). At a limit the work stops and asks you; $stamina shows and sets " +
        "the limits, and the pages' header shows the day."],
    ];
    for (const [title, text] of steps) pane.push(make("h3", "", title), make("div", "doc", text));
    pane.push(make("h3", "", "Read more"));
    pane.push(linkList([["Design: students, agents, harnesses", h.github("docs/DESIGN.md")],
                        ["momochat", link("extensions/momochat")], ["chimera", link("extensions/chimera")],
                        ["students", link("extensions/students")], ["web UI", link("plugins/webui")]]));
    return pane;
  }

  // ---- the guide: a student's personality, apart from her memory (docs/DESIGN.md) -----------------------------------
  function personalityChart(h) {
    const { fileLink, link } = h;
    const chart = new h.Chart("DOWN", { overview: true });
    const talk = chart.node("What you say", { kind: "external", sub: "every message, on any interface" });
    const teachers = chart.group("Who teaches her", () => {
      chart.node("Luna", { kind: "providers", sub: "built: judges each exchange, a baseline",
                           target: fileLink("extensions/students/memory.py") });
      chart.node("you: 👍 / 👎", { kind: "external", sub: "assisted training: what is right for you",
                                  target: fileLink("plugins/whale_chan/bot.py") });
    }, { kind: "capabilities", pack: true });
    const memory = chart.group("Memory: what happened (large, looked up when it matters)", () => {
      chart.node("notes", { kind: "store", sub: "built: facts, plans, impressions", target: fileLink("extensions/students/memory.py") });
      chart.node("what was said", { kind: "store", sub: "built: every exchange, word for word" });
    }, { kind: "personas", pack: true });
    const personality = chart.group("Personality: who she has become (small, always there)", () => {
      chart.node("reactions", { kind: "module", sub: "built: a local model of how things move her" });
      chart.node("habits", { kind: "external", sub: "planned: how she talks, measured" });
      chart.node("ways of thinking", { kind: "external", sub: "planned: if this, then she…" });
      chart.node("her best replies", { kind: "external", sub: "planned: word for word" });
    }, { kind: "engines", pack: true });
    const character = chart.node("Her character", { kind: "module", sub: "who she is to everyone: student.yml",
                                                    target: link("extensions/students") });
    const turn = chart.node("One turn's prompt", { kind: "manager", sub: "stays the same size, however long you talk" });
    const model = chart.node("Any model", { kind: "providers", sub: "DeepSeek today; swap it and she stays", target: link("core/providers") });
    chart.edge(talk, memory, { label: "kept" });
    chart.edge(talk, teachers, { label: "judged" });
    chart.edge(teachers, personality, { label: "trains" });
    chart.edge(memory, turn, { dashed: true, label: "only what bears on this message" });
    chart.edge(personality, turn, { label: "always" });
    chart.edge(character, turn, { label: "always" });
    chart.edge(turn, model, { label: "she answers" });
    return chart;
  }

  // What is left of her after the transplant: your goal, as something to try.
  function transplant(h) {
    const { make } = h;
    const parts = [["Her character", "stays", "stays"], ["Reactions, habits, ways of thinking, her best replies", "stays", "stays"],
                   ["What she knows about you", "gone", "stays"], ["The way the model itself tends to write", "stays", "changes"]];
    const box = make("div", "doc");
    const toggles = {};
    const result = make("ul", "");
    const draw = () => {
      result.replaceChildren(...parts.map(([name, wiped, swapped]) => {
        const state = toggles.memory.checked && wiped === "gone" ? "gone" : toggles.model.checked && swapped === "changes"
          ? "changes: her examples and habits pull it back" : "stays";
        return make("li", "", `${name}: ${state}`);
      }));
      verdict.textContent = toggles.memory.checked || toggles.model.checked
        ? "The goal: you would still recognise her. She may not know your cat's name, but she would tease you the same way."
        : "Today's companion, whole.";
    };
    const verdict = make("p", "");
    for (const [key, label] of [["memory", "Wipe her memory"], ["model", "Swap her model"]]) {
      const row = make("label", "");
      row.style.display = "block";
      row.style.padding = "6px 0";
      toggles[key] = make("input", "");
      toggles[key].type = "checkbox";
      toggles[key].addEventListener("change", draw);
      row.append(toggles[key], " " + label);
      box.append(row);
    }
    box.append(result, verdict);
    draw();
    return box;
  }

  function personalityPane(h) {
    const { make, link, linkList, heading } = h;
    const pane = [...heading("Guide", "A student's personality")];
    pane.push(make("div", "doc", "A companion should be someone, not a model reading a long file. So a student is kept " +
      "in separate parts: her character (who she is to everyone), her personality (who she has become with you) and her " +
      "memory (what happened). Personality is the part we want to keep small and carry anywhere."));
    const sections = [
      ["The context issue", "A conversation can go on forever; a model's context cannot, and each extra token costs money " +
        "and time. If everything she is had to fit in the prompt, a long friendship would slowly break her. So only two " +
        "things are always there: her character and her personality, both small. Memory is large and stays outside: each " +
        "message brings up only what bears on it, and what has faded leaves the context but can still be searched. In a " +
        "simulated month of daily chats the prompt levelled off at about 8,600 tokens, and replies stayed around 2 seconds."],
      ["Memory is not personality", "Memory answers \"what is your cat called?\". Personality answers \"how would she " +
        "tease you about the cat?\". Delete the first and she forgets facts. Delete the second and a stranger is wearing " +
        "her name. Most systems store only the first and hope the second follows from it."],
      ["How she learns: Luna first", "After each exchange a small set of questions is asked: how did this move her, what " +
        "does she feel, was it worth remembering? GPT-6 Luna's Decisions API answers them in about 0.3 seconds for a " +
        "tiny cost. A local model on your own machine watches every answer and learns from it, and once its guesses agree " +
        "with Luna's it answers by itself. This is pre-training: it gives her a sensible baseline. But a baseline is what " +
        "anyone would feel, so it is not yet hers."],
      ["👍 and 👎: assisted training", "Your reactions are what make her yours. A 👍 or 👎 on her message is a label from " +
        "the only judge who counts: you. Luna says what is reasonable; you say what is right for you. Marked examples " +
        "outweigh Luna's, so the local model drifts from the baseline toward your companion. Today this files her voice " +
        "messages with their text. Next it should cover every reply, and feed the parts below."],
      ["Learning from your mood", "She should feel with you. If you say you got promoted, or \"this is worth celebrating, " +
        "we should be happy!\", she should be happy, and remember that this is the kind of thing that makes her happy. " +
        "Two things are different here. Catching your mood is a moment: her mood moves toward yours, more the closer you " +
        "are. Learning from it is lasting: the moment becomes a lesson for the local model, so next time she reacts that " +
        "way without being told. Joy she shares; when you are low she does not sink with you, she worries for you, " +
        "because a companion who only mirrors makes things worse."],
      ["Habits", "How she talks should be measured, not described. If she has picked up saying 喵 at the end of sentences, " +
        "that is a number: how often, where in the sentence, in what mood. Counted from her own replies, it costs no model " +
        "call, it goes into her prompt as one short line, and it can be checked: if she drifts, the count shows it."],
      ["Ways of thinking", "Some of what you shape is not speech but attitude. Chinese internet has a word for one: 绿茶, " +
        "or 小绿茶, close to a pick-me girl with a sweet, innocent act. Adjectives do not carry that across models. " +
        "\"Pick-me\" means little to a model; what carries is behaviour: \"when you mention another girl, she acts " +
        "unbothered, then lets slip that she would never do that to you.\" So ways of thinking are kept as a short list of " +
        "if-then rules, each with the exchanges that show it, stronger the more you reinforce it, fading when you do not."],
      ["Her best replies", "A description of a voice is weaker than the voice. The replies you marked 👍 are kept word for " +
        "word, and each turn shows the two or three closest to the moment. Changing to a new model, this is what pulls it " +
        "back to her: every model has a default manner, and examples are the strongest thing against it."],
      ["Unique to each person", "Her character is the same for everyone. Personality is kept per person: with you she may " +
        "have learned to be a little pick-me and say 喵; with someone else she has not. Nobody but you and those close to " +
        "you can shape it, so a stranger in a group chat cannot teach her anything."],
    ];
    for (const [title, text] of sections) pane.push(make("h3", "", title), make("div", "doc", text));
    pane.push(make("h3", "", "The goal: try the transplant"), make("div", "doc", "Remove her memory, replace the model, and " +
      "from personality alone you should get a very similar companion out of the box. That is also how we will measure " +
      "it: the same messages to the original and to the transplant, and you judge which is which."), transplant(h));
    pane.push(make("h3", "", "What exists today"));
    const status = make("ul", "doc");
    for (const line of ["Built: memory, the first look and search, moods, closeness, voice clips marked with 👍 / 👎.",
                        "Built, and on since 2026-10-30: Luna's judgments, and the local model learning from them.",
                        "Planned: 👍 / 👎 on every reply, marked examples outweighing Luna's, catching your mood, " +
                        "habits, ways of thinking, her best replies, a personality you can export, the transplant test."]) {
      status.append(make("li", "", line));
    }
    pane.push(status, make("h3", "", "Read more"));
    pane.push(linkList([["Design: students, agents, harnesses", h.github("docs/DESIGN.md")],
                        ["Her memory: memory.py", h.fileLink("extensions/students/memory.py")],
                        ["Her voice and the marks: bot.py", h.fileLink("plugins/whale_chan/bot.py")],
                        ["students", link("extensions/students")]]));
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
        "description=\"…\", settings=(…), requires=(…), uses=(…)). contracts is the major version of the contracts it " +
        "targets; a mismatch refuses to load. requires and uses name the modules it builds on (a package as " +
        "package:<name>): without one it requires it is left out, and says why. The folder's other files are imported " +
        "with relative imports (from .engine import MyEngine)."],
      ["3. Return what it adds from setup(context)", "setup gets context.name, context.config (its config.yml, with " +
        "core.yml's extension_config over it), context.data_dir (a private folder), context.facts (what it publishes " +
        "for pages), context.present (which modules it uses are loaded), context.personas() (the agents loaded before " +
        "it) and context.log(event, **fields). It returns Contributions(...): interfaces (only they know a platform), " +
        "engines (a turn loop through Session handles), capabilities (one action each, returning a ToolResult), " +
        "personas (agents), model profiles and providers, prompt parts, watchers, and assignments, admission and " +
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
      ["An agent or a new student needs no code", "An agent is a persona with her models: give it in core.yml under " +
        "agents: (a prompt and models: in one entry). A student is an agent with her memory: add a folder to " +
        "extensions/students/ with a student.yml (name, display_name, description, models, the prompt files in order; " +
        "aliases, strong models, pictures and formerly if she has them) and her prompt.md. The students module reads " +
        "every such folder at startup; her prompt is the files' lines joined with nothing between them. See " +
        "extensions/students/README.md."],
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
    const examples = Object.keys(SLOT).map((slot) => [slot, smallest(h, slot)]).filter(([, folder]) => folder);
    pane.push(linkList([...examples.map(([slot, folder]) => [folder, link(folder), `${SLOT[slot]} · ${A.modules[folder].description}`]),
      ...(A.modules["plugins/whale_chan"] ? [["plugins/whale_chan", link("plugins/whale_chan"), "a complete drop-in: her own Discord bot and voice, on MomoChat"]] : [])]));
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
    return { kind: "module", tip: moduleTip(h, folder), sub: module.slots.map((slot) => slot.replace(/s$/, "")).join(" · ") || "module" };
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
           hint: "The core in the middle, the modules around it in the slots they fill. Click a box to go in." },
    guides: [{ path: GUIDE, label: "How a mod goes in", keywords: "how a mod goes in module plugin extension guide",
               chart: guideChart, pane: guidePane, title: "How a mod goes in",
               hint: "From one folder to the core's slots. Click a box for the real code; the steps are in the details." },
             { path: LAYERS, label: "The three layers", keywords: "three layers interface engine harness agent student " +
               "memory chatgpt design momochat chimera stamina", chart: layersChart, pane: layersPane,
               title: "The three layers", hint: "Interface → engine → agent or student, beside ChatGPT's page → " +
               "harness → model. Click a box for the module." },
             { path: PERSONALITY, label: "A student's personality", keywords: "personality persona memory habits training " +
               "thumbs luna decisions mood transplant context companion voice", chart: personalityChart, pane: personalityPane,
               title: "A student's personality", hint: "Memory is what happened, and stays outside. Personality is who she " +
               "has become, and is always there. The details have the plan, and a transplant to try." }],
    // Folders whose files are grouped two levels deep when another folder imports them (core/manager, extensions/web).
    deep: ["core", "extensions", "plugins"],
    folderBox, folderPane,
  };
})();
