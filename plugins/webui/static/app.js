"use strict";
(() => {
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  // accent: rings, the bubble's strip and glows; ink: names and text; border/bubble (optional): override the
  // strip colour and the bubble's fill.
  const PEOPLE = {
    luna: { accent: "#ffffff", ink: "#7d8898" },
    gemini: { accent: "#9a86f2", ink: "#7a63de" },
    claude: { accent: "#e5896a", ink: "#c4694a" },
    deepseek: { accent: "#4d8ff0", ink: "#3570d2" },
    grok: { accent: "#e2b545", ink: "#a8801c" },
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
  };
  const messages = $("#messages");
  const input = $("#input");
  // A touch screen (a phone's on-screen keyboard): Return makes a new line and ➤ sends, as in phone chat apps.
  const touch = window.matchMedia("(pointer: coarse)").matches;

  // ---- people --------------------------------------------------------------------------------------
  const keyOf = (name) => String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 40);
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
    node.style.setProperty("--cb", colors.border || colors.accent);
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

  // ---- panels: chats on the left, classmates on the right ------------------------------------------
  // Three widths: wide shows both panels, medium the chats only, narrow neither (they slide over the chat
  // there). Either can be shown or hidden at any width; the choice is remembered per width, except on a
  // phone, where both start folded.
  const LEVELS = { wide: "(min-width: 1201px)", medium: "(min-width: 761px) and (max-width: 1200px)",
                   narrow: "(max-width: 760px)" };
  const DEFAULTS = { wide: { sidebar: true, members: true }, medium: { sidebar: true, members: false },
                     narrow: { sidebar: false, members: false } };
  const SIZES = { sidebar: [180, 440, 260], members: [180, 420, 240] };  // min, max, default width
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
  function fitWidths() {
    const shown = { ...wanted };
    const open = Object.keys(shown).filter(docked);
    const spare = window.innerWidth - CHAT_MIN;
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
    const room = window.innerWidth - CHAT_MIN - (docked(other) ? $(`#${other}`).getBoundingClientRect().width : 0);
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

  function renderMembers() {
    const list = $("#member-list");
    list.replaceChildren();
    const typing = new Set();
    for (const event of state.progress.values()) {
      if (event.conversation !== state.current) continue;
      const name = classmateIn(event.text);
      if (name) typing.add(name);
    }
    const chat = chatOf(state.current);
    const members = membersOf(chat);
    $("#members-title").textContent = isDirect(chat) ? "Direct message" : `In this chat — ${members.length}`;
    for (const name of members) {
      const spoke = (state.spoke.get(name) || 0) > Date.now() - 4000;
      const card = paint(make("div", "member-card"), keyOf(name));
      const member = paint(make("button", "member"), keyOf(name));
      member.type = "button";
      member.title = `Mention @${name}`;
      if (typing.has(name)) member.classList.add("is-typing");
      else if (spoke) member.classList.add("is-speaking");
      const face = make("div", "member-face");
      face.append(avatar(name), make("span", "presence"));
      const text = make("div", "member-text");
      text.append(make("div", "member-name", name),
        make("div", "member-status", typing.has(name) ? "typing…" : spoke ? "speaking" : "in class"));
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
  function renderChats() {
    const list = $("#chats");
    list.replaceChildren();
    // Group chats first, then direct messages, each in the order the user arranged.
    const direct = state.conversations.filter(isDirect);
    for (const chat of [...state.conversations.filter((item) => !isDirect(item)), ...direct]) {
      if (chat === direct[0]) list.append(make("div", "chats-subhead", "Direct messages"));
      const item = make("div", "chat" + (chat.id === state.current ? " is-current" : "") + (isDirect(chat) ? " is-direct" : ""));
      item.draggable = true;
      item.dataset.id = chat.id;
      const name = make("button", "chat-name", chat.title || chat.id);
      name.type = "button";
      name.title = chat.personas ? membersOf(chat).join(", ") : "The whole class";
      name.onclick = () => { open(chat.id); if (level() === "narrow") setPanel("sidebar", false); };
      if (isDirect(chat)) name.prepend(avatar(membersOf(chat)[0]));
      item.append(name);
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
    const current = state.conversations.find((chat) => chat.id === state.current);
    $("#chat-title").textContent = current ? current.title : "New chat";
    document.title = `${current ? current.title + " · " : ""}${state.title}`;
    const members = membersOf(current);
    $("#empty-title").textContent = isDirect(current) ? `Just you and ${members[0]}.`
      : current?.personas ? `${members.join(", ")} are here.` : "Everyone's here.";
    input.placeholder = isDirect(current) ? `Message ${members[0]}…` : "Message the class…";
  }

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

  function receive(event) {
    if (event.type === "hello") {
      const s = event.state;
      Object.assign(state, { title: s.title, user: s.user, classmates: s.classmates, commands: s.commands,
                             conversations: s.conversations, avatars: new Set(s.avatars),
                             roster: s.roster || s.classmates.map((name) => ({ name: keyOf(name), display: name })) });
      state.progress = new Map(s.progress.map((item) => [item.run_id, item]));
      $("#brand").textContent = s.title;
      renderMembers();
      const me = $("#me");
      me.replaceChildren(avatar(s.user.name, "you"), make("span", "", s.user.name));
      for (const pending of state.pending.splice(0)) state.ws.send(JSON.stringify(pending));
      if (!state.conversations.some((chat) => chat.id === state.current)) {
        open(s.default);
        return;
      }
      renderChats();
      renderTyping();
      loadHistory();
      askModels();
    } else if (event.type === "models") {
      receiveModels(event);
    } else if (event.type === "message") {
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
    rename(state.conversations.find((chat) => chat.id === state.current) || { id: state.current, title: "" });
  };
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
