"use strict";
// The cafe: every student walking around a preset room (she is anywhere at once, so she is here too). A tap opens
// her direct chat in MomoChat. Rooms are drawn here until pictures exist; any picture in rooms/ is a room as well.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const t = (text, values) => (window.t ? window.t(text, values) : text);  // (i18n.js)
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  // Each student's colour (her plate, as on MomoChat's page and the Students page); anyone else gets one from her name.
  const COLOURS = { gpt: "#5a5296", gemini: "#683fa0", claude: "#9a4916", deepseek: "#2f4f9e", grok: "#6f5900" };
  // What floats over her head: her mood (mneme-llm's face), or zzz at night.
  const MARKS = { great: "♪", good: "♪", ok: "~", worry: "?", sad: "…" };
  const markOf = (student) => {
    const hour = new Date().getHours();
    if (hour >= 1 && hour < 7) return "zzz";
    return MARKS[student.mood?.face] || "";
  };
  const shown = (student) => student.alias || student.display;
  const keyOf = (name) => String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 40);
  const colourOf = (name) => {
    const key = keyOf(name);
    if (COLOURS[key]) return COLOURS[key];
    let hash = 0;
    for (const letter of key) hash = (hash * 31 + letter.charCodeAt(0)) % 360;
    return `hsl(${hash} 45% 45%)`;
  };
  const DRAWN = {
    cafe: { name: "Sunny café", wall: "#f6dfc4", trim: "#c98f5e", floor: "#d8ad80", plank: "rgba(110,70,30,.16)",
            sky: "#cfe6f7", windows: true, counter: true, tables: true },
    library: { name: "Quiet library", wall: "#e9dcc6", trim: "#8b6a4a", floor: "#b88a5e", plank: "rgba(70,40,20,.18)",
               sky: "#dde6ee", windows: true, shelves: true, tables: true },
    rooftop: { name: "Rooftop", wall: "#bfe0f5", trim: "#b9b2a6", floor: "#cdc7bc", plank: "rgba(80,70,60,.12)",
               clouds: true, fence: true },
  };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {  // the chosen room is this browser's own convenience; nothing breaks without storage
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
  };
  const state = { students: [], avatars: new Set(), rooms: [], room: store.get("cafe.room") || "drawn:cafe", walkers: [] };

  // ---- the room ---------------------------------------------------------------------------------------
  const prettyName = (file) => file.replace(/\.[a-z]+$/i, "").replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());
  function choices() {
    return [...Object.entries(DRAWN).map(([key, room]) => ({ id: `drawn:${key}`, name: room.name, drawn: room })),
            ...state.rooms.map((file) => ({ id: `picture:${file}`, name: prettyName(file), picture: `rooms/${encodeURIComponent(file)}` }))];
  }
  function shape(className, style = {}) {
    const node = make("div", className);
    Object.assign(node.style, style);
    return node;
  }
  function drawRoom(room) {
    const parts = [shape("room-wall", { background: room.wall })];
    if (room.windows) {
      parts.push(shape("room-window", { left: "6%", background: room.sky }), shape("room-window", { left: "28%", background: room.sky }));
    }
    if (room.clouds) {
      parts.push(shape("room-cloud", { left: "10%", top: "12%", width: "140px" }), shape("room-cloud", { left: "44%", top: "22%", width: "190px" }),
                 shape("room-cloud", { left: "76%", top: "8%", width: "120px" }));
    }
    if (room.shelves) {
      const shelves = shape("room-shelves");
      shelves.append(make("div", "room-books"), make("div", "room-books"), make("div", "room-books"));
      parts.push(shelves);
    }
    parts.push(shape("room-trim", { background: room.trim }),
               shape("room-floor", { backgroundColor: room.floor,
                                     backgroundImage: `repeating-linear-gradient(90deg, transparent 0 86px, ${room.plank} 86px 88px)` }));
    if (room.fence) parts.push(shape("room-fence"));
    if (room.counter) parts.push(shape("room-counter"));
    if (room.tables) parts.push(shape("room-table", { left: "16%", bottom: "4%" }), shape("room-table", { left: "60%", bottom: "26%" }));
    return parts;
  }
  function renderRoom() {
    const all = choices();
    const room = all.find((choice) => choice.id === state.room) || all[0];
    const holder = $("#room");
    if (room.picture) {
      const picture = make("img", "picture");
      picture.src = room.picture;
      picture.alt = "";
      holder.replaceChildren(picture);
    } else {
      holder.replaceChildren(...drawRoom(room.drawn));
    }
    $("#room-name").textContent = room.name;
    $("#room-list").replaceChildren(...all.map((choice) => {
      const button = make("button", "room-choice");
      button.type = "button";
      button.setAttribute("aria-pressed", String(choice.id === room.id));
      const swatch = make("span", "room-swatch");
      if (choice.picture) swatch.style.backgroundImage = `url("${choice.picture}")`;
      else swatch.style.background = `linear-gradient(${choice.drawn.wall} 0 62%, ${choice.drawn.floor} 62%)`;
      button.append(swatch, make("span", "", choice.name));
      button.onclick = () => { state.room = choice.id; store.set("cafe.room", choice.id); renderRoom(); };
      return button;
    }));
  }

  // ---- the students ---------------------------------------------------------------------------------------
  function face(student, className) {
    const key = student.avatar || keyOf(student.name);
    if (state.avatars.has(key)) {
      const picture = make("img", className);
      picture.src = `avatars/${encodeURIComponent(key)}`;
      picture.alt = "";
      return picture;
    }
    const initial = make("span", className, (student.display || "?").trim().charAt(0).toUpperCase() || "?");
    initial.style.background = colourOf(student.name);
    return initial;
  }
  function toast(text) {
    const node = $("#toast");
    node.textContent = text;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3200);
  }
  function chat(student) {
    if (!student.ready) {
      toast(`${student.display} needs a model key before she can chat.`);
      return;
    }
    location.href = `./#dm=${encodeURIComponent(student.name)}`;
  }
  function place(walker, x, y) {
    walker.x = x;
    walker.y = y;
    walker.node.style.left = `${x}%`;
    walker.node.style.bottom = `${y}%`;
    walker.node.style.zIndex = String(100 - Math.round(y));  // nearer the front, in front
  }
  function wander(walker) {
    if (reduced || document.hidden) return;
    const x = 6 + Math.random() * 86, y = 3 + Math.random() * 27;  // somewhere on the floor
    const seconds = Math.max(1.2, Math.hypot(x - walker.x, y - walker.y) / 6);  // about 6% of the room a second
    walker.node.classList.toggle("is-left", x < walker.x);
    walker.node.classList.add("is-walking");
    walker.node.style.transition = `left ${seconds}s linear, bottom ${seconds}s linear`;
    place(walker, x, y);
    walker.timer = setTimeout(() => {
      walker.node.classList.remove("is-walking");
      walker.timer = setTimeout(() => wander(walker), 1500 + Math.random() * 5000);
    }, seconds * 1000);
  }
  function renderStudents() {
    for (const walker of state.walkers) clearTimeout(walker.timer);
    const count = Math.max(state.students.length, 1);
    state.walkers = state.students.map((student, index) => {
      const node = make("button", `chibi${student.ready ? "" : " is-away"}`);
      node.type = "button";
      const name = shown(student);
      node.setAttribute("aria-label", student.ready ? `Chat with ${name}${student.alias ? ` (${student.display})` : ""}`
                                                   : `${name} (needs a model key)`);
      const figure = make("span", "chibi-figure");
      figure.style.animationDuration = `${0.7 + ((index * 0.37) % 0.7)}s`;  // each her own pace
      const mark = markOf(student);
      const above = make("span", "chibi-mark", mark);
      above.style.color = colourOf(student.name);
      above.hidden = !mark;
      if ((student.pictures || []).includes("chibi")) {  // her own chibi (tools/draw_students.py)
        const art = make("img", "chibi-art");
        art.src = `pictures/${encodeURIComponent(student.name)}/chibi`;
        art.alt = "";
        node.classList.add("has-art");
        figure.append(above, art);
      } else {  // a stand-in: her face on a small body
        const torso = make("span", "chibi-torso");
        torso.style.background = colourOf(student.name);
        figure.append(above, face(student, "chibi-face"), torso);
      }
      node.append(figure, make("span", "chibi-shadow"), make("span", "chibi-name", name));
      node.onclick = () => chat(student);
      const walker = { node, x: 0, y: 0, timer: 0 };
      place(walker, 10 + (80 * (index + 0.5)) / count, 6 + ((index * 37) % 22));  // spread out to begin with
      return walker;
    });
    $("#floor").replaceChildren(...state.walkers.map((walker) => walker.node));
    state.walkers.forEach((walker, index) => { walker.timer = setTimeout(() => wander(walker), 600 + index * 700); });
    $("#people").replaceChildren(...state.students.map((student) => {
      const row = make("li");
      const chats = t(student.chats === 1 ? "MomoChat (1 chat)" : "MomoChat ({0} chats)", { 0: student.chats });
      const classroom = state.classroom && t(state.classroom.has(student.name) ? "the Classroom (working now)" : "the Classroom");
      const status = !student.ready ? t("needs a model key")
        : (classroom ? t("also in {0} and {1}", { 0: chats, 1: classroom }) : t("also in {0}", { 0: chats })) +
          (student.own_ready === false ? ` · ${t("answers on a classmate's model until you add her key")}` : "");
      row.append(face(student, "person-face"), make("strong", "", shown(student)), make("span", "person-status", status));
      return row;
    }));
  }
  document.addEventListener("visibilitychange", () => {  // a hidden page keeps still (and the Pi cool)
    for (const walker of state.walkers) {
      clearTimeout(walker.timer);
      walker.node.classList.remove("is-walking");
      if (!document.hidden) walker.timer = setTimeout(() => wander(walker), 400 + Math.random() * 1500);
    }
  });

  function show(data) {
    document.title = `Cafe · ${data.title}`;
    state.students = data.students || [];
    state.avatars = new Set(data.avatars || []);
    state.rooms = data.rooms || [];
    window.renderPlaces?.($("#places-nav"), data.places || { here: data.here, others: [] }, "Cafe");
    window.renderStaminaTotal?.($("#stamina"), data.stamina, { set: async (total) => {
      try {
        await fetch("api/stamina", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ total }) });
      } catch { /* shown again below */ }
      refresh();
    } });
    // the Classroom next door, when there is one: who is at work in it now
    const classroom = (data.places?.others || []).find((place) => place.place?.name === "Classroom");
    state.classroom = classroom ? new Set((classroom.people || []).map((person) => person.key)) : null;
    renderRoom();
    renderStudents();
  }
  async function refresh() {  // who can answer, the rooms and the places, again (the page came with them once)
    try {
      const response = await fetch("api/cafe", { headers: { Accept: "application/json" } });
      if (response.status === 401) { location.href = "login"; return; }
      show(await response.json());
    } catch {
      toast("Could not reach the cafe. Reload to try again.");
    }
  }
  let given = null;
  try { given = JSON.parse($("#cafe-data")?.textContent || "null"); } catch { given = null; }
  if (given && typeof given === "object") show(given);
  else refresh();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
