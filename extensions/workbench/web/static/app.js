"use strict";
/* The workbench page. The server sends `hello` with everything, then live `task`, `step`, `message` and `progress`
   events; a task's full steps and messages come from api/tasks/<id>, its diff from api/tasks/<id>/diff. What you
   type goes back as `start` (a new task) or `send` (to an open one); buttons send their `$command`. */
(() => {
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const PEOPLE = {
    luna: { accent: "#c9d2de", ink: "#6f7c8e" }, gemini: { accent: "#9a86f2", ink: "#7a63de" },
    claude: { accent: "#e5896a", ink: "#c4694a" }, deepseek: { accent: "#4d8ff0", ink: "#3570d2" },
    flash: { accent: "#3fb8e8", ink: "#1b88b8" }, pro: { accent: "#5868d8", ink: "#3f4cb8" },
    grok: { accent: "#e2b545", ink: "#a8801c" }, you: { accent: "#f0a04b", ink: "#b8741f" },
    bot: { accent: "#b39c8a", ink: "#8c7a6b" },
  };
  const STATUS = { queued: "Queued", working: "Working", review: "Ready for review", approved: "Checked",
                   landed: "Landed", discarded: "Discarded", failed: "Didn't pass", cancelled: "Cancelled",
                   answered: "Answered", invited: "Invitation" };
  const CLOSED = new Set(["landed", "discarded"]);
  let WORKFLOWS = [  // the server sends the real list (workflows/), with any added ones
    ["auto", "Auto", "The club picks the simplest workflow likely to be enough"],
    ["single", "Single", "One classmate does it (questions are read-only)"],
    ["cascade", "Cascade", "A cheap draft, tests and a quick check; a stronger classmate takes over only if needed"],
    ["critique", "Critique", "A draft, a review by another classmate (another model family where there is one), one revision"],
  ];
  const TOOL_ICONS = { list_files: "🗂", read_file: "📖", search: "🔎", edit_file: "✏️", write_file: "📝",
                       delete_file: "🗑", run: "▶", diff: "Δ", test: "🧪", probe_file: "📝", probe: "⚖" };
  const ROLES = { router: "assigns", drafter: "drafts", judge: "checks", strong: "takes over", critic: "reviews", tester: "tests" };
  const SAMPLE_IDEAS = [
    "Meetings on Sunday are listed first instead of last. Fix it and add a test.",
    "Add a --json flag to the list command that prints the meetings as JSON.",
    "What does next_meeting do when the schedule is empty?",
    "Let a meeting line carry a room, like `Mon 15:30 Chess @ Room 204`, and show it in list.",
  ];
  const store = {
    get: (key, fallback = null) => { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
    set: (key, value) => { try { localStorage.setItem(key, value); } catch { /* private window */ } },
  };
  const state = {
    view: "list", sessionTab: "all",  // the Classroom: its strips ("list") or one session ("task")
    title: "Club Room", user: { name: "You" }, team: [], seats: new Map(), byName: new Map(), roles: {},
    avatars: new Set(), projects: [], tasks: new Map(), current: store.get("workbench.task"), steps: new Map(),
    messages: new Map(), loaded: new Set(), progress: new Map(), unread: new Set(), ws: null, retry: 0, pending: [],
    workflow: store.get("workbench.workflow", "auto"), project: store.get("workbench.project"),
    places: null, links: {}, arrival: new Map(),  // the mini map and where interfaces are; edits to an invitation
    crew: new Set(),  // who comes along on the next new task (nobody picked: the usual roles)
    sent: new Map(),  // what was sent and not acknowledged yet (to give it back if the lab refuses it)
    tab: store.get("workbench.tab", "changes"), diff: null, expanded: new Set(), settings: {},
  };
  const timeline = $("#timeline");
  const input = $("#input");
  const touch = window.matchMedia("(pointer: coarse)").matches;

  // ---- people ---------------------------------------------------------------------------------------
  const keyOf = (text) => String(text || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 40);
  function seatOf(who) {
    if (!who) return null;
    return state.seats.get(who) || state.byName.get(who) || state.byName.get(keyOf(who)) || null;
  }
  function look(key) {
    if (PEOPLE[key]) return PEOPLE[key];
    let hash = 0;
    for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) % 360;
    return { accent: `hsl(${hash} 55% 62%)`, ink: `hsl(${hash} 45% 42%)` };
  }
  function paint(node, key) {
    const colors = look(key);
    node.style.setProperty("--c", colors.accent);
    node.style.setProperty("--ci", colors.ink);
    node.style.setProperty("--cb", colors.accent);
    return node;
  }
  function avatar(who, small = false) {
    const seat = seatOf(who);
    const key = seat ? seat.persona : keyOf(who) || "bot";
    const picture = seat ? seat.avatar || seat.persona : key;
    const node = paint(make("div", small ? "avatar small" : "avatar"), key);
    const name = seat ? seat.name : who === "you" ? state.user.name : String(who || "?");
    if (state.avatars.has(picture)) {
      const image = make("img");
      image.src = `avatars/${picture}`;
      image.alt = "";
      image.onerror = () => { image.remove(); node.textContent = name.charAt(0).toUpperCase(); };
      node.append(image);
    } else {
      node.textContent = name.trim().charAt(0).toUpperCase() || "?";
    }
    return node;
  }
  const nameOf = (who) => seatOf(who)?.name || who || "";

  // ---- formatting -----------------------------------------------------------------------------------
  const clock = (at) => at ? new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const ago = (at) => {
    const seconds = Math.max(0, Date.now() / 1000 - at);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return new Date(at * 1000).toLocaleDateString();
  };
  const tokens = (n) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n || 0);
  const money = (n) => n == null ? "—" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(3)}`;
  const span = (seconds) => seconds < 60 ? `${Math.round(seconds)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  const stepsOf = (task) => state.steps.get(task) || state.steps.set(task, new Map()).get(task);
  const messagesOf = (task) => state.messages.get(task) || state.messages.set(task, new Map()).get(task);
  const currentTask = () => state.tasks.get(state.current);

  // ---- the connection -------------------------------------------------------------------------------
  function connect() {
    const base = location.pathname.replace(/[^/]*$/, "");
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${base}ws`);
    state.ws = ws;
    ws.onopen = () => {
      state.retry = 0;
      setConnection(true);
      for (const event of state.pending.splice(0)) ws.send(JSON.stringify(event));
    };
    ws.onmessage = (message) => { try { handle(JSON.parse(message.data)); } catch (error) { console.error(error); } };
    ws.onclose = () => {
      setConnection(false);
      state.retry = Math.min(10000, (state.retry || 400) * 2);
      setTimeout(connect, state.retry);
    };
  }
  function send(event) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(event));
    else state.pending.push(event);
  }
  function setConnection(online) {
    const node = $("#connection");
    node.textContent = online ? "" : "reconnecting…";
    node.classList.toggle("is-off", !online);
  }
  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "");

  function handle(event) {
    switch (event.type) {
      case "hello": return hello(event.state);
      case "loaded": {
        if (event.missing) { if (state.current === event.id) toast("Could not load that task."); return; }
        const taskId = event.task.id;
        state.tasks.set(taskId, event.task);
        state.steps.set(taskId, new Map(event.steps.map((step) => [step.id, step])));
        state.messages.set(taskId, new Map(event.messages.map((item) => [item.id, item])));
        state.loaded.add(taskId);
        if (state.current === taskId) { renderHeader(); renderComposer(); render(true); if (isOpen("inspector")) renderInspector(); }
        return;
      }
      case "task": return taskChanged(event.task);
      case "places": state.places = event.places; state.links = event.links || {}; renderMap(); renderPlaces(); return;
      case "step": {
        const step = event.step;
        if (!state.loaded.has(step.task)) return;
        stepsOf(step.task).set(step.id, step);
        if (step.task === state.current) { schedule(); if (state.tab === "steps" || state.tab === "plan") renderInspector(); }
        return;
      }
      case "message": case "message_update": {
        const item = event.item;
        if (!state.loaded.has(item.task)) return;
        messagesOf(item.task).set(item.id, item);
        if (item.task === state.current) schedule();
        return;
      }
      case "progress": {
        const runs = state.progress.get(event.task) || new Map();
        runs.set(event.run_id, event);
        state.progress.set(event.task, runs);
        if (event.task === state.current) renderTyping();
        return;
      }
      case "progress_clear":
        state.progress.get(event.task)?.delete(event.run_id);
        if (event.task === state.current) renderTyping();
        return;
      case "projects": state.projects = event.items; renderProjects(); renderComposer(); return;
      case "team":  // who has a key, as the latest run could tell
        state.team = event.team;
        state.seats = new Map(event.team.map((seat) => [seat.persona, seat]));
        renderTeam(); renderComposer(); schedule();
        return;
      case "deleted":
        state.tasks.delete(event.id);
        if (state.current === event.id) open(null);
        renderSidebar();
        return;
      case "ack": {
        const sent = state.sent.get(event.id);
        state.sent.delete(event.id);
        if (event.ok) { if (event.task) open(event.task); return; }
        toast({ invalid: "Pick a project and write something first.",
                not_here: "This task belongs to another conversation; answer it there." }[event.reason] || `Not sent: ${event.reason}`);
        if (sent?.text && !input.value.trim()) { input.value = sent.text; autosize(); }
        if (sent?.accept && sent.accept === state.current) render();
        return;
      }
      case "toast": case "error": return toast(event.text);
      case "folder": {
        const field = $("#create-project [name=path]");
        if (field && !field.value) field.placeholder = event.path;
        return;
      }
    }
  }

  // The task (and tab) a link points at: #t-…, or #t-…/changes. A malformed link (a stray %) is no link.
  function linkedTask() {
    try { return decodeURIComponent(location.hash.slice(1)).split("/"); } catch { return []; }
  }

  function hello(data) {
    state.title = data.title;
    state.user = data.user;
    state.team = data.team;
    state.seats = new Map(data.team.map((seat) => [seat.persona, seat]));
    state.byName = new Map(data.team.flatMap((seat) => [[seat.name, seat], [keyOf(seat.name), seat]]));
    state.roles = data.roles;
    state.avatars = new Set(data.avatars);
    state.projects = data.projects;
    state.settings = data.settings;
    state.prices = data.prices;
    state.places = data.places;
    state.links = data.links || {};
    if (data.workflows) WORKFLOWS = data.workflows.map((item) => [item.name, item.title, item.text]);
    renderMap();
    renderPlaces();
    state.tasks = new Map(data.tasks.map((task) => [task.id, task]));
    state.progress = new Map(Object.entries(data.progress || {}).map(([task, runs]) =>
      [task, new Map(runs.map((run) => [run.run_id, run]))]));
    $("#brand").textContent = data.title;
    const me = $("#me");
    me.replaceChildren(avatar("you", true), make("span", "", state.user.name));
    state.loaded.clear();
    // A link to a session (#t-…, or #t-…/changes to open the inspector on a tab) opens it, #new starts one; otherwise
    // the Classroom shows its sessions.
    const [linked, tab] = linkedTask();
    if (["changes", "steps", "plan"].includes(tab)) { state.tab = tab; setPanel("inspector", true, false); }
    renderSidebar();
    renderProjects();
    renderTeam();
    if (linked && state.tasks.has(linked)) open(linked, false);
    else if (linked === "new") open(null, false);
    else showList();
    showView();
  }

  // ---- the places bar: this page is the Classroom; #students and #logbook are its other views ----------------
  const VIEW_OF = { students: "Students", logbook: "Logbook" };
  const viewOf = () => VIEW_OF[location.hash.slice(1)] || "Classroom";
  function renderPlaces() {
    if (state.places) window.renderPlaces?.($("#places-nav"), state.places, viewOf());
  }
  function showView() {
    const view = viewOf();
    if (view === "Students") { openDialog("team"); refreshPrices(); }
    else if (view === "Logbook") { showList("done"); $("#sessions-search").focus(); }
    renderPlaces();
  }

  // ---- the sessions: strips to choose from, open ones first, who took part faintly on each -----------------
  const GROUPS = { all: "All", working: "Working", waiting: "Waiting for you", done: "Done" };
  const groupOf = (task) => ["queued", "working"].includes(task.status) ? "working"
    : ["review", "approved", "invited", "failed"].includes(task.status) ? "waiting" : "done";
  const statusLine = (task) => task.status === "working" && task.phase ? task.phase
    : task.status === "invited" ? `${task.data?.invited?.by || nameOf(task.data?.invited?.lead) || "A classmate"} is waiting`
    : STATUS[task.status] || task.status;
  function showList(tab) {
    state.view = "list";
    if (tab) state.sessionTab = tab;
    document.body.classList.add("view-list");
    $("#sessions").hidden = false;
    if (!VIEW_OF[location.hash.slice(1)]) history.replaceState(null, "", location.pathname + location.search);
    renderSessions();
    renderPlaces();
  }
  function stripFace(seat) {
    const picture = seat.avatar || seat.persona;
    const face = paint(make("span", "strip-face"), seat.persona);
    if (state.avatars.has(picture)) {
      const image = make("img");
      image.src = `avatars/${picture}`;
      image.alt = "";
      face.append(image);
    } else {
      face.textContent = (seat.name || "?").charAt(0).toUpperCase();
    }
    return face;
  }
  function strip(task, number) {
    const item = make("li");
    const button = make("button", `strip st-${task.status}${state.unread.has(task.id) ? " is-unread" : ""}`);
    button.type = "button";
    const people = (task.people || []).map((name) => state.seats.get(name)).filter(Boolean);
    const art = make("span", "strip-art");
    art.setAttribute("aria-hidden", "true");
    art.append(...people.slice(0, 4).map(stripFace));
    const top = make("span", "strip-top");
    top.append(make("span", `chip st-${task.status}`, statusLine(task)), make("span", "strip-project", `📁 ${task.project}`));
    const totals = task.data?.totals || {};
    const ended = ["queued", "working"].includes(task.status) ? Date.now() / 1000 : task.updated;
    const meta = [task.workflow ? task.workflow[0].toUpperCase() + task.workflow.slice(1) : "",
                  people.map((seat) => seat.name).join(", "),
                  totals.cost != null && totals.calls ? money(totals.cost) : "",
                  totals.calls ? span(Math.max(0, ended - task.created)) : "", ago(task.updated)].filter(Boolean);
    const body = make("span", "strip-body");
    body.append(top, make("span", "strip-title", task.title), make("span", "strip-meta", meta.join(" · ")));
    button.append(art, make("span", "strip-no", String(number).padStart(2, "0")), body, make("span", "strip-go", "›"));
    button.onclick = () => open(task.id);
    item.append(button);
    return item;
  }
  function renderSessions() {
    if (state.view !== "list") return;
    const all = [...state.tasks.values()];
    const numbers = new Map([...all].sort((a, b) => a.created - b.created).map((task, index) => [task.id, index + 1]));
    const counts = { all: all.length, working: 0, waiting: 0, done: 0 };
    for (const task of all) counts[groupOf(task)] += 1;
    $("#session-tabs").replaceChildren(...Object.entries(GROUPS).map(([key, label]) => {
      const tab = make("button", "session-tab");
      tab.type = "button";
      tab.setAttribute("aria-pressed", String(state.sessionTab === key));
      tab.append(document.createTextNode(label), make("span", "session-count", String(counts[key])));
      tab.onclick = () => { state.sessionTab = key; renderSessions(); };
      return tab;
    }));
    const query = $("#sessions-search").value.trim().toLowerCase();
    const shown = all.filter((task) => (state.sessionTab === "all" || groupOf(task) === state.sessionTab)
      && (!query || task.title.toLowerCase().includes(query) || task.project.toLowerCase().includes(query)))
      .sort((a, b) => (groupOf(a) === "done") - (groupOf(b) === "done") || b.updated - a.updated);
    if (!shown.length) {
      const empty = make("li", "strips-empty");
      empty.append(make("b", "", all.length ? "Nothing here." : "No sessions yet."),
                   make("span", "", all.length ? "Try another tab, or other words." : "Start one: pick the work, then the team."));
      $("#strips").replaceChildren(empty);
      return;
    }
    $("#strips").replaceChildren(...shown.map((task) => strip(task, numbers.get(task.id))));
  }
  $("#sessions-search").addEventListener("input", renderSessions);
  $("#add-session").onclick = () => open(null);
  $("#add-fab").onclick = () => open(null);
  $("#back-to-sessions").onclick = () => showList();
  window.addEventListener("hashchange", () => { if (VIEW_OF[location.hash.slice(1)]) showView(); else renderPlaces(); });

  function taskChanged(task) {
    const before = state.tasks.get(task.id);
    state.tasks.set(task.id, task);
    if (task.id !== state.current && before && before.status !== task.status) state.unread.add(task.id);
    if (document.hidden && task.status === "review" && before?.status !== "review") document.title = `● ${state.title}`;
    renderSidebar();
    renderSessions();
    if (task.id === state.current) {
      renderHeader();
      renderComposer();
      schedule();
      const fingerprint = task.data?.change?.fingerprint || "";
      if (state.diff && state.diff.task === task.id && state.diff.fingerprint !== fingerprint) state.diff.stale = true;
      if (isOpen("inspector")) renderInspector();
    }
  }

  // ---- the task list ------------------------------------------------------------------------------
  function renderSidebar() {
    const nav = $("#tasks");
    const filter = $("#filter").value.trim().toLowerCase();
    const tasks = [...state.tasks.values()].filter((task) => !filter || task.title.toLowerCase().includes(filter)
      || task.project.toLowerCase().includes(filter)).sort((a, b) => b.updated - a.updated);
    $("#new-task").classList.toggle("is-current", !state.current);
    if (!tasks.length) {
      nav.replaceChildren(make("div", "tasks-empty", filter ? "No task matches." : "No tasks yet. Start one!"));
      return;
    }
    const groups = new Map();
    for (const task of tasks) {
      const group = task.project || "other";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(task);
    }
    const nodes = [];
    for (const [project, items] of groups) {
      nodes.push(make("div", "tasks-head", project));
      for (const task of items) {
        const row = make("button", "task-row");
        row.type = "button";
        row.classList.toggle("is-current", task.id === state.current);
        row.classList.toggle("is-unread", state.unread.has(task.id));
        const dot = make("span", `status-dot st-${task.status}`);
        dot.title = STATUS[task.status] || task.status;
        const stat = task.data?.change?.stat && task.data.change.stat !== "no changes" ? ` · ${task.data.change.stat}` : "";
        const by = task.data?.invited?.by;
        const phase = task.status === "working" && task.phase ? task.phase
          : task.status === "invited" ? `${by || nameOf(task.data.invited.lead) || "A classmate"} is waiting`
          : STATUS[task.status] || task.status;
        row.append(dot, make("span", "task-row-title", task.title),
                   make("span", "task-row-meta", `${phase}${stat} · ${ago(task.updated)}`));
        row.onclick = () => { open(task.id); closeNarrow(); };
        nodes.push(row);
      }
    }
    nav.replaceChildren(...nodes);
  }

  // ---- opening a task -----------------------------------------------------------------------------
  async function open(taskId, remember = true) {
    state.current = taskId || null;
    state.view = "task";
    document.body.classList.remove("view-list");
    $("#sessions").hidden = true;
    if (remember) store.set("workbench.task", state.current || "");
    history.replaceState(null, "", state.current ? `#${state.current}` : "#new");
    if (taskId) state.unread.delete(taskId);
    state.expanded.clear();
    state.diff = null;
    renderSidebar();
    renderHeader();
    renderComposer();
    renderTyping();
    render();
    // Loaded over the socket, so its steps and messages come in order with the live events: an event sent before
    // the snapshot is in it, one sent after comes after it (none lost while it loads, none applied out of order).
    if (taskId && !state.loaded.has(taskId)) send({ type: "load", task: taskId });
    if (isOpen("inspector")) renderInspector();
    if (!touch) input.focus();
  }

  function renderHeader() {
    const task = currentTask();
    const meta = $("#task-meta");
    $("#task-title").textContent = task ? task.title : "New session";
    if (!task) {
      meta.replaceChildren(make("span", "", "Describe the work, pick a project, and the club takes it from there."));
      return;
    }
    const data = task.data || {};
    const parts = [make("span", "chip", `📁 ${task.project}`)];
    if (task.workflow) parts.push(make("span", "chip wf", task.workflow[0].toUpperCase() + task.workflow.slice(1)));
    const status = make("span", `chip st-${task.status}`, STATUS[task.status] || task.status);
    if (task.status === "working" && task.phase) status.textContent += ` · ${task.phase}`;
    parts.push(status);
    const totals = data.totals || {};
    if (totals.calls) {
      parts.push(make("span", "", `${totals.calls} call${totals.calls === 1 ? "" : "s"} · ${tokens(totals.input_tokens + totals.output_tokens)} tokens`
        + (totals.cost != null ? ` · ${money(totals.cost)}` : "")));
    }
    const end = CLOSED.has(task.status) || ["answered", "failed", "cancelled", "review", "approved"].includes(task.status)
      ? task.updated : Date.now() / 1000;
    parts.push(make("span", "", span(Math.max(0, end - task.created))));
    if (!task.own) parts.push(make("span", "chip", `from ${task.address.split(":")[0]}`));
    const actions = make("span", "");
    const can = new Set(task.actions || []);  // what the server allows now (views.py): the page only renders it
    if (task.own && can.has("discard") && task.status !== "invited" && data.worktree) {
      const discard = make("button", "link", "Discard");
      discard.type = "button";
      discard.onclick = () => { if (confirm("Throw this change set away? Its worktree and branch are removed.")) sendText(`$discard ${task.id}`, "Discard"); };
      actions.append(discard);
    } else if (can.has("forget")) {
      const forget = make("button", "link", "Remove from the list");
      forget.type = "button";
      forget.onclick = () => send({ type: "forget", task: task.id });
      actions.append(forget);
    }
    const origin = data.origin_address;
    if (task.own && origin) {
      if (data.reported && !can.has("report")) {  // (a new outcome can be reported again)
        const back = make("a", "link", `✓ Reported back · open ${placeName(origin)}`);
        back.href = placeUrl(origin);
        actions.append(document.createTextNode(" "), back);
      } else if (data.reporting && !can.has("report")) {
        actions.append(document.createTextNode(" "), make("span", "", `Reporting back to ${placeName(origin)}…`));
      } else if (can.has("report")) {
        const report = make("button", "link", `↩ Report back to ${placeName(origin)}`);
        report.type = "button";
        report.onclick = () => send({ type: "report", task: task.id });
        actions.append(document.createTextNode(" "), report);
      }
    }
    parts.push(actions);
    meta.replaceChildren(...parts);
  }

  // ---- places: this lab and the others that published one (contracts 1.7) ------------------------------
  const placeOf = (address) => (state.places?.others || []).find((place) => place.interface === String(address || "").split(":")[0]);
  const placeName = (address) => placeOf(address) ? `the ${placeOf(address).place.name.toLowerCase()}`
    : String(address || "").startsWith("discord:") ? "Discord" : "where it came from";
  function placeUrl(address) {  // the conversation in its own interface, where it publishes a link
    const [interfaceName, ...rest] = String(address || "").split(":");
    const link = (state.links || {})[interfaceName];
    return link ? `${link.replace(/\/$/, "")}/#${encodeURIComponent(rest.join(":"))}` : "";
  }

  // The mini map: the places, who is in each (a student is in every place at once), and the corridor
  // with the invitations waiting for you.
  function personFace(person) {
    if (state.seats.has(person.key)) return avatar(person.key, true);
    if (person.avatar && state.avatars.has(person.avatar)) {
      const face = paint(make("div", "avatar small"), person.avatar);
      const image = make("img");
      image.src = `avatars/${person.avatar}`;
      image.alt = "";
      face.append(image);
      return face;
    }
    return avatar(person.name || person.key, true);
  }
  function renderMap() {
    const map = $("#map");
    const places = state.places;
    if (!map || !places) return;
    map.hidden = false;
    const tickets = new Map((places.here.tickets || []).filter((ticket) => ticket.key).map((ticket) => [ticket.key, ticket]));
    const tile = (place, here) => {
      const node = make(here ? "div" : "button", `place${here ? " is-here" : ""}`);
      if (!here) { node.type = "button"; node.title = `Go to ${place.place.name}`; node.onclick = () => { if (place.link) location.href = place.link; }; }
      node.append(make("span", "place-name", `${place.place.icon || "📍"} ${place.place.name}`));
      const faces = make("span", "place-faces");
      const people = place.people || [];  // a student is in every place at once
      for (const person of people.slice(0, 6)) {
        const face = personFace(person);
        face.title = person.title ? `${person.name} · ${person.doing || "working"} on “${person.title}”` : person.name;
        const ticket = here ? null : tickets.get(person.key);
        if (ticket) { face.classList.add("has-ticket"); face.title = `${person.name} has an invitation for you: “${ticket.title}”`; }
        faces.append(face);
      }
      const note = place.place.note || "";
      node.append(faces, make("span", "place-note", here ? `You are here${note && note !== "quiet" ? ` · ${note}` : ""}` : note));
      return node;
    };
    const corridor = make("div", "corridor");
    for (const ticket of (places.here.tickets || []).slice(0, 3)) {  // invitations not followed yet: she waits there
      const door = make("button", "door");
      door.type = "button";
      door.title = `${ticket.by || "A classmate"} invites you: “${ticket.title}”`;
      door.append(make("span", "", "🎟️"), make("span", "", "🚪"));
      door.onclick = () => open(ticket.task);
      corridor.append(door);
    }
    const others = (places.others || []).slice(0, 1).map((place) => tile(place, false));
    map.replaceChildren(...(others.length ? [...others, corridor] : []), tile(places.here, true));
    map.classList.toggle("is-alone", !others.length);
  }

  // ---- the timeline -------------------------------------------------------------------------------
  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; render(); });
  }

  function render(toBottom = false) {
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 140;
    const task = currentTask();
    if (!task) {
      timeline.replaceChildren(welcome());
      return;
    }
    const nodes = [];
    let previous = null;
    if (task.status === "invited") nodes.push(arrivalCard(task));
    for (const entry of entries(task)) {
      const node = entry.step ? renderStep(entry.step, task) : renderMessage(entry.message, task, previous);
      if (node) nodes.push(node);
      previous = entry.message ? entry.message : null;
    }
    if (!state.loaded.has(task.id)) nodes.push(make("div", "pill", "Loading…"));
    const back = backCard(task);
    if (back) nodes.push(back);
    timeline.replaceChildren(...nodes);
    if (toBottom || nearBottom) timeline.scrollTop = timeline.scrollHeight;
  }

  function entries(task) {
    const steps = [...stepsOf(task.id).values()];
    const said = new Set(steps.filter((step) => step.data?.say).map((step) => `${nameOf(step.seat)}\u0000${step.data.say.trim()}`));
    const cards = new Set([...messagesOf(task.id).values()].filter((item) => item.code === "approval_request").map(
      (item) => item.data?.capability));
    const list = [];
    for (const message of messagesOf(task.id).values()) {
      if (message.kind === "text" && message.speaker && said.has(`${message.speaker}\u0000${String(message.text).trim()}`)) continue;
      if (message.kind === "notice" && message.code === "waiting_approval") continue;
      list.push({ at: message.at, order: message.id, message });
    }
    for (const step of steps) {
      if (step.kind === "approval" && (task.own && cards.size)) continue;
      list.push({ at: step.started, order: step.seq, step });
    }
    return list.sort((a, b) => a.at - b.at || a.order - b.order);
  }

  function nameLine(who, role, model, at) {
    const line = make("div", "name");
    line.append(make("span", "", nameOf(who)));
    if (role) line.append(make("span", "role", role));
    if (model) line.append(make("span", "model-tag", model));
    if (at) line.append(make("span", "time", clock(at)));
    return line;
  }

  function bubbleRow(who, text, { role = "", model = "", at = 0, mine = false, followup = false } = {}) {
    const row = make("div", mine ? "row row-mine" : "row");
    if (followup) row.classList.add("is-followup");
    const key = mine ? "you" : (seatOf(who)?.persona || keyOf(who) || "bot");
    paint(row, key);
    const column = make("div", "column");
    column.append(nameLine(mine ? state.user.name : who, role, model, at));
    const bubble = make("div", "bubble markdown");
    window.Markdown.into(bubble, text, state.team.map((seat) => seat.name));
    column.append(bubble);
    row.append(avatar(mine ? "you" : who), column);
    return row;
  }

  function renderMessage(item, task, previous) {
    if (item.kind === "mine") return bubbleRow("you", item.text, { at: item.at, mine: true });
    if (item.kind === "choice") return center(make("div", "pill", `You chose “${item.text}”`));
    if (item.kind === "text") {
      if (!item.speaker) {
        if (item.source === "command" && !item.text.includes("\n") && item.text.length < 120) {
          return center(make("div", "pill", item.text));
        }
        if (item.source === "command") {
          const card = make("div", "card is-quiet");
          const body = make("div", "card-body markdown");
          window.Markdown.into(body, item.text);
          card.append(body);
          return center(card);
        }
        return center(make("div", "pill", item.text));
      }
      const followup = previous && previous.kind === "text" && previous.speaker === item.speaker && item.at - previous.at < 120;
      const model = item.data?.model ? item.data.model : "";
      return bubbleRow(item.speaker, item.text, { at: item.at, model, followup });
    }
    if (item.kind === "file" && item.file) {
      const link = make("a", "attachment-file", `📎 ${item.file.name}`);
      link.href = item.file.url;
      link.target = "_blank";
      return center(link);
    }
    if (item.kind === "notice") {
      if (item.code === "approval_request") return approvalCard(item, task);
      const text = { run_failed: "⚠️ That run stopped unexpectedly; nothing was applied.",
                     unknown_command: "Unknown command." }[item.code] || item.text || item.code;
      return center(make("div", "pill", text));
    }
    if (item.text) return center(make("div", "pill", item.text));
    return null;
  }
  const center = (node) => { const row = make("div", "row row-center"); row.append(node); return row; };

  // Arriving from a chat: what she brought, the brief to start from, where and how.
  function arrivalCard(task) {
    const invited = task.data?.invited || {};
    const seat = seatOf(invited.lead) || seatOf(invited.by);
    const who = invited.by || seat?.name || "A classmate";  // who asked you, even if a sister seat leads here
    const draft = state.arrival.get(task.id) || { text: task.data?.request || "", project: task.project,
                                                  workflow: "auto", crew: new Set(invited.crew || []) };
    state.arrival.set(task.id, draft);
    const card = make("div", "card arrival is-ask");
    const head = make("div", "card-head");
    const face = state.avatars.has(keyOf(who)) ? paint(make("div", "avatar"), keyOf(who)) : avatar(seat?.persona || who);
    if (state.avatars.has(keyOf(who))) {
      const image = make("img");
      image.src = `avatars/${keyOf(who)}`;
      image.alt = "";
      face.append(image);
    }
    head.append(face, make("span", "grow", `${who} brought you to the Classroom`),
                make("span", "chip st-invited", placeName(invited.from)));
    card.append(head, make("div", "card-body", "Nothing has started yet. Check the brief she wrote, pick the " +
      "project and how to work, then start. You can go back to the chat any time."));
    if ((invited.excerpt || []).length) {
      const details = make("details", "excerpt");
      details.append(make("summary", "", `The conversation she brought (${invited.excerpt.length} messages)`));
      for (const item of invited.excerpt) {
        const line = make("div", "excerpt-line");
        line.append(make("b", "", `${item.who}: `), document.createTextNode(item.text));
        details.append(line);
      }
      card.append(details);
    }
    if (invited.notes) {
      card.append(make("div", "section-title", `What ${who} knows about how you like it`));
      const notes = make("div", "card-body markdown");
      window.Markdown.into(notes, invited.notes);
      card.append(notes);
    }
    card.append(make("div", "section-title", "The brief"));
    const brief = make("textarea", "brief");
    brief.rows = 7;
    brief.value = draft.text;
    brief.setAttribute("aria-label", "The brief");
    brief.oninput = () => { draft.text = brief.value; };
    card.append(brief);
    const options = make("div", "arrival-options");
    const label = make("label", "select-label", "Project ");
    const select = make("select");
    for (const project of state.projects) {
      const option = make("option", "", project.source === "sample" ? `${project.name} (practice)` : project.name);
      option.value = project.name;
      option.selected = project.name === draft.project;
      select.append(option);
    }
    if (!draft.project && state.projects.length) draft.project = select.value;
    select.onchange = () => { draft.project = select.value; };
    label.append(select);
    const flows = make("div", "workflows");
    for (const [key, name, text] of WORKFLOWS) {
      const button = make("button", "workflow", name);
      button.type = "button";
      button.title = text;
      button.setAttribute("aria-checked", String(draft.workflow === key));
      button.onclick = () => { draft.workflow = key; for (const other of flows.children) other.setAttribute("aria-checked", String(other === button)); };
      flows.append(button);
    }
    options.append(label, flows);
    card.append(options);
    card.append(make("div", "section-title", `Who comes along with ${seat?.name || who}`),
                crewPicker(draft.crew, seat?.persona));
    const buttons = make("div", "buttons");
    const start = make("button", "action action-primary", "Start the session");
    start.type = "button";
    start.onclick = () => {
      if (!draft.text.trim() || !draft.project) { toast("The brief and a project are needed to start."); return; }
      const id = uid();
      state.sent.set(id, { accept: task.id });
      send({ type: "accept", task: task.id, id, text: draft.text, project: draft.project || select.value,
             workflow: draft.workflow, crew: [...draft.crew] });
      buttons.replaceChildren(make("span", "decided", "Starting…"));
    };
    const later = make("button", "action", "Not now");
    later.type = "button";
    later.onclick = () => sendText(`$discard ${task.id}`, "Not now");
    buttons.append(start, later);
    card.append(buttons);
    return center(card);
  }

  // Done in the Classroom: take the result back to where it came from, where the conversation goes on.
  function backCard(task) {
    const data = task.data || {};
    if (!task.own || !(task.actions || []).includes("report")
        || !["landed", "answered", "failed", "discarded"].includes(task.status)) return null;
    const lead = seatOf(data.invited?.lead) || seatOf(data.invited?.by);
    const card = make("div", "card back");
    const head = make("div", "card-head");
    head.append(lead ? avatar(lead.persona, true) : make("span", "", "🚪"),
                make("span", "grow", `Back to ${placeName(data.origin_address)}?`));
    card.append(head, make("div", "card-body", `${lead ? `${lead.name} will` : "The club will"} tell them there what ` +
      "happened here, so the conversation goes on with it."));
    const buttons = make("div", "buttons");
    const report = make("button", "action action-primary", "Report back");
    report.type = "button";
    report.onclick = () => { send({ type: "report", task: task.id }); buttons.replaceChildren(make("span", "decided", "Sending…")); };
    const stay = make("button", "action", "Stay in the Classroom");
    stay.type = "button";
    stay.onclick = () => card.remove();
    buttons.append(report, stay);
    card.append(buttons);
    return center(card);
  }

  function renderStep(step, task) {
    const data = step.data || {};
    if (step.kind === "route") {
      const card = make("div", "route-card");
      const signals = data.signals || {};
      card.append(make("span", "", `🧭 ${(data.workflow || "").replace(/^./, (c) => c.toUpperCase())}: ${data.why || ""}`));
      for (const value of [signals.kind, signals.size && `${signals.size}`, signals.risk === "high" && "high risk"].filter(Boolean)) {
        card.append(make("span", "chip", value));
      }
      return card;
    }
    if (step.kind === "turn") return renderTurn(step);
    if (step.kind === "validate") {
      const good = step.status === "done";
      const running = step.status === "running";
      const row = make("div", `test-row ${running ? "" : good ? "is-good" : "is-bad"}`);
      row.append(running ? make("span", "spinner") : make("span", "", good ? "✓" : "✗"),
                 make("span", "", `${step.title}${running ? "…" : ""}`));
      if (data.summary && !running) row.append(make("span", "mono", data.summary));
      if (data.seconds != null && !running) row.append(make("span", "time", `${Number(data.seconds).toFixed(1)}s`));
      const wrap = make("div", "");
      wrap.append(row);
      if (data.output) toggled(wrap, row, `test:${step.id}`, () => make("pre", "act-detail", data.output));
      return wrap;
    }
    if (step.kind === "change") {
      const row = make("div", "change-row");
      const stat = data.stat || "";
      row.append(make("span", "", "📝"), make("span", "", step.title.replace(` · ${stat}`, "")), statNode(data));
      row.onclick = () => showChanges();
      return row;
    }
    if (step.kind === "review") return renderReview(step);
    if (step.kind === "requirements") return requirementsCard(data.requirements || [], `📋 ${step.title}`);
    if (step.kind === "approval") {
      const waiting = step.status === "running";
      const where = task.own ? "" : ` (approve in ${task.address.split(":")[0]})`;
      return center(make("div", "pill", waiting ? `⏳ ${step.title}${where}` :
        `${step.status === "done" ? "✓" : "✕"} ${step.title}: ${data.outcome || step.status}`));
    }
    return null;
  }

  function statNode(change) {
    const node = make("span", "");
    if (!change || !change.files || !change.files.length) { node.textContent = "no changes"; return node; }
    node.append(make("span", "", `${change.files.length} file${change.files.length === 1 ? "" : "s"} `),
                make("span", "plus", `+${change.added}`), make("span", "", " "), make("span", "minus", `−${change.removed}`));
    return node;
  }

  function toggled(container, handle, key, build) {
    handle.style.cursor = "pointer";
    const show = () => {
      const detail = build();
      detail.dataset.detail = key;
      container.append(detail);
    };
    if (state.expanded.has(key)) show();
    handle.addEventListener("click", (event) => {
      event.stopPropagation();
      const existing = container.querySelector(`[data-detail="${CSS.escape(key)}"]`);
      if (existing) { existing.remove(); state.expanded.delete(key); }
      else { state.expanded.add(key); show(); }
    });
  }

  function callLabel(step) {
    const calls = step.data?.calls || [];
    if (!calls.length) return "";
    const call = calls[calls.length - 1];
    const used = calls.reduce((sum, item) => sum + item.input_tokens + item.output_tokens, 0);
    const seconds = calls.reduce((sum, item) => sum + item.seconds, 0);
    return `${call.model} · ${tokens(used)} tok · ${seconds.toFixed(1)}s`;
  }

  function renderTurn(step) {
    const data = step.data || {};
    if (step.status !== "running" && !data.say && !data.note && !(data.actions || []).length && data.status !== "blocked") return null;
    const wrap = make("div", "");
    paint(wrap, seatOf(step.seat)?.persona || step.seat);
    const role = ROLES[step.role] || step.role;
    if (data.say) wrap.append(bubbleRow(step.seat, data.say, { role, model: callLabel(step), at: step.started }));
    const block = make("div", "activity");
    const head = make("div", "activity-head");
    if (!data.say) head.append(avatar(step.seat, true), make("b", "", nameOf(step.seat)));
    head.append(make("span", "", `${data.mode === "question" ? "looking it up" : step.title.split(" · ").slice(1).join(" · ") || role}`));
    if (step.status === "running") head.append(make("span", "spinner"));
    if (!data.say && callLabel(step)) head.append(make("span", "model-tag", callLabel(step)));
    block.append(head);
    if (data.note) block.append(make("div", "activity-note", data.note));
    const actions = data.actions || [];
    if (actions.length) {
      const acts = make("div", "acts");
      actions.forEach((action, index) => {
        const chip = make("button", `act ${action.status === "failed" ? "is-failed" : action.status === "waiting" ? "is-waiting" : ""}`);
        chip.type = "button";
        const target = action.arguments?.path || action.arguments?.command || action.arguments?.pattern || "";
        chip.append(make("b", "", TOOL_ICONS[action.tool] || "•"), make("span", "", `${action.tool.replace("_", " ")} ${target}`.trim()));
        const extra = action.data || {};
        if (action.tool === "run" && extra.exit_code != null) chip.append(make("b", "", extra.exit_code === 0 ? "✓" : `✗ ${extra.exit_code}`));
        if (action.tool === "probe" && extra.same != null) chip.append(make("b", "", extra.same ? "old = new" : "old ≠ new"));
        if (action.status === "waiting") chip.append(make("b", "", "⏳"));
        const key = `act:${step.id}:${index}`;
        chip.classList.toggle("is-open", state.expanded.has(key));
        acts.append(chip);
        toggled(block, chip, key, () => make("pre", "act-detail", action.summary || action.error || action.status));
      });
      block.append(acts);
    }
    if (data.status === "blocked" && data.summary) block.append(make("div", "activity-note", `Blocked: ${data.summary}`));
    if (data.mode === "probe" && step.status !== "running" && ["done", "blocked"].includes(data.status)
        && Array.isArray(data.findings)) {  // the tester's verdict, on her last turn
      block.append(make("div", "activity-note", data.findings.length
        ? `Found ${data.findings.length} problem${data.findings.length === 1 ? "" : "s"}:` : "Tried to break it: nothing broke."));
      if (data.findings.length) block.append(findingsList(data.findings));
    }
    wrap.append(block);
    return wrap;
  }

  // The acceptance criteria written down from the request (kernel/verify.py), with what the review found.
  function requirementsCard(criteria, title, verdicts = []) {
    const card = make("div", "card is-quiet");
    card.append(make("div", "card-head", title));
    const list = make("div", "criteria");
    const found = new Map(verdicts.map((item) => [item.id, item]));
    for (const item of criteria) {
      const verdict = found.get(item.id);
      const mark = !verdict ? "•" : verdict.met === "yes" ? "✓" : verdict.met === "no" ? "✗" : "?";
      const row = make("div", `criterion ${verdict ? `is-${verdict.met}` : ""}`);
      row.append(make("b", "", `${mark} ${item.id}`), make("span", "chip", item.kind), make("span", "", item.text));
      if (verdict?.evidence) row.title = verdict.evidence;
      list.append(row);
    }
    card.append(list);
    return center(card);
  }

  function findingsList(findings) {
    const list = make("div", "issues");
    for (const finding of findings) {
      const node = make("div", "issue");
      node.append(make("div", "issue-where", `✗ ${finding.criterion || "found"}`), make("div", "", finding.problem));
      if (finding.evidence) node.append(make("pre", "act-detail", finding.evidence));
      list.append(node);
    }
    return list;
  }

  function renderReview(step) {
    const data = step.data || {};
    const card = make("div", `card ${step.status === "running" ? "" : data.verdict === "approve" ? "is-good" : "is-bad"}`);
    paint(card, seatOf(step.seat)?.persona || step.seat);
    const head = make("div", "card-head");
    const title = data.mode === "judge" ? "checked the draft" : "reviewed the change";
    head.append(avatar(step.seat, true), make("span", "", `${nameOf(step.seat)} ${step.status === "running" ? title.replace("ed", "ing") : title}`));
    head.append(make("span", "grow"));
    if (step.status === "running") head.append(make("span", "spinner"));
    else head.append(make("span", `chip ${data.verdict === "approve" ? "st-landed" : "st-failed"}`,
                          data.verdict === "approve" ? "✓ approve" : "✎ revise"));
    if (callLabel(step)) head.append(make("span", "model-tag", callLabel(step)));
    card.append(head);
    if (data.say) {
      const say = make("div", "card-body markdown");
      window.Markdown.into(say, data.say);
      card.append(say);
    }
    if (data.summary && data.summary !== data.say) {
      const summary = make("div", "card-body markdown");
      window.Markdown.into(summary, data.summary);
      summary.style.color = "var(--muted)";
      card.append(summary);
    }
    for (const item of data.unexplained || []) {  // changed parts no criterion asks for
      card.append(make("div", "card-body issue", `✗ not asked for: ${item.file}: ${item.change}`));
    }
    if ((data.issues || []).length) {
      const list = make("div", "issues");
      for (const issue of data.issues) {
        const node = make("div", "issue");
        const where = make("div", "issue-where");
        where.append(make("span", `sev sev-${issue.severity}`, issue.severity), make("span", "",
          ` ${issue.category ? `${issue.category} · ` : ""}${issue.file || ""}${issue.line ? `:${issue.line}` : ""}`));
        const problem = make("div", "", issue.problem);
        node.append(where, problem);
        if (issue.suggestion) node.append(make("div", "time", `→ ${issue.suggestion}`));
        list.append(node);
      }
      card.append(list);
    }
    return center(card);
  }

  // ---- approvals ----------------------------------------------------------------------------------
  function approvalCard(item, task) {
    const data = item.data || {};
    let args = {};
    try { args = JSON.parse(data.arguments || "{}"); } catch { args = {}; }
    const capability = data.capability || "";
    // The server says which approval the task waits for now (views.py); any other open card is not for now.
    const pending = task.approval && task.approval.id === data.approval_id ? task.approval : null;
    const current = task.data?.change?.fingerprint;
    const stale = !item.decided && !pending && args.fingerprint && current && args.fingerprint !== current;
    const busy = !item.decided && !pending && !stale && capability === "workbench.review" && ["working", "queued"].includes(task.status);
    const closed = !item.decided && !pending && !stale && !busy;
    const card = make("div", `card ${item.decided || stale || closed || busy ? "is-quiet" : "is-ask"}`);
    const head = make("div", "card-head");
    const titles = { "workbench.review": "🔍 Second check: look at everything", "workbench.land": "🛬 Final approval: land it",
                     "workbench.run": "▶ Run a command?" };
    head.append(make("span", "grow", data.kind === "confirm" ? `Confirm: ${titles[capability] || capability}` : titles[capability] || `Approval: ${capability}`),
                make("span", `tier tier-${data.tier}`, data.tier || ""));
    card.append(head);
    const body = make("div", "card-body", data.summary || item.text);
    card.append(body);
    if (capability === "workbench.run" && args.command) card.append(make("div", "command-line", `$ ${args.command}`));
    let checks = null;
    if (capability === "workbench.review" && Array.isArray(args.checklist)) {
      const ticked = new Set((pending?.checklist || []).filter((entry) => entry.ticked).map((entry) => entry.id));
      checks = make("div", "checklist");
      for (const entry of pending?.checklist || args.checklist) {
        const label = make("label", "check");
        const box = make("input");
        box.type = "checkbox";
        box.checked = ticked.has(entry.id) || item.decided === "approved";
        box.disabled = !pending;
        label.classList.toggle("is-checked", box.checked);
        box.onchange = () => {
          send({ type: "tick", task: task.id, fingerprint: args.fingerprint, item: entry.id, checked: box.checked });
          label.classList.toggle("is-checked", box.checked);
          updateApprove();
        };
        label.append(box, make("span", "", entry.label));
        if (entry.id.startsWith("file:")) {
          const look = make("button", "link", "look");
          look.type = "button";
          look.onclick = (event) => { event.preventDefault(); showChanges(entry.id.slice(5)); };
          label.append(look);
        }
        checks.append(label);
      }
      card.append(checks);
      const tools = make("div", "check-tools");
      const all = make("button", "link", "Open all the changes");
      all.type = "button";
      all.onclick = () => showChanges();
      tools.append(all, make("span", "", "· every box must be ticked before you can approve"));
      card.append(tools);
    }
    const buttons = make("div", "buttons");
    const actionButtons = [];
    if (item.decided) {
      buttons.append(make("span", "decided", item.decided === "approved" ? "✓ approved" : "✕ denied"));
    } else if (stale) {
      buttons.append(make("span", "decided", "This was for an older version of the change."));
    } else if (busy) {
      buttons.append(make("span", "decided", "The club is making a new version; it comes with its own check."));
    } else if (closed) {
      buttons.append(make("span", "decided", CLOSED.has(task.status) ? `The task is ${task.status}.`
        : "Not waiting on this one any more (a newer request replaced it)."));
    } else {
      for (const action of item.actions || []) {
        const button = make("button", `action action-${action.style}`, action.label);
        button.type = "button";
        button.onclick = () => {
          sendText(action.command, action.label);
          card.classList.add("is-quiet");
          buttons.replaceChildren(make("span", "decided", "Sending…"));
        };
        actionButtons.push([button, action]);
        buttons.append(button);
      }
    }
    card.append(buttons);
    function updateApprove() {
      if (!checks) return;
      const done = [...checks.querySelectorAll("input")].every((box) => box.checked);
      for (const [button, action] of actionButtons) {
        if (action.style !== "danger") button.disabled = !done;
      }
    }
    updateApprove();
    return center(card);
  }

  function sendText(text, action = "") {
    const task = currentTask();
    if (!task) return;
    send({ type: "send", task: task.id, text, id: uid(), action });
  }

  // ---- the welcome screen -------------------------------------------------------------------------
  function welcome() {
    const node = make("div", "welcome");
    const faces = make("div", "welcome-faces");
    for (const seat of state.team.slice(0, 6)) faces.append(avatar(seat.persona));
    node.append(faces, make("div", "welcome-title", "What should the club work on?"),
                make("div", "welcome-text", "Describe a change or ask a question. The club picks how to work, each task " +
                     "in its own copy of the project, and nothing reaches your code until you have checked it and " +
                     "approved it."));
    const project = selectedProject();
    const ideas = project?.source === "sample" ? SAMPLE_IDEAS : [];
    if (ideas.length) {
      const list = make("div", "suggestions");
      for (const idea of ideas) {
        const button = make("button", "suggestion", idea);
        button.type = "button";
        button.onclick = () => { input.value = idea; autosize(); input.focus(); };
        list.append(button);
      }
      node.append(list);
    }
    const flows = make("div", "flows");
    for (const [key, label, text] of WORKFLOWS.slice(1)) {
      const flow = make("div", "flow");
      flow.append(make("b", "", label), document.createTextNode(text));
      if (key === "cascade") flow.title = "HydraFusion's cascade";
      flows.append(flow);
    }
    node.append(flows);
    return node;
  }

  // ---- composer -----------------------------------------------------------------------------------
  function selectedProject() {
    const names = state.projects.map((project) => project.name);
    if (!state.project || !names.includes(state.project)) {
      state.project = (state.projects.find((project) => project.source !== "sample") || state.projects[0])?.name || null;
    }
    return state.projects.find((project) => project.name === state.project);
  }

  function renderComposer() {
    const task = currentTask();
    const options = $("#composer-options");
    const composer = $("#composer");
    options.hidden = Boolean(task);
    composer.classList.toggle("is-closed", Boolean(task && CLOSED.has(task.status)));
    const hint = $("#composer-hint");
    const sendButton = $("#send");
    input.disabled = false;
    sendButton.disabled = false;
    if (!task) {
      const select = $("#project-select");
      selectedProject();
      select.replaceChildren(...state.projects.map((project) => {
        const option = make("option", "", project.source === "sample" ? `${project.name} (practice)` : project.name);
        option.value = project.name;
        option.selected = project.name === state.project;
        return option;
      }), Object.assign(make("option", "", "＋ Add a project…"), { value: "__add" }));
      const flows = $("#workflows");
      flows.replaceChildren(...WORKFLOWS.map(([key, label, text]) => {
        const button = make("button", "workflow", label);
        button.type = "button";
        button.title = text;
        button.setAttribute("role", "radio");
        button.setAttribute("aria-checked", String(state.workflow === key));
        button.onclick = () => { state.workflow = key; store.set("workbench.workflow", key); renderComposer(); };
        return button;
      }));
      $("#crew").replaceChildren(make("span", "select-label", "Crew"), crewPicker(state.crew, null,
        "Nobody picked: each role takes its usual classmate."));
      input.placeholder = "Describe a task, or ask about the code…";
      hint.textContent = touch ? "" : "Enter starts it · Shift+Enter for a new line";
      return;
    }
    if (task.status === "invited") {
      input.disabled = true;
      sendButton.disabled = true;
      input.placeholder = "Start the invitation above first.";
      hint.textContent = "";
      return;
    }
    if (CLOSED.has(task.status)) {
      input.disabled = true;
      sendButton.disabled = true;
      input.placeholder = `This task is ${task.status}.`;
      hint.textContent = "Start a new task for more work.";
      return;
    }
    input.placeholder = { working: "Add something; the club takes it after the current step…",
                          queued: "Add something; the club takes it after the current step…",
                          review: "Ask for changes, or approve above…", approved: "Ask for changes, or give the final approval above…",
                          answered: "Ask a follow-up, or ask for a change…",
                          failed: "Tell the club what to try, or discard it…",
                          cancelled: "Tell the club to try again, or discard it…" }[task.status] || "Reply…";
    hint.textContent = task.own ? "" : `This task lives in ${task.address.split(":")[0]}; reply there.`;
    if (!task.own) { input.disabled = true; sendButton.disabled = true; }
  }

  // Who comes along: toggles for the classmates (the one who leads is always there; those without a key can't come).
  function crewPicker(selected, lead, hint = "") {
    const row = make("div", "crew-picker");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "Who comes along");
    for (const seat of state.team) {
      const chip = make("button", "crew-chip");
      chip.type = "button";
      const leads = seat.persona === lead;
      const away = seat.ready === false;
      chip.append(avatar(seat.persona, true), make("span", "", seat.name));
      if (leads) chip.append(make("span", "crew-note", "leads"));
      else if (away) chip.append(make("span", "crew-note", "no key"));
      chip.disabled = leads || away;
      chip.title = leads ? `${seat.name} leads this visit` : away ? `${seat.name} has no model key here` : seat.voice;
      chip.setAttribute("aria-pressed", String(leads || selected.has(seat.persona)));
      chip.onclick = () => {
        selected.has(seat.persona) ? selected.delete(seat.persona) : selected.add(seat.persona);
        chip.setAttribute("aria-pressed", String(selected.has(seat.persona)));
      };
      row.append(chip);
    }
    if (hint) row.title = hint;
    return row;
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
  }

  function submit() {
    const text = input.value.trim();
    if (!text) return;
    const task = currentTask();
    if (!task) {
      const project = selectedProject();
      if (!project) { openDialog("projects"); toast("Add a project first."); return; }
      const id = uid();
      state.sent.set(id, { text });
      send({ type: "start", id, project: project.name, workflow: state.workflow, text, crew: [...state.crew] });
    } else {
      if (CLOSED.has(task.status) || !task.own) return;
      const id = uid();
      state.sent.set(id, { text });
      send({ type: "send", task: task.id, id, text });
    }
    input.value = "";
    autosize();
  }

  // ---- typing lines -------------------------------------------------------------------------------
  function renderTyping() {
    const node = $("#typing");
    const runs = [...(state.progress.get(state.current)?.values() || [])].filter((run) => Date.now() / 1000 - run.at < 900);
    node.replaceChildren(...runs.map((run) => {
      const row = make("div", "typing-row");
      const who = state.team.find((seat) => run.text.startsWith(seat.name));
      row.append(who ? avatar(who.persona, true) : make("span", "dots"), make("span", "typing-text", run.text), make("span", "dots"));
      for (const action of run.actions || []) {
        const button = make("button", `action action-${action.style}`, action.label);
        button.type = "button";
        button.onclick = () => sendText(action.command, action.label);
        row.append(button);
      }
      return row;
    }));
  }

  // ---- inspector ----------------------------------------------------------------------------------
  function showChanges(path) {
    state.tab = "changes";
    store.set("workbench.tab", "changes");
    setPanel("inspector", true);
    renderInspector(path);
  }

  async function renderInspector(focus) {
    for (const tab of document.querySelectorAll(".tab")) tab.setAttribute("aria-selected", String(tab.dataset.tab === state.tab));
    const body = $("#inspector-body");
    const task = currentTask();
    if (!task) { body.replaceChildren(make("div", "inspector-empty", "Open a task to see its changes, steps and plan.")); return; }
    if (state.tab === "steps") return body.replaceChildren(ledger(task));
    if (state.tab === "plan") return body.replaceChildren(plan(task));
    const fingerprint = task.data?.change?.fingerprint || "";
    if (!state.diff || state.diff.task !== task.id || state.diff.stale) {
      if (!state.diff || state.diff.task !== task.id) body.replaceChildren(make("div", "inspector-empty", "Loading the changes…"));
      try {
        const response = await fetch(`api/tasks/${encodeURIComponent(task.id)}/diff`);
        const data = await response.json();
        state.diff = { task: task.id, fingerprint, data };
      } catch {
        body.replaceChildren(make("div", "inspector-empty", "Could not load the changes."));
        return;
      }
    }
    if (state.current !== task.id || state.tab !== "changes") return;
    body.replaceChildren(diffView(state.diff.data, task));
    if (focus) document.getElementById(`file-${focus}`)?.scrollIntoView({ block: "start" });
  }

  function diffView(data, task) {
    const wrap = make("div", "");
    const summary = make("div", "diff-summary");
    summary.append(statNode(data), make("span", "grow"));
    summary.append(make("span", "time", data.live ? "live from the task's worktree" : CLOSED.has(task.status) ? `as it was ${task.status}` : ""));
    const refresh = make("button", "link", "refresh");
    refresh.type = "button";
    refresh.onclick = () => { state.diff = null; renderInspector(); };
    summary.append(refresh);
    wrap.append(summary);
    if (!data.files || !data.files.length) {
      wrap.append(make("div", "inspector-empty", task.status === "working" ? "No changes yet." : "No files changed."));
      return wrap;
    }
    const list = make("div", "file-list");
    for (const file of data.files) {
      const item = make("button", "file-item");
      item.type = "button";
      item.append(make("span", `file-status ${file.status}`, file.status), make("span", "path", file.path),
                  make("span", "plus", file.binary ? "bin" : `+${file.added}`), make("span", "minus", file.binary ? "" : `−${file.removed}`));
      item.onclick = () => document.getElementById(`file-${file.path}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
      list.append(item);
    }
    wrap.append(list);
    for (const file of parseDiff(data.patch || "")) {
      const details = make("details", "diff-file");
      details.open = true;
      details.id = `file-${file.path}`;
      const head = make("summary", "");
      const known = data.files.find((item) => item.path === file.path) || {};
      head.append(make("span", `file-status ${known.status || "M"}`, known.status || "M"), make("span", "path", file.oldPath && file.oldPath !== file.path ? `${file.oldPath} → ${file.path}` : file.path));
      if (!known.binary) head.append(make("span", "plus", `+${known.added ?? ""}`), make("span", "minus", `−${known.removed ?? ""}`));
      details.append(head);
      const table = make("table", "diff-table");
      for (const hunk of file.hunks) {
        const row = make("tr", "hunk");
        const cell = make("td", "", hunk.header);
        cell.colSpan = 3;
        row.append(cell);
        table.append(row);
        for (const line of hunk.lines) {
          const tr = make("tr", line.type);
          tr.append(make("td", "ln", line.old ?? ""), make("td", "ln", line.new ?? ""),
                    make("td", "code", (line.type === "add" ? "+" : line.type === "del" ? "−" : " ") + line.text));
          table.append(tr);
        }
      }
      if (!file.hunks.length) {
        const row = make("tr", "hunk");
        const cell = make("td", "", file.note || "(no text changes)");
        cell.colSpan = 3;
        row.append(cell);
        table.append(row);
      }
      details.append(table);
      wrap.append(details);
    }
    if (data.truncated) wrap.append(make("div", "pill", "The diff is long; the rest is cut here."));
    return wrap;
  }

  function parseDiff(patch) {
    const files = [];
    let file = null;
    let hunk = null;
    let oldLine = 0;
    let newLine = 0;
    for (const raw of patch.split("\n")) {
      const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(raw);
      if (header) {
        file = { oldPath: header[1], path: header[2], hunks: [], note: "" };
        files.push(file);
        hunk = null;
        continue;
      }
      if (!file) continue;
      if (raw.startsWith("rename from ")) { file.oldPath = raw.slice(12); continue; }
      if (raw.startsWith("rename to ")) { file.path = raw.slice(10); continue; }
      if (raw.startsWith("Binary files")) { file.note = "Binary file"; continue; }
      const at = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(raw);
      if (at) {
        oldLine = Number(at[1]);
        newLine = Number(at[2]);
        hunk = { header: raw, lines: [] };
        file.hunks.push(hunk);
        continue;
      }
      if (!hunk) continue;
      if (raw.startsWith("+")) hunk.lines.push({ type: "add", new: newLine++, text: raw.slice(1) });
      else if (raw.startsWith("-")) hunk.lines.push({ type: "del", old: oldLine++, text: raw.slice(1) });
      else if (raw.startsWith(" ")) hunk.lines.push({ type: "ctx", old: oldLine++, new: newLine++, text: raw.slice(1) });
      else if (raw.startsWith("\\")) hunk.lines.push({ type: "ctx", text: raw });
    }
    return files;
  }

  function ledger(task) {
    const wrap = make("div", "");
    const table = make("table", "ledger");
    const head = make("tr");
    for (const [label, numeric] of [["Step", false], ["Who", false], ["Model", false], ["In", true], ["Out", true], ["Cost", true], ["Time", true]]) {
      const th = make("th", numeric ? "num" : "", label);
      head.append(th);
    }
    table.append(head);
    let input = 0, output = 0, cost = 0, priced = true, seconds = 0, count = 0;
    for (const step of stepsOf(task.id).values()) {
      for (const call of step.data?.calls || []) {
        count++;
        input += call.input_tokens; output += call.output_tokens; seconds += call.seconds;
        if (call.cost == null) priced = false; else cost += call.cost;
        const row = make("tr");
        row.append(make("td", "", step.title.replace(/^\S+ · /, "") || step.kind), make("td", "", `${nameOf(call.seat)} · ${ROLES[call.role] || call.role}`),
                   make("td", "model", `${call.model}${call.effort ? ` · ${call.effort}` : ""}`), make("td", "num", tokens(call.input_tokens)),
                   make("td", "num", tokens(call.output_tokens)), make("td", "num", money(call.cost)), make("td", "num", `${call.seconds.toFixed(1)}s`));
        const notes = [];
        if (call.price) notes.push(`$${call.price.input} in / $${call.price.output} out per 1M tokens, ` +
          (call.price.source === "config" ? "from your config" : `${call.price.slug} on Artificial Analysis, ${call.price.as_of}`));
        if (call.fallbacks?.length) notes.push(`Fell back: ${call.fallbacks.join("; ")}`);
        if (notes.length) row.title = notes.join("\n");
        table.append(row);
      }
    }
    const total = make("tr", "total");
    total.append(make("td", "", `${count} model call${count === 1 ? "" : "s"}`), make("td", ""), make("td", ""),
                 make("td", "num", tokens(input)), make("td", "num", tokens(output)), make("td", "num", priced && count ? money(cost) : "—"),
                 make("td", "num", `${seconds.toFixed(1)}s`));
    table.append(total);
    wrap.append(table);
    wrap.append(priceNote(!priced && count));
    const tests = [...stepsOf(task.id).values()].filter((step) => step.kind === "validate");
    if (tests.length) {
      wrap.append(make("div", "section-title", "Test runs"));
      const list = make("table", "ledger");
      for (const step of tests) {
        const row = make("tr");
        row.append(make("td", "", step.status === "done" ? "✓" : step.status === "running" ? "…" : "✗"), make("td", "", step.title),
                   make("td", "model", step.data?.summary || ""), make("td", "num", step.data?.seconds != null ? `${Number(step.data.seconds).toFixed(1)}s` : ""));
        list.append(row);
      }
      wrap.append(list);
    }
    return wrap;
  }

  function plan(task) {
    const data = task.data || {};
    const wrap = make("div", "");
    if ((data.plan || []).length) {
      wrap.append(make("div", "section-title", "Plan"));
      const list = make("div", "plan-list");
      for (const item of data.plan) list.append(make("div", `plan-item ${item.done ? "done" : ""}`, `${item.done ? "☑" : "☐"} ${item.step}`));
      wrap.append(list);
    }
    if ((data.requirements || []).length) {  // what "done" means, and what the review and the tester found
      wrap.append(make("div", "section-title", "Acceptance criteria"));
      wrap.append(requirementsCard(data.requirements, "Checked by the review", data.review?.requirements || []));
    }
    const verification = data.verification || {};
    if ((verification.rounds || []).length || verification.note) {
      wrap.append(make("div", "section-title", "The tester"));
      wrap.append(make("div", "card-body", verification.note || `${nameOf(verification.tester)} ran ${verification.probes || 0} probe(s)`
        + (verification.repairs ? `, ${verification.repairs} repair round(s)` : "")
        + ((verification.findings || []).length ? `; still found ${verification.findings.length} problem(s):` : "; nothing broke.")));
      if ((verification.findings || []).length) wrap.append(findingsList(verification.findings));
    }
    const route = data.route || {};
    wrap.append(make("div", "section-title", "How it was routed"));
    const facts = make("dl", "facts");
    const fact = (label, value) => { if (value == null || value === "") return; facts.append(make("dt", "", label)); const dd = make("dd", ""); dd.append(value instanceof Node ? value : document.createTextNode(String(value))); facts.append(dd); };
    fact("Workflow", route.workflow ? `${route.workflow} (${route.why})` : task.workflow);
    fact("Sorted as", [route.kind, route.size, route.risk && `${route.risk} risk`].filter(Boolean).join(" · "));
    fact("By", route.by ? `${nameOf(route.by)}${route.by === "keywords" ? "" : ""}` : "");
    fact("Reason", route.reason);
    wrap.append(facts);
    const who = new Map();
    for (const step of stepsOf(task.id).values()) {
      for (const call of step.data?.calls || []) who.set(`${call.seat}:${call.role}`, call);
    }
    if (who.size) {
      wrap.append(make("div", "section-title", "Who did what"));
      const people = make("dl", "facts");
      for (const call of who.values()) {
        people.append(make("dt", "", nameOf(call.seat)));
        people.append(make("dd", "", `${ROLES[call.role] || call.role} · ${call.provider}/${call.model} (${call.family})`));
      }
      wrap.append(people);
    }
    wrap.append(make("div", "section-title", "Where"));
    const git = make("dl", "facts");
    const code = (text) => { const node = make("code", "", text); return node; };
    git.append(make("dt", "", "Project"), Object.assign(make("dd", ""), { textContent: task.project }));
    if (data.branch) { git.append(make("dt", "", "Branch")); const dd = make("dd", ""); dd.append(code(data.branch)); git.append(dd); }
    if (data.base) { git.append(make("dt", "", "From")); const dd = make("dd", ""); dd.append(code(String(data.base).slice(0, 10))); git.append(dd); }
    if (data.worktree) { git.append(make("dt", "", "Worktree")); const dd = make("dd", ""); dd.append(code(data.worktree)); git.append(dd); }
    if (data.landed) { git.append(make("dt", "", "Landed")); const dd = make("dd", ""); dd.append(code(`${String(data.landed.commit).slice(0, 10)} on ${data.landed.branch}`)); git.append(dd); }
    if (data.baseline) { git.append(make("dt", "", "Tests before")); git.append(make("dd", "", data.baseline.summary || "")); }
    if (data.validation?.ran) { git.append(make("dt", "", "Tests after")); git.append(make("dd", "", data.validation.summary || "")); }
    if (data.failure) { git.append(make("dt", "", "Why it stopped")); git.append(make("dd", "", data.failure)); }
    wrap.append(git);
    return wrap;
  }

  // ---- projects and the club ----------------------------------------------------------------------
  function renderProjects() {
    const list = $("#project-list");
    list.replaceChildren(...state.projects.map((project) => {
      const item = make("div", "project-item");
      const text = make("div", "grow");
      text.append(make("div", "project-name", project.source === "sample" ? `${project.name} · practice project` : project.name));
      const path = make("div", "project-path");
      path.append(make("code", "", project.path));
      path.append(document.createTextNode(` · tests: `), make("code", "", project.test || "none found"));
      if (project.detected && project.test) path.append(document.createTextNode(" (guessed)"));
      text.append(path);
      item.append(make("span", "", project.source === "sample" ? "🎒" : "📁"), text);
      if (project.source === "added") {
        const remove = make("button", "link", "remove");
        remove.type = "button";
        remove.onclick = () => { if (confirm(`Remove ${project.name} from the list? Its folder is not touched.`)) send({ type: "remove_project", name: project.name }); };
        item.append(remove);
      }
      return item;
    }));
  }

  // Where the prices come from. Artificial Analysis's terms ask that the source be credited wherever it is shown.
  function priceNote(missing) {
    const note = make("div", "price-note");
    const status = state.prices;
    if (status?.fetched_at) {
      note.append(document.createTextNode("Prices: "));
      const link = make("a", "", status.attribution.name);
      link.href = status.attribution.url;
      link.target = "_blank";
      link.rel = "noopener";
      note.append(link, document.createTextNode(`, ${new Date(status.fetched_at * 1000).toLocaleDateString()} · list prices, ` +
        "estimates" + (status.error ? ` · last refresh failed: ${status.error}` : "")));
    } else if (status && !status.key) {
      note.append(document.createTextNode(`Costs need prices: add a free ${status.key_env} (artificialanalysis.ai) to .env, ` +
        "or set prices: in the workbench's config.yml."));
    } else if (missing) {
      note.append(document.createTextNode("Some models have no price yet: see 👥 The club."));
    }
    if (missing && status?.fetched_at) note.append(document.createTextNode(" Some models have no price: see 👥 The club."));
    return note;
  }

  async function refreshPrices() {
    try {
      const response = await fetch("api/prices?refresh=1");
      if (response.ok) { state.prices = await response.json(); renderTeam(); }
    } catch { /* the dialog still shows what it has */ }
  }

  function renderTeam() {
    const list = $("#team-list");
    list.replaceChildren(...state.team.map((seat) => {
      const item = make("div", "seat");
      paint(item, seat.persona);
      const text = make("div", "grow");
      text.append(make("div", "seat-name", seat.name));
      text.append(make("div", "seat-line", seat.voice));
      const models = make("div", "seat-line");
      models.append(document.createTextNode(`${seat.family} · cheap `), make("code", "", seat.cheap));
      if (seat.strong && seat.strong !== seat.cheap) models.append(document.createTextNode(" · strong "), make("code", "", seat.strong));
      text.append(models);
      for (const row of (state.prices?.models || []).filter((item) => item.seat === seat.persona)) {
        const line = make("div", "seat-line");
        const label = `${row.tier}: `;
        if (row.price) {
          line.textContent = `${label}$${row.price.input} in · $${row.price.output} out per 1M tokens · ` +
            (row.price.source === "config" ? "your config" : `Artificial Analysis (${row.price.slug})`);
        } else {
          line.textContent = `${label}${row.model}: no price` + (row.candidates.length
            ? ` (close: ${row.candidates.join(", ")}; pick one under price_aliases)` : "");
        }
        text.append(line);
      }
      item.append(avatar(seat.persona), text);
      return item;
    }));
    const note = $("#team-prices");
    if (note) note.replaceChildren(priceNote(false));
    const roles = $("#roles");
    roles.replaceChildren();
    for (const [role, names] of Object.entries(state.roles || {})) {
      roles.append(make("dt", "", `${role} (${ROLES[role] || role})`), make("dd", "", names.map(nameOf).join(" → ")));
    }
  }

  function openDialog(name) {
    $(`#${name}-dialog`).hidden = false;
    if (name === "projects") send({ type: "folder", name: "new-project" });
  }
  for (const dialog of document.querySelectorAll(".dialog")) {
    dialog.addEventListener("click", (event) => { if (event.target === dialog || event.target.closest("[data-close]")) dialog.hidden = true; });
  }
  $("#open-projects").onclick = () => openDialog("projects");
  $("#open-team").onclick = () => { openDialog("team"); refreshPrices(); };
  $("#add-project").onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    send({ type: "add_project", name: form.get("name"), path: form.get("path"), test: form.get("test") });
    event.target.reset();
  };
  $("#create-project").onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    send({ type: "create_project", name: form.get("name"), path: form.get("path") });
    event.target.reset();
  };

  // ---- panels -------------------------------------------------------------------------------------
  const narrow = () => window.matchMedia("(max-width: 980px)").matches;
  const isOpen = (name) => document.body.classList.contains(`show-${name}`);
  function setPanel(name, open, remember = true) {
    document.body.classList.toggle(`show-${name}`, open);
    if (name === "inspector") $("#toggle-inspector").setAttribute("aria-pressed", String(open));
    if (remember && !narrow()) store.set(`workbench.${name}`, open ? "open" : "closed");
    $("#scrim").hidden = !(narrow() && (isOpen("sidebar") || isOpen("inspector")));
    if (name === "inspector" && open) renderInspector();
  }
  function closeNarrow() { if (narrow()) { setPanel("sidebar", false); setPanel("inspector", false); } }
  setPanel("sidebar", narrow() ? false : store.get("workbench.sidebar", "open") === "open", false);
  setPanel("inspector", narrow() ? false : store.get("workbench.inspector", "closed") === "open", false);
  $("#toggle-sidebar").onclick = () => setPanel("sidebar", !isOpen("sidebar"));
  $("#close-sidebar").onclick = () => setPanel("sidebar", false);
  $("#toggle-inspector").onclick = () => setPanel("inspector", !isOpen("inspector"));
  $("#close-inspector").onclick = () => setPanel("inspector", false);
  $("#scrim").onclick = closeNarrow;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => { state.tab = tab.dataset.tab; store.set("workbench.tab", state.tab); renderInspector(); };
  }
  const resizer = $("#resize-inspector");
  const savedWidth = Number(store.get("workbench.inspector.width"));
  if (savedWidth) document.documentElement.style.setProperty("--inspector-w", `${savedWidth}px`);
  resizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    document.body.classList.add("is-resizing");
    const move = (moved) => {
      const width = Math.round(Math.max(320, Math.min(window.innerWidth - 420, window.innerWidth - moved.clientX)));
      document.documentElement.style.setProperty("--inspector-w", `${width}px`);
      store.set("workbench.inspector.width", String(width));
    };
    const up = () => { document.body.classList.remove("is-resizing"); resizer.removeEventListener("pointermove", move); };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", up, { once: true });
  });
  resizer.addEventListener("dblclick", () => { document.documentElement.style.removeProperty("--inspector-w"); store.set("workbench.inspector.width", ""); });

  // ---- input --------------------------------------------------------------------------------------
  $("#composer").onsubmit = (event) => { event.preventDefault(); submit(); };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !touch) { event.preventDefault(); submit(); }
  });
  $("#project-select").onchange = (event) => {
    if (event.target.value === "__add") { openDialog("projects"); renderComposer(); return; }
    state.project = event.target.value;
    store.set("workbench.project", state.project);
    render();
  };
  $("#new-task").onclick = () => { open(null); closeNarrow(); };
  $("#filter").addEventListener("input", renderSidebar);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      for (const dialog of document.querySelectorAll(".dialog")) dialog.hidden = true;
      closeNarrow();
    }
  });
  document.addEventListener("click", (event) => {
    const copy = event.target.closest(".copy");
    if (copy) {
      const code = copy.closest("pre")?.querySelector("code")?.textContent || "";
      navigator.clipboard?.writeText(code).then(() => toast("Copied"));
    }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) document.title = state.title; });
  // A link to another task while the page is open (back, forward, a link from the map) opens it.
  window.addEventListener("hashchange", () => {
    const [linked, tab] = linkedTask();
    if (!linked || !state.tasks.has(linked)) return;
    if (["changes", "steps", "plan"].includes(tab)) { state.tab = tab; setPanel("inspector", true, false); }
    if (linked !== state.current) open(linked);
  });
  // A phone keyboard shrinks the visible area: fit the app to it, so the composer stays in view.
  const viewport = window.visualViewport;
  if (viewport) {
    const fit = () => {
      document.documentElement.style.setProperty("--app-top", `${viewport.offsetTop}px`);
      document.documentElement.style.setProperty("--app-height", `${viewport.height}px`);
    };
    viewport.addEventListener("resize", fit);
    viewport.addEventListener("scroll", fit);
    fit();
  }
  setInterval(() => { renderSidebar(); if (currentTask()?.status === "working") renderHeader(); }, 30000);

  let toastTimer = null;
  function toast(text) {
    const node = $("#toast");
    node.textContent = text;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3600);
  }

  try {  // what the page came with, drawn at once; the server's hello follows with the same, live
    const given = JSON.parse($("#state-data")?.textContent || "null");
    if (given?.team) hello(given);
  } catch { /* drawn at hello */ }
  connect();
})();
