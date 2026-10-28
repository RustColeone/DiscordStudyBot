"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  // accent: rings and glows; ink: names and text; plate: the band under her portrait (her card, the Students
  // page); border/bubble (optional): override the strip colour and the bubble's fill.
  const PEOPLE = {
    gpt: { accent: "#ffffff", ink: "#5a5296", plate: "#5a5296" },
    gemini: { accent: "#9a86f2", ink: "#683fa0", plate: "#683fa0" },
    claude: { accent: "#e5896a", ink: "#9a4916", plate: "#9a4916" },
    deepseek: { accent: "#4d8ff0", ink: "#2f4f9e", plate: "#2f4f9e" },
    grok: { accent: "#e2b545", ink: "#6f5900", plate: "#6f5900" },
    you: { accent: "#f0a04b", ink: "#b8741f" },
    bot: { accent: "#b39c8a", ink: "#8c7a6b" },
  };
  const NOTICES = {
    run_failed: "⚠️ Sorry, we couldn't finish that one.",
    waiting_approval: "⏳ Waiting for approval…",
    unknown_command: "Unknown command. Type $ to see them all.",
    not_permitted: "That command isn't available here.",
    locked: "🔧 The bot is in maintenance right now.",
  };
  const state = {
    title: "Study Room", user: { name: "You" }, classmates: [], roster: [], avatars: new Set(), commands: [],
    conversations: [], current: localStorage.getItem("webui.chat") || "classroom", lastId: 0,
    progress: new Map(), unread: new Set(), pending: [], files: [], ws: null, retry: 0, paletteIndex: 0,
    spoke: new Map(), menuFor: null, dragging: null, drafts: new Map(),
    // The model menu: `$model`'s overview for the current chat (each classmate's model and what the providers
    // offer), the model that last answered for each classmate here, and whether `$model` is allowed at all.
    models: null, answered: new Map(), modelsAllowed: true, modelMenuFor: null,
    people: [], search: "",
  };
  // The places bar from the places the page came with, until the server says more.
  try {
    const given = JSON.parse($("#places-data")?.textContent || "null");
    if (given?.here) { state.places = given.others?.length ? given : null; state.here = given.here;
                       window.renderPlaces?.($("#places-nav"), given, "MomoChat"); }
  } catch { /* drawn when the server says hello */ }
  const messages = $("#messages");
  const input = $("#input");
  // A touch screen (a phone's on-screen keyboard): Return makes a new line and ➤ sends, as in phone chat apps.
  const touch = window.matchMedia("(pointer: coarse)").matches;

  // ---- people --------------------------------------------------------------------------------------
  const keyOf = (name) => String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 40);
  // A name as places compare them (contracts.PLACE_STATUS): its letters and digits in any script; "" matches nobody.
  const placeKey = (name) => String(name || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "").slice(0, 40);
  function look(key) {
    if (PEOPLE[key]) return PEOPLE[key];
    let hash = 0;
    for (const char of key) hash = (hash * 31 + char.charCodeAt(0)) % 360;
    return { accent: `hsl(${hash} 55% 62%)`, ink: `hsl(${hash} 45% 42%)`, plate: `hsl(${hash} 40% 36%)` };
  }
  function paint(node, key) {
    const colors = look(key);
    node.style.setProperty("--c", colors.accent);
    node.style.setProperty("--ci", colors.ink);
    node.style.setProperty("--cb", colors.border || colors.accent);
    node.style.setProperty("--cp", colors.plate || colors.ink);
    if (colors.bubble) node.style.setProperty("--cbg", colors.bubble);
    return node;
  }
  function avatar(name, key = keyOf(name)) {
    const node = paint(make("div", "avatar"), key);
    if (state.avatars.has(key)) {
      const image = make("img");
      image.src = `avatars/${key}`;
      image.alt = "";
      image.onerror = () => { image.remove(); node.textContent = initial(name); };
      node.append(image);
    } else {
      node.textContent = initial(name);
    }
    return node;
  }
  const initial = (name) => (String(name || "?").trim().charAt(0) || "?").toUpperCase();
  const classmateIn = (text) => state.classmates.find((name) => String(text || "").toLowerCase()
    .startsWith(name.toLowerCase()));
  // Who is in a chat: the classmates picked for it, or (picked by nobody) the whole class. One makes it direct.
  const displayOf = (name) => state.roster.find((item) => item.name === name)?.display || name;
  const chatOf = (id) => state.conversations.find((chat) => chat.id === id);
  const membersOf = (chat) => (chat?.personas ? chat.personas.map(displayOf) : state.classmates);
  const isDirect = (chat) => chat?.personas?.length === 1;
  // Where a student stands with you (the companion's facts, when it runs): your name for her, her bond and mood.
  const standingOf = (display) => state.roster.find((item) => item.display === display) || {};
  const FACES = { great: "😄", good: "🙂", ok: "😐", worry: "😟", sad: "😢" };
  // Her mood as a word (mneme-llm's face), and the pill's tone.
  const MOODS = { great: ["happy", "good"], good: ["content", "good"], ok: ["calm", "plain"], worry: ["worried", "warn"],
                  sad: ["sad", "low"] };
  const personOf = (display) => {
    const name = state.roster.find((item) => item.display === display)?.name;
    return state.people.find((item) => item.name === name) || {};
  };
  // Small drawn icons (constants of this file).
  const ICONS = {
    heart: '<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z" fill="#e0607e" stroke="none"/>',
  };
  function icon(name, size = 14) {
    const holder = make("span", `icon icon-${name}`);
    holder.setAttribute("aria-hidden", "true");
    holder.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}">${ICONS[name]}</svg>`;
    return holder;
  }
  function moodPill(display) {
    const { mood } = standingOf(display);
    if (!mood) return null;
    const [word, tone] = MOODS[mood.face] || ["", "plain"];
    const pill = make("span", `mood-pill mood-${tone}`, word || `mood ${mood.value}`);
    pill.title = `Mood ${mood.value}/100` + (mood.feelings?.length ? ` · ${mood.feelings.map(([name]) => name).join(", ")}` : "");
    return pill;
  }
  function bondPill(display, big = false) {
    const { bond } = standingOf(display);
    if (!bond) return null;
    const pill = make("span", big ? "bond-pill" : "bond-count");
    pill.append(icon("heart", big ? 16 : 14), make("span", "", big ? `Bond ${bond.closeness}` : String(bond.closeness)));
    pill.title = `${bond.role}, closeness ${bond.closeness}/100`;
    return pill;
  }
  function bondLine(display) {
    const { bond, mood } = standingOf(display);
    const parts = [], said = [];
    if (bond) { parts.push(`♥ ${bond.closeness}`); said.push(`${bond.role}, closeness ${bond.closeness}/100`); }
    if (mood) { parts.push(`${FACES[mood.face] || ""} ${mood.value}`.trim()); said.push(`mood ${mood.value}/100 (${mood.face})`); }
    if (!parts.length) return null;
    const line = make("div", "member-bond", parts.join("  "));
    line.title = said.join("; ");
    return line;
  }

  // ---- panels: chats on the left, classmates on the right ------------------------------------------
  // Three widths: wide shows both panels, medium the chats only, narrow neither (they slide over the chat
  // there). Either can be shown or hidden at any width; the choice is remembered per width, except on a
  // phone, where both start folded.
  const LEVELS = { wide: "(min-width: 1201px)", medium: "(min-width: 761px) and (max-width: 1200px)",
                   narrow: "(max-width: 760px)" };
  const DEFAULTS = { wide: { sidebar: true, members: true }, medium: { sidebar: true, members: false },
                     narrow: { sidebar: false, members: false } };
  const SIZES = { sidebar: [220, 440, 300], members: [220, 420, 280] };  // min, max, default width
  const media = Object.fromEntries(Object.entries(LEVELS).map(([name, query]) => [name, window.matchMedia(query)]));
  const level = () => Object.keys(media).find((name) => media[name].matches) || "wide";
  const isOpen = (name) => document.body.classList.contains(`show-${name}`);

  function setPanel(name, open, remember = true) {
    document.body.classList.toggle(`show-${name}`, open);
    $(name === "sidebar" ? "#toggle-sidebar" : "#toggle-members").setAttribute("aria-pressed", String(open));
    if (remember && level() !== "narrow") localStorage.setItem(`webui.${level()}.${name}`, open ? "open" : "closed");
    // On a phone a panel covers the chat; a tap outside closes it.
    $("#scrim").hidden = !(level() === "narrow" && (isOpen("sidebar") || isOpen("members")));
    fitWidths();
  }
  const closePanels = () => { if (level() === "narrow") { setPanel("sidebar", false); setPanel("members", false); } };
  function applyLevel() {
    const current = level();
    for (const name of ["sidebar", "members"]) {
      const stored = current === "narrow" ? null : localStorage.getItem(`webui.${current}.${name}`);
      setPanel(name, stored ? stored === "open" : DEFAULTS[current][name], false);
    }
  }

  // The width asked for (dragged, remembered) and the width shown can differ: on a smaller window the open
  // panels give way so the chat keeps at least CHAT_MIN, and grow back when there's room again.
  const CHAT_MIN = 360;
  const wanted = Object.fromEntries(Object.entries(SIZES).map(([name, [, , fallback]]) =>
    [name, Number(localStorage.getItem(`webui.width.${name}`)) || fallback]));
  const docked = (name) => level() !== "narrow" && isOpen(name);
  function inside() {  // the page's width inside its margins, less the gaps between the cards
    const app = $("#app");
    const style = getComputedStyle(app);
    const gaps = ["sidebar", "members"].filter(docked).length * (parseFloat(style.getPropertyValue("--gap")) || 0);
    return app.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0) - gaps;
  }
  function fitWidths() {
    const shown = { ...wanted };
    const open = Object.keys(shown).filter(docked);
    const spare = inside() - CHAT_MIN;
    const total = open.reduce((sum, name) => sum + shown[name], 0);
    if (total > spare) {
      for (const name of open) shown[name] = Math.max(SIZES[name][0], Math.floor(shown[name] * spare / total));
    }
    for (const [name, width] of Object.entries(shown)) {
      document.documentElement.style.setProperty(`--${name}-w`, `${width}px`);
    }
  }
  function setWidth(name, width) {
    const [low, high] = SIZES[name];
    const other = name === "sidebar" ? "members" : "sidebar";
    const room = inside() - CHAT_MIN - (docked(other) ? $(`#${other}`).getBoundingClientRect().width : 0);
    wanted[name] = Math.round(Math.max(low, Math.min(high, room, width)));
    fitWidths();
    return wanted[name];
  }
  function resizable(handle, name) {
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const startX = event.clientX;
      const startWidth = $(`#${name}`).getBoundingClientRect().width;
      const direction = name === "sidebar" ? 1 : -1;  // the sidebar grows rightwards, the class list leftwards
      document.body.classList.add("is-resizing");
      let width = startWidth;
      const moveTo = (move) => { width = setWidth(name, startWidth + direction * (move.clientX - startX)); };
      const stop = () => {
        handle.removeEventListener("pointermove", moveTo);
        document.body.classList.remove("is-resizing");
        localStorage.setItem(`webui.width.${name}`, String(width));
      };
      handle.addEventListener("pointermove", moveTo);
      handle.addEventListener("pointerup", stop, { once: true });
      handle.addEventListener("pointercancel", stop, { once: true });
    });
    handle.addEventListener("dblclick", () => {
      localStorage.removeItem(`webui.width.${name}`);
      setWidth(name, SIZES[name][2]);
    });
  }

  // Her card beside a direct chat: her portrait, your bond, what she is, and which model answers.
  function renderCard(name) {
    const card = $("#her-card");
    const person = personOf(name);
    const { bond, alias } = standingOf(name);
    const key = keyOf(name);
    paint(card, key);
    const portrait = make("div", "card-portrait");
    if (state.avatars.has(key)) {
      const image = make("img");
      image.src = `avatars/${key}`;
      image.alt = `${name}'s portrait`;
      portrait.append(image);
    } else {
      portrait.append(make("span", "card-initial", initial(name)));
    }
    const plate = make("div", "card-plate");
    plate.append(make("h2", "card-name", alias || name),
                 make("div", "card-what", [name, person.stage ? `Stage ${person.stage} art` : "",
                                           person.level ? `Lv ${person.level}` : "",
                                           ...(person.own || []).slice(0, 1).map(shortModel)].filter(Boolean).join(" · ")));
    const body = make("div", "card-body");
    if (bond) {
      const meter = make("div", "meter meter-bond");
      const head = make("div", "meter-head");
      head.append(make("span", "", "Bond"), make("span", "meter-value", `${bond.closeness} / 100`));
      const bar = make("div", "meter-bar");
      const fill = make("div", "meter-fill");
      fill.style.width = `${Math.max(0, Math.min(100, bond.closeness))}%`;
      bar.append(fill);
      meter.append(head, bar, make("div", "meter-note", bond.role));
      body.append(meter);
    }
    const { stamina } = standingOf(name);
    const view = window.staminaOf?.(stamina);
    if (view && (view.account != null || view.today != null)) {
      const meter = make("div", "meter meter-stamina");
      const head = make("div", "meter-head");
      head.append(make("span", "", "Stamina"), make("span", "meter-value is-stamina", view.accountText || ""));
      meter.append(head);
      if (view.account != null) {
        const bar = make("div", "meter-bar is-stamina");
        const fill = make("div", "meter-fill");
        fill.style.width = `${Math.round(view.account * 100)}%`;
        bar.append(fill);
        meter.append(bar);
      }
      if (view.today != null) {
        const today = make("div", "meter-head is-small");
        today.append(make("span", "", "Today"), make("span", "meter-value is-today", view.todayText));
        const bar = make("div", "meter-bar is-today");
        const fill = make("div", "meter-fill");
        fill.style.width = `${Math.round(view.today * 100)}%`;
        bar.append(fill);
        meter.append(today, bar);
      }
      body.append(meter);
    }
    if (person.own_ready === false) {
      body.append(make("p", "card-note is-warn", "Her own company's key isn't set, so she answers on a classmate's model."));
    }
    if (person.description) body.append(make("p", "card-note", person.description));
    body.append(make("p", "card-note", "Bond grows here, in MomoChat, as mneme-llm reads how your talks go. " +
                                       "Her level grows in the Classroom, with the work you land."));
    const model = modelLine(name);
    if (model) body.append(model);
    const persona = state.roster.find((item) => item.display === name)?.name;
    if (persona) {
      const link = make("a", "card-link", "Her student card");
      link.href = `students#${encodeURIComponent(persona)}`;
      body.append(link);
    }
    card.replaceChildren(portrait, plate, body);
  }

  function renderMembers() {
    const list = $("#member-list");
    list.replaceChildren();
    const herChat = chatOf(state.current);
    const herName = isDirect(herChat) ? membersOf(herChat)[0] : null;
    $("#her-card").hidden = !herName;
    $("#members").classList.toggle("is-card", Boolean(herName));
    if (herName) { renderCard(herName); return; }
    const typing = new Set();
    for (const event of state.progress.values()) {
      if (event.conversation !== state.current) continue;
      const name = classmateIn(event.text);
      if (name) typing.add(name);
    }
    const chat = chatOf(state.current);
    const members = membersOf(chat);
    $("#members-title").textContent = isDirect(chat) ? "Direct message" : `In this chat — ${members.length}`;
    const visiting = new Map((state.places?.others || []).flatMap((place) => (place.away || [])
      .map((key) => [key, place.place?.name || "another place"])));  // visiting another place (the Classroom, say)
    const tickets = ticketsHere(state.places);
    for (const name of members) {
      const spoke = (state.spoke.get(name) || 0) > Date.now() - 4000;
      const key = placeKey(name);
      // A student is in every place at once: here, and also wherever else she is working.
      const where = key && visiting.has(key) ? `here · also in the ${visiting.get(key)}`
        : key && tickets.has(key) ? `here · has an invitation to the ${tickets.get(key).place}` : "here";
      const card = paint(make("div", "member-card"), keyOf(name));
      const member = paint(make("button", "member"), keyOf(name));
      member.type = "button";
      member.title = `Mention @${name}`;
      if (typing.has(name)) member.classList.add("is-typing");
      else if (spoke) member.classList.add("is-speaking");
      const face = make("div", "member-face");
      face.append(avatar(name), make("span", "presence"));
      const text = make("div", "member-text");
      const { alias } = standingOf(name);
      const shown = make("div", "member-name", alias || name);
      if (alias) shown.append(make("span", "member-real", ` · ${name}`));  // what you call her, then her name
      text.append(shown, make("div", "member-status", typing.has(name) ? "typing…" : spoke ? "speaking" : where));
      const bond = bondLine(name);
      if (bond) text.append(bond);
      member.append(face, text);
      member.onclick = () => { mention(name); if (level() === "narrow") setPanel("members", false); };
      const row = make("div", "member-row");
      row.append(member);
      const persona = state.roster.find((item) => item.display === name)?.name;
      if (persona && !isDirect(chat)) {
        const direct = make("button", "member-dm", "💬");
        direct.type = "button";
        direct.title = `Message ${name}`;
        direct.setAttribute("aria-label", `Message ${name}`);
        direct.onclick = () => { startChat([persona]); if (level() === "narrow") setPanel("members", false); };
        row.append(direct);
      }
      card.append(row);
      const model = modelLine(name);
      if (model) card.append(model);
      list.append(card);
    }
    const you = make("div", "members-you");
    you.append(make("div", "members-subhead", "You"));
    const me = paint(make("div", "member is-you"), "you");
    const face = make("div", "member-face");
    face.append(avatar(state.user.name, "you"), make("span", "presence"));
    const text = make("div", "member-text");
    text.append(make("div", "member-name", state.user.name), make("div", "member-status", "creator"));
    me.append(face, text);
    you.append(me);
    list.append(you);
  }

  // ---- models: who answers with what, and choosing (`$model`, asked in the background) ----------------
  const personaOf = (name) => state.models?.personas.find((row) => row.display_name === name);
  const shortModel = (model) => String(model || "").split("/").slice(1).join("/") || String(model || "");
  function askModels() {
    if (state.modelsAllowed) transmit({ type: "models", conversation: state.current });
  }
  function modelLine(name) {
    const row = personaOf(name);
    const answered = state.answered.get(name);
    if (!state.modelsAllowed || (!row && !answered)) return null;
    // What she answers with here: the model chosen for this chat, else her own (until the overview arrives, the
    // one that last answered). Each of her messages shows the model that actually wrote it.
    const shown = row ? row.model : answered;
    const line = make("button", "member-model" + (row?.chosen ? " is-chosen" : ""),
      `${shortModel(shown) || "no model"}${row?.chosen ? " · chosen" : ""} ▾`);
    line.type = "button";
    line.title = [answered && `Answered last: ${answered}`, row && `Set here: ${row.model || "none"}${row.chosen ? " (chosen)" : " (her own)"}`]
      .filter(Boolean).join("\n") + "\nChoose her model in this chat";
    line.onclick = (event) => {  // a toggle: the same line closes her menu again
      event.stopPropagation();
      if (state.modelMenuFor === name && !$("#model-menu").hidden) hideModelMenu();
      else showModelMenu(name, line);
    };
    return line;
  }
  function showModelMenu(name, anchor) {
    const menu = $("#model-menu");
    menu.replaceChildren();
    state.modelMenuFor = name;
    const row = personaOf(name);
    menu.append(make("div", "menu-label", `${name}'s model here`));
    const entry = (label, action, current = false) => {
      const item = make("button", "menu-item" + (current ? " is-current" : ""), label);
      item.type = "button";
      item.disabled = !action;
      if (action) item.onclick = () => { hideModelMenu(); action(); };
      menu.append(item);
    };
    const choose = (model) => transmit({ type: "model", conversation: state.current, persona: row.name, model });
    if (!row) {
      entry("Loading…");
      askModels();
    } else {
      entry(row.chosen ? "↺ Her own model" : `✓ Her own (${shortModel(row.model) || "none"})`,
            row.chosen ? () => choose("reset") : null, !row.chosen);
      for (const [provider, models] of Object.entries(state.models.models || {})) {
        if (!models || !models.length) continue;
        menu.append(make("div", "menu-label", provider));
        for (const model of models) {
          const id = `${provider}/${model}`;
          entry(model, () => choose(id), row.chosen && row.model === id);
        }
      }
      for (const provider of Object.keys(state.models.errors || {})) {
        menu.append(make("div", "menu-label", `${provider} · unavailable now`));
      }
    }
    menu.hidden = false;
    const box = anchor.getBoundingClientRect();
    menu.style.top = `${Math.max(8, Math.min(box.bottom + 4, window.innerHeight - menu.offsetHeight - 8))}px`;
    menu.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - menu.offsetWidth - 8))}px`;
    if (Date.now() - (state.models?.at || 0) > 60000) askModels();  // lists are kept fresh enough by the core
  }
  function hideModelMenu() { $("#model-menu").hidden = true; state.modelMenuFor = null; }
  function receiveModels(event) {
    if (event.purpose === "choose") {
      toast(event.text || NOTICES[event.code] || "Not changed.");
      askModels();
      return;
    }
    if (event.conversation !== state.current) return;
    if (event.ok && event.result.personas) {
      state.models = { ...event.result, at: Date.now() };
    } else if (event.code === "not_permitted") {
      state.modelsAllowed = false;  // `$model` is for admins; the menu stays away
    }
    renderMembers();
    const open = state.modelMenuFor && [...document.querySelectorAll(".member-card")]
      .find((card) => card.querySelector(".member-name")?.textContent === state.modelMenuFor);
    if (open?.querySelector(".member-model")) showModelMenu(state.modelMenuFor, open.querySelector(".member-model"));
  }

  // ---- chats ---------------------------------------------------------------------------------------
  // A chat's faces: hers for a direct chat, else up to three of the class, overlapping.
  function chatFaces(chat) {
    const members = membersOf(chat);
    const holder = make("span", `faces${isDirect(chat) ? " is-one" : ""}`);
    for (const name of members.slice(0, isDirect(chat) ? 1 : 3)) holder.append(avatar(name));
    return holder;
  }
  const shownName = (display) => standingOf(display).alias || display;
  function lastLine(chat) {
    const last = chat.last;
    if (!last) return isDirect(chat) ? "Say hello" : `${membersOf(chat).length} classmates`;
    if (last.kind === "user") return `You: ${last.text}`;
    const who = classmateIn(last.speaker) || last.speaker;
    return isDirect(chat) || !who ? last.text : `${shownName(who)}: ${last.text}`;
  }
  function renderChats() {
    const list = $("#chats");
    list.replaceChildren();
    const query = state.search.trim().toLowerCase();
    const matches = (chat) => !query || [chat.title, ...membersOf(chat).map((name) => `${name} ${shownName(name)}`),
                                          chat.last?.text || ""].join(" ").toLowerCase().includes(query);
    // Group chats first, then direct messages, each in the order the user arranged.
    const direct = state.conversations.filter(isDirect).filter(matches);
    for (const chat of [...state.conversations.filter((item) => !isDirect(item)).filter(matches), ...direct]) {
      if (chat === direct[0]) list.append(make("div", "chats-subhead", "Direct messages"));
      const item = make("div", "chat" + (chat.id === state.current ? " is-current" : "") + (isDirect(chat) ? " is-direct" : ""));
      item.draggable = !query;
      item.dataset.id = chat.id;
      const open_ = make("button", "chat-open");
      open_.type = "button";
      open_.title = chat.personas ? membersOf(chat).join(", ") : "The whole class";
      if (chat.id === state.current) open_.setAttribute("aria-current", "true");
      open_.onclick = () => { open(chat.id); if (level() === "narrow") setPanel("sidebar", false); };
      const text = make("span", "chat-text");
      const top = make("span", "chat-top");
      const title = make("strong", "chat-name");
      const right = make("span", "chat-side");
      if (isDirect(chat)) {
        const her = membersOf(chat)[0];
        title.textContent = shownName(her);
        if (standingOf(her).alias) title.append(make("span", "chat-real", ` ${her}`));
        const bond = bondPill(her);
        if (bond) right.append(bond);
      } else {
        title.textContent = chat.title || chat.id;
        right.textContent = "group";
      }
      top.append(title, right);
      text.append(top, make("span", "chat-last", lastLine(chat)));
      open_.append(chatFaces(chat), text);
      item.append(open_);
      if (chat.pinned) item.append(make("span", "chat-pin", "📌"));
      if (state.unread.has(chat.id)) item.append(make("span", "dot"));
      const more = make("button", "chat-more", "⋯");
      more.type = "button";
      more.title = "Chat options";
      more.onclick = (event) => { event.stopPropagation(); showMenu(chat, more); };
      item.append(more);
      item.addEventListener("dragstart", (event) => {
        state.dragging = chat.id;
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", chat.id);
        item.classList.add("is-dragging");
      });
      item.addEventListener("dragend", () => { state.dragging = null; item.classList.remove("is-dragging"); });
      item.addEventListener("dragover", (event) => {
        if (!state.dragging || state.dragging === chat.id) return;
        event.preventDefault();
        const below = event.offsetY > item.offsetHeight / 2;
        item.classList.toggle("drop-below", below);
        item.classList.toggle("drop-above", !below);
      });
      item.addEventListener("dragleave", () => item.classList.remove("drop-above", "drop-below"));
      item.addEventListener("drop", (event) => {
        if (!state.dragging) return;
        event.preventDefault();
        event.stopPropagation();
        const below = item.classList.contains("drop-below");
        item.classList.remove("drop-above", "drop-below");
        move(state.dragging, chat.id, below);
      });
      list.append(item);
    }
    if (query && !list.children.length) list.append(make("div", "chats-none", "No chat matches."));
    // Students without their own company's key still answer, on a classmate's model (their fallback).
    const borrowing = state.people.filter((item) => item.own_ready === false).map((item) => displayOf(item.name));
    if (borrowing.length && !query) {
      const note = make("div", "chats-note");
      const faces = make("span", "faces is-grey");
      for (const name of borrowing.slice(0, 3)) faces.append(avatar(name));
      const names = borrowing.length === 1 ? borrowing[0]
        : `${borrowing.slice(0, -1).join(", ")} and ${borrowing[borrowing.length - 1]}`;
      note.append(faces, make("span", "", `${names} answer on a classmate's model until you add ` +
                                          `${borrowing.length === 1 ? "her key" : "their keys"}`));
      list.append(note);
    }
    renderHeader();
  }

  // The chat's header: who it is with, where else she is, her mood and your bond.
  function renderHeader() {
    const current = chatOf(state.current);
    const members = membersOf(current);
    const her = isDirect(current) ? members[0] : null;
    const faces = $("#chat-faces");
    faces.replaceChildren(current ? chatFaces(current) : make("span"));
    const title = $("#chat-title");
    title.replaceChildren(her ? shownName(her) : current ? current.title : "New chat");
    if (her && standingOf(her).alias) title.append(make("span", "chat-title-real", ` ${her}, as you call her`));
    title.classList.toggle("can-rename", !her);
    title.title = her ? "" : "Rename";
    $("#chat-sub").replaceChildren(...subLine(current, her));
    $("#chat-standing").replaceChildren(...(her ? [moodPill(her), bondPill(her, true)].filter(Boolean) : []));
    messages.classList.toggle("is-direct", Boolean(her));
    document.title = `${current ? (her ? shownName(her) : current.title) + " · " : ""}${state.title}`;
    $("#empty-title").textContent = her ? `Just you and ${shownName(her)}.`
      : current?.personas ? `${members.map(shownName).join(", ")} are here.` : "Everyone's here.";
    input.placeholder = her ? `Message ${shownName(her)}…` : "Message the club…";
  }
  function subLine(chat, her) {
    if (!her) return [make("span", "", chat?.personas ? `${members_(chat)} classmates` : "The whole club · group")];
    const key = placeKey(her);
    const ticket = key ? ticketsHere(state.places).get(key) : null;
    if (ticket) {  // she has an invitation for you, to the Classroom say
      const go = make("a", "chat-ticket", `Invites you to the ${ticket.place}: “${ticket.title}”`);
      go.href = ticket.link || "";
      return [go];
    }
    const visiting = (state.places?.others || []).find((place) => (place.away || []).includes(key));
    return [make("span", "", visiting ? `Also in the ${visiting.place?.name || "Classroom"}` : "Here, in MomoChat")];
  }
  const members_ = (chat) => membersOf(chat).length;

  function move(id, target, below) {
    const order = state.conversations.map((chat) => chat.id).filter((item) => item !== id);
    const index = order.indexOf(target);
    order.splice(index + (below ? 1 : 0), 0, id);
    const byId = new Map(state.conversations.map((chat) => [chat.id, chat]));
    state.conversations = order.map((item) => byId.get(item));
    renderChats();
    transmit({ type: "order", conversations: order });
  }

  function showMenu(chat, anchor) {
    const menu = $("#chat-menu");
    menu.replaceChildren();
    state.menuFor = chat.id;
    const index = state.conversations.findIndex((item) => item.id === chat.id);
    const entries = [
      ["✏️ Rename", () => rename(chat)],
      [chat.pinned ? "📌 Unpin" : "📌 Pin to top", () => transmit({ type: "pin", conversation: chat.id, pinned: !chat.pinned })],
      ["↑ Move up", () => index > 0 && move(chat.id, state.conversations[index - 1].id, false), index === 0],
      ["↓ Move down", () => index < state.conversations.length - 1 &&
        move(chat.id, state.conversations[index + 1].id, true), index === state.conversations.length - 1],
      ["🗑️ Delete", () => remove(chat), false, "danger"],
    ];
    for (const [label, action, disabled, style] of entries) {
      const item = make("button", "menu-item" + (style ? ` menu-${style}` : ""), label);
      item.type = "button";
      item.disabled = Boolean(disabled);
      item.onclick = () => { hideMenu(); action(); };
      menu.append(item);
    }
    const box = anchor.getBoundingClientRect();
    menu.style.top = `${Math.min(box.bottom + 4, window.innerHeight - 230)}px`;
    menu.style.left = `${Math.min(box.left, window.innerWidth - 190)}px`;
    menu.hidden = false;
  }
  function hideMenu() { $("#chat-menu").hidden = true; state.menuFor = null; }

  function rename(chat) {
    const title = prompt("Name this chat", chat.title || "");
    if (title && title.trim()) transmit({ type: "rename", conversation: chat.id, title: title.trim() });
  }
  function remove(chat) {
    if (confirm(`Delete “${chat.title || chat.id}”? Its messages and files disappear from this page.`)) {
      transmit({ type: "delete", conversation: chat.id });
    }
  }

  // ---- messages ------------------------------------------------------------------------------------
  const time = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const nearBottom = () => messages.scrollHeight - messages.scrollTop - messages.clientHeight < 120;
  const scrollDown = () => { messages.scrollTop = messages.scrollHeight; };

  function speakerOf(item) {
    if (item.kind === "user") return { name: item.sender || state.user.name, key: "you", mine: true };
    if (item.speaker) return { name: item.speaker, key: keyOf(item.speaker), mine: false };
    return { name: state.title, key: "bot", mine: false };
  }

  function fileView(file) {
    const url = `files/${encodeURIComponent(file.id)}`;
    if (/^image\//.test(file.mime)) {
      const image = make("img", "attachment-image");
      image.src = url;
      image.alt = file.name;
      image.loading = "lazy";
      image.onclick = () => view(url);
      return image;
    }
    if (/^(audio|video)\//.test(file.mime)) {
      const player = make(file.mime.startsWith("audio") ? "audio" : "video", "attachment-media");
      player.controls = true;
      player.src = url;
      return player;
    }
    const link = make("a", "attachment-file");
    link.href = url;
    link.download = file.name;
    link.append(make("span", "file-icon", "📄"), make("span", "file-name", file.name),
      make("span", "file-size", size(file.size)));
    return link;
  }
  const size = (bytes) => bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  function bubbleText(text) {
    const body = make("div", "markdown");
    window.Markdown.into(body, text, state.classmates);
    body.querySelectorAll(".mention").forEach((chip) => paint(chip, chip.dataset.who));
    return body;
  }

  function approvalCard(item) {
    const data = item.data || {};
    const card = make("div", "approval");
    const confirm = data.kind === "confirm";
    const head = make("div", "approval-head");
    head.append(make("span", "approval-icon", confirm ? "🙋" : "🔐"),
      make("span", "approval-title", confirm ? "Please confirm" : "May we do this?"));
    if (data.tier) head.append(make("span", `tier tier-${data.tier}`, data.tier));
    card.append(head, make("div", "approval-summary", data.summary || data.capability || item.text));
    for (const warning of String(data.warnings || "").split(" | ").filter(Boolean)) {
      card.append(make("div", "approval-warning", `⚠️ ${warning}`));
    }
    card.append(make("div", "approval-meta", `Asked by ${data.asked_by || "someone"} in ${data.address || "this chat"}`));
    const details = make("details", "approval-details");
    details.append(make("summary", "", "Exact arguments"), make("pre", "", data.arguments || "{}"));
    card.append(details);
    const buttons = make("div", "approval-buttons");
    if (item.decided) {
      const denied = item.decided === "denied";
      const word = confirm ? (denied ? "cancelled" : "confirmed") : item.decided;
      buttons.append(make("span", "decided", `${denied ? "✕" : "✓"} ${word}`));
    } else {
      for (const action of item.actions || []) buttons.append(actionButton(action));
    }
    card.append(buttons);
    return card;
  }

  function actionButton(action) {
    const button = make("button", `action action-${action.style || "secondary"}`, action.label);
    button.type = "button";
    button.onclick = () => {
      button.closest(".approval-buttons, .typing-row")?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      send(action.command, [], action.label);
    };
    return button;
  }

  function noticeText(item) {
    const data = item.data || {};
    if (item.code === "relayed") {
      return data.ok === "true" ? `↪️ Sent to ${data.address}` : `⚠️ Not sent to ${data.address}: ${data.detail || "error"}`;
    }
    if (item.code === "waiting_approval" && data.approval_id) return `⏳ Waiting for approval ${data.approval_id}…`;
    return item.text || NOTICES[item.code] || `[${item.code || "notice"}]`;
  }

  function renderItem(item) {
    const row = make("div", "row");
    row.dataset.id = item.id;
    if (item.kind === "notice" && item.code === "approval_request") {
      row.classList.add("row-card");
      row.append(approvalCard(item));
      return row;
    }
    if (item.kind === "notice" && item.code === "reminder") {
      row.classList.add("row-card");
      const card = make("div", "reminder");
      card.append(make("span", "reminder-icon", "⏰"), bubbleText(item.text));
      row.append(card);
      return row;
    }
    if (item.kind === "notice" || item.kind === "event" || (item.kind === "user" && item.action)) {
      row.classList.add("row-pill");
      const text = item.kind === "user" ? `You chose “${item.action}”` :
        item.kind === "event" ? `📨 ${item.text}` : noticeText(item);
      const pill = make("div", "pill", text);
      if (item.origin) pill.append(make("span", "origin", ` · ${item.origin}`));
      row.append(pill);
      return row;
    }
    const who = speakerOf(item);
    row.classList.add(who.mine ? "row-mine" : "row-theirs");
    row.dataset.speaker = who.key;
    paint(row, who.key);
    const face = avatar(who.name, who.key);
    const column = make("div", "column");
    const name = make("div", "name");
    name.append(make("span", "", who.name));
    if (item.label) name.append(make("span", "label", item.label));
    name.append(make("span", "time", time(item.at)));
    if (item.data?.model) {  // the model that wrote it, and why, when it was not her first choice
      const fell = item.data.fallback;
      const model = make("span", "time model-tag" + (fell ? " is-fallback" : ""),
        `· ${item.data.model}${fell ? " ⚠" : ""}`);
      model.title = `${item.data.provider}/${item.data.model}` + (fell ? `\nFell back: ${fell}` : "");
      name.append(model);
    }
    const bubble = make("div", "bubble");
    if (item.label) {
      const note = make("details", "work-note");
      note.append(make("summary", "", `📝 ${item.label}`), bubbleText(item.text));
      bubble.append(note);
      bubble.classList.add("bubble-note");
    } else if (item.text) {
      bubble.append(bubbleText(item.text));
    }
    for (const file of item.files || (item.file ? [item.file] : [])) bubble.append(fileView(file));
    if (item.origin) bubble.append(make("div", "origin", `from ${item.origin}`));
    for (const action of item.kind !== "user" ? item.actions || [] : []) bubble.append(actionButton(action));
    column.append(name, bubble);
    row.append(face, column);
    return row;
  }

  function append(item, live = false) {
    if (item.id <= state.lastId) return;
    $("#empty").hidden = true;
    const stick = nearBottom();
    const row = renderItem(item);
    const previous = messages.lastElementChild;
    if (previous && previous.dataset.speaker && previous.dataset.speaker === row.dataset.speaker &&
        item.at - Number(previous.dataset.at || 0) < 180) {
      row.classList.add("is-followup");
    }
    row.dataset.at = item.at;
    messages.append(row);
    state.lastId = Math.max(state.lastId, item.id);
    if (item.speaker && item.data?.model) {
      state.answered.set(classmateIn(item.speaker) || item.speaker, `${item.data.provider}/${item.data.model}`);
      if (live) renderMembers();
    }
    if (live && item.speaker && item.kind === "text" && !item.label) {
      const name = classmateIn(item.speaker);
      if (name) { state.spoke.set(name, Date.now()); renderMembers(); setTimeout(renderMembers, 4100); }
    }
    if (stick || item.kind === "user") scrollDown();
  }

  async function open(conversation) {
    if (conversation !== state.current) {
      // Each chat keeps its own half-typed message.
      state.drafts.set(state.current, input.value);
      input.value = state.drafts.get(conversation) || "";
      grow();
      closePalette();
    }
    state.current = conversation;
    localStorage.setItem("webui.chat", conversation);
    state.unread.delete(conversation);
    state.lastId = 0;
    state.models = null;
    state.answered = new Map();
    hideModelMenu();
    askModels();
    messages.querySelectorAll(".row").forEach((row) => row.remove());
    $("#empty").hidden = false;
    renderChats();
    renderMembers();
    renderTyping();
    await loadHistory();
    if (!touch) input.focus();  // on a phone that would pop the keyboard over the chat just opened
  }

  async function loadHistory() {
    const conversation = state.current;
    try {
      const response = await fetch(`api/history?conversation=${encodeURIComponent(conversation)}&after=${state.lastId}`);
      if (!response.ok) return;
      const { messages: items } = await response.json();
      if (conversation !== state.current) return;
      items.forEach((item) => append(item));
      renderMembers();
      scrollDown();
    } catch (error) { /* the next reconnect loads it */ }
  }

  // ---- typing --------------------------------------------------------------------------------------
  function renderTyping() {
    const area = $("#typing");
    area.replaceChildren();
    const stale = Date.now() / 1000 - 900;
    for (const [run, event] of state.progress) {
      if (event.at < stale) { state.progress.delete(run); continue; }
      if (event.conversation !== state.current) continue;
      const name = classmateIn(event.text);
      const row = make("div", "typing-row");
      row.append(avatar(name || state.title, name ? keyOf(name) : "bot"));
      let text = event.text;
      if (name && text.startsWith(`${name} · `)) text = `${name} is using ${text.slice(name.length + 3)}`;
      row.append(make("span", "dots"), make("span", "typing-text", text));
      for (const action of event.actions || []) row.append(actionButton(action));
      area.append(row);
    }
    renderMembers();
  }

  // ---- connection ----------------------------------------------------------------------------------
  function connect() {
    // Relative to the page, like every other URL here, so it also works under a proxy's path (…/chat/ws).
    const url = new URL("ws", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => { state.retry = 0; setConnection(""); };
    ws.onmessage = (message) => receive(JSON.parse(message.data));
    ws.onclose = () => {
      setConnection("Reconnecting…");
      const delay = Math.min(10000, 500 * 2 ** state.retry++);
      setTimeout(connect, delay);
    };
  }
  function setConnection(text) {
    $("#connection").textContent = text;
    $("#connection").classList.toggle("is-off", Boolean(text));
  }

  // The mini map: MomoChat (here) and the Classroom next door, who is in each, and who waits at the Classroom's door
  // with an invitation. The Classroom says who is there (the server asks it); without a Classroom there is no map.
  // The places other interfaces publish in the core's directory (contracts 1.7), and this classroom.
  const awayKeys = (places) => new Set((places?.others || []).flatMap((place) => place.away || []));
  const ticketsHere = (places) => new Map((places?.others || []).flatMap((place) => (place.tickets || [])
    .filter((ticket) => ticket.key).map((ticket) => [ticket.key, { ...ticket, place: place.place?.name || "there" }])));

  function renderMap() {
    const map = $("#map");
    const places = state.places;
    if (!map) return;
    map.hidden = !places;
    if (!places) return;
    const tickets = ticketsHere(places); // invitations to another place, held by classmates here
    const tile = (place, here) => {
      const node = make(here ? "div" : "a", `place${here ? " is-here" : ""}`);
      if (!here && place.link) { node.href = place.link; node.title = `Go to ${place.place.name}`; }
      node.append(make("span", "place-name", `${place.place.icon || "📍"} ${place.place.name}`));
      const faces = make("span", "place-faces");
      const people = place.people || [];  // a student is in every place at once: here, and wherever she works
      for (const person of people.slice(0, 6)) {
        const face = avatar(person.name, person.avatar || keyOf(person.name));
        face.title = person.title ? `${person.name} · ${person.doing || "working"} on “${person.title}”` : person.name;
        const ticket = here ? tickets.get(person.key) : null;
        if (ticket) {  // she has an invitation for you
          face.classList.add("has-ticket");
          face.title = `${person.name} invites you to the ${ticket.place.toLowerCase()}: “${ticket.title}”`;
        }
        faces.append(face);
      }
      const note = place.place.note || "";
      node.append(faces, make("span", "place-note", here ? `You are here${note ? ` · ${note}` : ""}` : note));
      return node;
    };
    const corridor = make("div", "corridor");
    for (const ticket of [...tickets.values()].slice(0, 3)) {
      const door = make("a", "door");
      door.href = ticket.link || "";
      door.title = `Follow ${ticket.by || "a classmate"} to the ${ticket.place.toLowerCase()}: “${ticket.title}”`;
      door.append(make("span", "", "🎟️"), make("span", "", "🚪"));
      corridor.append(door);
    }
    map.replaceChildren(tile(places.here, true), corridor, ...(places.others || []).slice(0, 1).map((place) => tile(place, false)));
  }

  // MomoChat not loaded here (no momochat engine, or no students): the page stays readable, greyed, and says why
  // instead of taking messages nobody answers.
  function offline(off) {
    document.body.classList.toggle("momochat-off", off);
    $("#input").disabled = off;
    $("#send").disabled = off;
    let note = $("#offline-note");
    if (off && !note) {
      note = make("div", "offline-note", "MomoChat isn't loaded, so nobody answers here. `python -m core --check` " +
                  "says why (it needs the momochat and students modules, and the students need mneme-llm).");
      note.id = "offline-note";
      $("#composer").before(note);
    } else if (!off && note) note.remove();
  }

  function receive(event) {
    if (event.type === "hello") {
      const s = event.state;
      Object.assign(state, { title: s.title, user: s.user, classmates: s.classmates, commands: s.commands,
                             conversations: s.conversations, avatars: new Set(s.avatars),
                             roster: s.roster || s.classmates.map((name) => ({ name: keyOf(name), display: name })) });
      state.progress = new Map(s.progress.map((item) => [item.run_id, item]));
      state.people = s.people || [];
      renderStaminaBar();
      renderMembers();
      for (const pending of state.pending.splice(0)) state.ws.send(JSON.stringify(pending));
      state.places = s.places;
      state.here = s.here;
      offline(s.momochat === false);
      renderMap();
      window.renderPlaces?.($("#places-nav"), state.places || { here: state.here, others: [] }, "MomoChat");
      // A link to a chat (#<chat>, from the Classroom's "open the chat", say) opens it; #dm=<student>, from the
      // cafe, opens the direct chat with her (started if there is none yet).
      let linked = "";
      try { linked = decodeURIComponent(location.hash.slice(1)); } catch { linked = ""; }
      if (linked) history.replaceState(null, "", location.pathname + location.search);
      const direct = linked.startsWith("dm=") ? linked.slice(3) : "";
      if (direct && state.roster.some((item) => item.name === direct)) {
        startChat([direct]);
        return;
      }
      if (linked && linked !== state.current && state.conversations.some((chat) => chat.id === linked)) {
        open(linked);
        return;
      }
      if (!state.conversations.some((chat) => chat.id === state.current)) {
        open(s.default);
        return;
      }
      renderChats();
      renderTyping();
      loadHistory();
      askModels();
    } else if (event.type === "places") {
      const where = (places) => JSON.stringify([[...awayKeys(places)], [...ticketsHere(places).keys()]]);
      const moved = where(state.places) !== where(event.places);
      state.places = event.places;
      renderMap();
      window.renderPlaces?.($("#places-nav"), state.places || { here: state.here, others: [] }, "MomoChat");
      if (moved) renderMembers();  // "also in the Classroom" in the member list too
    } else if (event.type === "roster") {  // a new alias, a bond grown, a mood changed, stamina spent
      noteBonds(state.roster, event.roster);
      state.roster = event.roster;
      renderStaminaBar();
      renderMembers();
      renderChats();
    } else if (event.type === "models") {
      receiveModels(event);
    } else if (event.type === "message") {
      const chat = chatOf(event.item.conversation);
      const item = event.item;
      if (chat && ["text", "user"].includes(item.kind) && item.text && !item.label && !item.action) {
        chat.last = { kind: item.kind, speaker: item.speaker || "", text: String(item.text).split(/\s+/).join(" ").slice(0, 120) };
        if (event.item.conversation === state.current) renderChats();
      }
      if (event.item.conversation === state.current) append(event.item, true);
      else { state.unread.add(event.item.conversation); renderChats(); }
    } else if (event.type === "update") {
      const old = messages.querySelector(`.row[data-id="${event.item.id}"]`);
      if (old) {
        const row = renderItem(event.item);
        row.className = old.className;
        row.dataset.at = old.dataset.at;
        old.replaceWith(row);
      }
    } else if (event.type === "deleted") {
      state.conversations = event.items;
      state.unread.delete(event.conversation);
      if (event.conversation === state.current) open(state.conversations[0]?.id || "classroom");
      else renderChats();
    } else if (event.type === "conversations") {
      state.conversations = event.items;
      renderChats();
    } else if (event.type === "progress") {
      state.progress.set(event.run_id, event);
      renderTyping();
    } else if (event.type === "progress_clear") {
      state.progress.delete(event.run_id);
      renderTyping();
    } else if (event.type === "error") {
      toast(event.text);
    } else if (event.type === "ack" && !event.ok) {
      toast(event.reason === "disabled" ? "This chat is switched off."
        : event.reason === "chat_not_ready" || event.reason === "unknown_chat"
          ? "That chat isn't there (it couldn't be set up, or was deleted), so this wasn't sent."
          : `Not sent (${event.reason}).`);
    }
  }

  // The stamina pill in the header: each student with her own key (the others spend a classmate's account).
  function renderStaminaBar() {
    window.renderStamina?.($("#stamina"), state.roster
      .filter((item) => state.people.find((person) => person.name === item.name)?.own_ready !== false)
      .map((item) => ({ name: item.alias || item.display, href: `students#${encodeURIComponent(item.name)}`,
                        picture: state.avatars.has(keyOf(item.display)) ? `avatars/${keyOf(item.display)}` : "",
                        plate: look(keyOf(item.display)).plate, stamina: item.stamina })));
  }

  // A bond that moved, said in the chat with her (or a group she is in) as it happens; it is not kept.
  function noteBonds(before, after) {
    const chat = chatOf(state.current);
    for (const item of after || []) {
      const old = (before || []).find((entry) => entry.name === item.name)?.bond?.closeness;
      const now = item.bond?.closeness;
      if (old == null || now == null || old === now || !membersOf(chat).includes(item.display)) continue;
      const row = make("div", "row row-pill");
      const pill = make("div", "pill bond-note");
      pill.append(icon("heart"), make("span", "", `Bond with ${item.alias || item.display} ${now > old ? "rose" : "fell"}: ${old} → ${now}`));
      row.append(pill);
      $("#empty").hidden = true;
      messages.append(row);
      if (nearBottom()) scrollDown();
    }
  }

  function transmit(payload) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(payload));
    else state.pending.push(payload);
  }

  function send(text, files = [], action = "") {
    const id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`).replace(/-/g, "");
    transmit({ type: "send", conversation: state.current, id, text, files, action });
  }

  function toast(text) {
    const node = make("div", "toast", text);
    document.body.append(node);
    setTimeout(() => node.remove(), 3500);
  }

  // ---- composer ------------------------------------------------------------------------------------
  function mention(name) {
    const text = input.value;
    const caret = input.selectionStart ?? text.length;
    const before = text.slice(0, caret).replace(/@\w*$/, "");
    const spacer = before && !/\s$/.test(before) ? " " : "";
    input.value = `${before}${spacer}@${name} ${text.slice(caret).replace(/^\s+/, "")}`;
    const end = before.length + spacer.length + name.length + 2;
    input.focus();
    input.setSelectionRange(end, end);
    grow();
  }
  function grow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  function submit() {
    let text = input.value;
    const command = /^\/(\w+)/.exec(text);
    if (command && state.commands.some((item) => item.name === command[1].toLowerCase())) text = "$" + text.slice(1);
    const files = state.files.filter((file) => file.id).map((file) => file.id);
    if (state.files.some((file) => !file.id)) { toast("Still uploading…"); return; }
    if (!text.trim() && !files.length) return;
    send(text, files);
    input.value = "";
    state.files = [];
    renderAttachments();
    closePalette();
    grow();
  }

  // Suggestions: `$` (or `/`) at the start lists commands; `@` before the caret lists classmates.
  function suggestions() {
    const command = /^[$/](\w*)$/.exec(input.value);
    if (command) {
      const word = command[1].toLowerCase();
      return state.commands.filter((item) => item.name.startsWith(word)).slice(0, 8)
        .map((item) => ({ kind: "command", item }));
    }
    const at = /(?:^|\s)@(\w*)$/.exec(input.value.slice(0, input.selectionStart ?? input.value.length));
    if (at) {
      const word = at[1].toLowerCase();
      return membersOf(chatOf(state.current)).filter((name) => name.toLowerCase().startsWith(word))
        .map((name) => ({ kind: "mention", item: name }));
    }
    return [];
  }
  function renderPalette() {
    const palette = $("#palette");
    const matches = suggestions();
    palette.replaceChildren();
    if (!matches.length) { palette.hidden = true; return; }
    state.paletteIndex = Math.min(state.paletteIndex, matches.length - 1);
    matches.forEach((match, index) => {
      const option = make("div", "option" + (index === state.paletteIndex ? " is-active" : ""));
      option.setAttribute("role", "option");
      if (match.kind === "command") {
        const usage = match.item.usage || match.item.name;
        option.append(make("span", "option-usage", usage.startsWith("$") ? usage : `$${usage}`),
          make("span", "option-description", match.item.description));
      } else {
        option.classList.add("option-person");
        option.append(avatar(match.item), make("span", "option-name", match.item),
          make("span", "option-description", "ask them directly"));
      }
      option.onmousedown = (event) => { event.preventDefault(); choose(match); };
      palette.append(option);
    });
    palette.hidden = false;
  }
  function choose(match) {
    closePalette();
    if (match.kind === "mention") { mention(match.item); return; }
    input.value = `$${match.item.name} `;
    input.focus();
  }
  function closePalette() { $("#palette").hidden = true; state.paletteIndex = 0; }

  input.addEventListener("input", () => { grow(); state.paletteIndex = 0; renderPalette(); });
  input.addEventListener("click", renderPalette);
  input.addEventListener("keydown", (event) => {
    const matches = $("#palette").hidden ? [] : suggestions();
    if (matches.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      state.paletteIndex = (state.paletteIndex + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
      renderPalette();
    } else if (matches.length && (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey))) {
      event.preventDefault();
      choose(matches[state.paletteIndex]);
    } else if (event.key === "Escape") {
      closePalette();
    } else if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !touch) {
      event.preventDefault();
      submit();
    }
  });
  $("#composer").addEventListener("submit", (event) => { event.preventDefault(); submit(); });

  // ---- attachments ---------------------------------------------------------------------------------
  function renderAttachments() {
    const area = $("#attachments");
    area.replaceChildren();
    for (const file of state.files) {
      const chip = make("span", "chip" + (file.id ? "" : " is-uploading"), file.name);
      const remove = make("button", "chip-remove", "×");
      remove.type = "button";
      remove.onclick = () => { state.files = state.files.filter((item) => item !== file); renderAttachments(); };
      chip.append(remove);
      area.append(chip);
    }
  }
  async function upload(list) {
    for (const file of list) {
      const entry = { name: file.name || "pasted.png", id: null };
      state.files.push(entry);
      renderAttachments();
      const form = new FormData();
      form.append("file", file, entry.name);
      try {
        const response = await fetch("api/upload", { method: "POST", body: form });
        if (!response.ok) throw new Error(response.status === 413 ? "too large" : String(response.status));
        const { files } = await response.json();
        Object.assign(entry, files[0]);
      } catch (error) {
        state.files = state.files.filter((item) => item !== entry);
        toast(`Could not attach ${entry.name}: ${error.message}`);
      }
      renderAttachments();
    }
  }
  $("#file-input").addEventListener("change", (event) => { upload([...event.target.files]); event.target.value = ""; });
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) { event.preventDefault(); upload(files); }
  });
  const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;  // dragging a chat in the list, not a file
    event.preventDefault();
    document.body.classList.add("is-dragging");
  });
  document.addEventListener("dragleave", (event) => {
    if (!event.relatedTarget) document.body.classList.remove("is-dragging");
  });
  document.addEventListener("drop", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    document.body.classList.remove("is-dragging");
    if (event.dataTransfer?.files?.length) upload([...event.dataTransfer.files]);
  });

  // ---- the rest ------------------------------------------------------------------------------------
  function view(url) {
    const viewer = $("#viewer");
    viewer.querySelector("img").src = url;
    viewer.hidden = false;
  }
  $("#viewer").onclick = () => { $("#viewer").hidden = true; };
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    $("#viewer").hidden = true;
    hidePicker();
    hideMenu();
    hideModelMenu();
    closePanels();
  });
  document.addEventListener("click", (event) => {
    if (!$("#chat-menu").hidden && !event.target.closest("#chat-menu, .chat-more")) hideMenu();
    if (!$("#model-menu").hidden && !event.target.closest("#model-menu, .member-model")) hideModelMenu();
  });

  messages.addEventListener("click", (event) => {
    const button = event.target.closest(".copy");
    if (!button) return;
    const code = button.closest("pre").querySelector("code").textContent;
    const done = () => { button.textContent = "Copied"; setTimeout(() => { button.textContent = "Copy"; }, 1500); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(code).then(done);
    } else {
      const area = make("textarea");
      area.value = code;
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      done();
    }
  });

  // ---- new chats: pick who is in it (one classmate: a direct message with her) -----------------------
  function startChat(personas) {
    if (personas.length === 1) {
      const existing = state.conversations.find((chat) => isDirect(chat) && chat.personas[0] === personas[0]);
      if (existing) { open(existing.id); return; }
    }
    const everyone = personas.length === state.roster.length;
    const id = `${personas.length === 1 ? "dm" : "chat"}-${Date.now().toString(36)}`;
    const title = personas.length === 1 ? displayOf(personas[0]) : "New chat";
    state.conversations.unshift({ id, title, personas: everyone ? null : personas });  // shown at once; the
    transmit({ type: "new", conversation: id, title, ...(everyone ? {} : { personas }) });  // server's list follows
    open(id);
  }
  function showPicker() {
    const list = $("#picker-list");
    list.replaceChildren();
    for (const { name, display } of state.roster) {
      const option = paint(make("label", "picker-option"), keyOf(display));
      const box = make("input");
      box.type = "checkbox";
      box.value = name;
      box.checked = true;
      box.onchange = pickerChanged;
      option.append(box, avatar(display), make("span", "picker-name", display));
      list.append(option);
    }
    pickerChanged();
    $("#picker").hidden = false;
    $("#picker-start").focus();
  }
  const picked = () => [...document.querySelectorAll("#picker-list input:checked")].map((box) => box.value);
  function pickerChanged() {
    const chosen = picked();
    $("#picker-start").disabled = !chosen.length;
    $("#picker-start").textContent = !chosen.length ? "Pick someone" : chosen.length === 1
      ? `Message ${displayOf(chosen[0])}` : chosen.length === state.roster.length ? "Start with everyone"
        : `Start with ${chosen.length}`;
  }
  const hidePicker = () => { $("#picker").hidden = true; };
  $("#picker-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const chosen = picked();
    if (!chosen.length) return;
    hidePicker();
    startChat(chosen);
    if (level() === "narrow") setPanel("sidebar", false);
  });
  $("#picker-cancel").onclick = hidePicker;
  $("#picker").addEventListener("click", (event) => { if (event.target.id === "picker") hidePicker(); });
  $("#new-chat").onclick = showPicker;
  $("#chat-title").onclick = () => {
    const chat = chatOf(state.current);
    if (isDirect(chat)) return;
    rename(chat || { id: state.current, title: "" });
  };
  $("#chat-search").addEventListener("input", (event) => { state.search = event.target.value; renderChats(); });
  $("#toggle-sidebar").onclick = () => setPanel("sidebar", !isOpen("sidebar"));
  $("#close-sidebar").onclick = () => setPanel("sidebar", false);
  $("#toggle-members").onclick = () => setPanel("members", !isOpen("members"));
  $("#close-members").onclick = () => setPanel("members", false);
  $("#scrim").onclick = closePanels;
  for (const query of Object.values(media)) query.addEventListener("change", (event) => event.matches && applyLevel());
  window.addEventListener("resize", fitWidths);
  resizable($("#resize-sidebar"), "sidebar");
  resizable($("#resize-members"), "members");
  applyLevel();
  setInterval(renderTyping, 60000);

  // ---- the phone keyboard ----------------------------------------------------------------------------
  // The app is sized to the visual viewport (what is actually visible): when the keyboard opens it shrinks,
  // so the header and composer stay on screen and the newest message stays in view. iOS would otherwise
  // slide the whole page up behind the keyboard; Android also resizes (interactive-widget in index.html).
  // Whether the chat was at its newest message, kept from the last scroll: by the time a resize is reported
  // the chat has already shrunk, so asking then would say no.
  let pinned = true;
  messages.addEventListener("scroll", () => { pinned = nearBottom(); }, { passive: true });
  function fitViewport() {
    const view = window.visualViewport;
    if (!view) return;
    const stick = pinned;
    const root = document.documentElement.style;
    root.setProperty("--app-height", `${Math.round(view.height)}px`);
    root.setProperty("--app-top", `${Math.round(view.offsetTop)}px`);
    document.body.classList.toggle("keyboard-open", touch && document.activeElement === input);
    if (stick) scrollDown();
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitViewport);
    window.visualViewport.addEventListener("scroll", fitViewport);
  }
  window.addEventListener("resize", fitViewport);  // Android resizes the window itself (resizes-content)
  input.addEventListener("focus", () => setTimeout(fitViewport, 50));
  input.addEventListener("blur", () => setTimeout(fitViewport, 50));
  if (touch) {
    input.setAttribute("enterkeyhint", "enter");
    // Tapping ➤ would move focus to the button and close the keyboard between messages: keep it on the input.
    $("#send").addEventListener("pointerdown", (event) => { if (document.activeElement === input) event.preventDefault(); });
  }
  fitViewport();

  renderChats();
  connect();
})();
