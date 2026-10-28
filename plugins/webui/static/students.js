"use strict";
// The Students page: every student's card (portrait, level, bond, models, her measured run alone), and one student in
// full (#<name>): her art stages and moods, level, bond, models, the benchmark rows she is in, your record together and
// her folder. Levels come from the Classroom (Chimera's facts), bond and mood from her memory, both through the core.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  };
  const PLATES = { gpt: "#5a5296", gemini: "#683fa0", claude: "#9a4916", deepseek: "#2f4f9e", grok: "#6f5900" };
  const COMPANIES = { openai: "OpenAI", deepseek: "DeepSeek", claude: "Anthropic", gemini: "Google", grok: "xAI" };
  const MOODS = [["happy", ["great", "good"]], ["calm", ["ok"]], ["worried", ["worry"]], ["sad", ["sad"]]];
  const VERBS = { planner: "plans", builder: "builds", tester: "tests", drafter: "drafts", judge: "judges",
                  strong: "takes over" };
  const ICONS = {  // (constants of this file)
    heart: '<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z" fill="#e0607e"/>',
    lock: '<rect x="5" y="11" width="14" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="2.2"/>' +
          '<path d="M8 11V8a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
    back: '<path d="M15 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
    plus: '<path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };
  function icon(name, size = 15) {
    const holder = make("span", "icon");
    holder.setAttribute("aria-hidden", "true");
    holder.innerHTML = `<svg viewBox="0 0 24 24" width="${size}" height="${size}">${ICONS[name]}</svg>`;
    return holder;
  }
  const store = {  // the chosen filter is this browser's own convenience
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
  };
  const state = { students: [], avatars: new Set(), filter: store.get("students.filter") || "all", places: null,
                  showing: {} };  // the picture each student's portrait shows (a stage or a memory you picked)
  const BOND_OPENS = [30, 60, 85];  // the closeness each bond memory opens at

  // ---- who she is ----------------------------------------------------------------------------------------
  const keyOf = (name) => String(name || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 40);
  const plateOf = (student) => {
    if (PLATES[student.name]) return PLATES[student.name];
    let hash = 0;
    for (const letter of keyOf(student.name)) hash = (hash * 31 + letter.charCodeAt(0)) % 360;
    return `hsl(${hash} 40% 36%)`;
  };
  const shown = (student) => student.alias || student.display;
  const modelName = (id) => String(id || "").split("/").slice(1).join("/") || String(id || "");
  const companyOf = (student) => COMPANIES[String(student.own?.[0] || "").split("/")[0]] || "";
  const byName = (name) => state.students.find((item) => item.name === name);
  const money = (dollars) => `$${dollars < 1 ? dollars.toFixed(3) : dollars.toFixed(2)}`;
  const minutes = (value) => (value == null ? "–" : `${value} min`);
  const level = (student) => student.record || { level: 1, toward: 0, per_level: 3, stage: 1, stages: 4, next_stage_at: 10,
                                                  landed: 0, discarded: 0, open: 0 };
  function portrait(student, className, kind = "card") {
    const kinds = new Set(student.pictures || []);
    const source = kinds.has(kind) ? `pictures/${encodeURIComponent(student.name)}/${kind}`
      : kinds.has("card") ? `pictures/${encodeURIComponent(student.name)}/card`
      : state.avatars.has(student.avatar) ? `avatars/${encodeURIComponent(student.avatar)}` : "";
    if (source) {
      const image = make("img", className);
      image.src = source;
      image.alt = `${student.display}${student.description ? `: ${student.description}` : ""}`;
      return image;
    }
    const initial = make("span", `${className} is-initial`, (student.display || "?").charAt(0).toUpperCase());
    initial.style.color = plateOf(student);
    return initial;
  }
  // Her run alone on a benchmark (one seat, hers), if she has one.
  const aloneRun = (student) => (student.benchmark || []).find((row) => row.formation === "alone" && row.seats.length === 1);
  const pointsOf = (row) => `${(100 * row.points / Math.max(1, row.total)).toFixed(1)}%`;
  function seatLabel(student, row) {  // what she did in that team, and who else did what
    const verbs = (seats) => seats.map((seat) => VERBS[seat] || seat).join(" and ");
    const others = {};
    for (const [seat, holder] of Object.entries(row.team || {})) {
      if (holder !== student.name) (others[holder] ||= []).push(seat);
    }
    const mine = verbs(row.seats);
    if (!Object.keys(others).length) return row.seats.length === 1 ? "Alone" : `Alone, ${mine}`;
    const rest = Object.entries(others).map(([holder, seats]) => `${byName(holder) ? shown(byName(holder)) : holder} ${verbs(seats)}`);
    return `${mine.charAt(0).toUpperCase()}${mine.slice(1)}, ${rest.join(", ")}`;
  }

  // ---- the list ------------------------------------------------------------------------------------------
  function renderList() {
    const main = $("#students-main");
    const all = state.students;
    const own = all.filter((student) => student.own_ready !== false);
    const borrowing = all.filter((student) => student.own_ready === false);
    const names = (list) => list.length <= 1 ? list.map(shown).join("")
      : `${list.slice(0, -1).map(shown).join(", ")} and ${shown(list[list.length - 1])}`;
    const head = make("div", "students-head");
    const heading = make("div", "students-heading");
    heading.append(make("h1", "", "Students"),
                   make("p", "", `${all.length} students. ${own.length ? `${names(own)} work and chat on ${own.length === 1 ? "her" : "their"} own models; ` : ""}` +
                                 `${borrowing.length ? `${names(borrowing)} answer on a classmate's model until you add ${borrowing.length === 1 ? "her company's key" : "their companies' keys"}. ` : ""}` +
                                 "Levels grow in the Classroom, bond grows in MomoChat."));
    const filters = make("div", "segmented");
    filters.setAttribute("role", "group");
    filters.setAttribute("aria-label", "Show");
    for (const [id, label, count] of [["all", "All", all.length], ["own", "Own key", own.length], ["key", "Needs a key", borrowing.length]]) {
      const button = make("button", "", `${label} `);
      button.type = "button";
      button.append(make("span", "count", String(count)));
      button.setAttribute("aria-pressed", String(state.filter === id));
      button.onclick = () => { state.filter = id; store.set("students.filter", id); renderList(); };
      filters.append(button);
    }
    head.append(heading, filters);
    const grid = make("div", "student-grid");
    const shownList = state.filter === "own" ? own : state.filter === "key" ? borrowing : all;
    for (const student of shownList) grid.append(studentCard(student));
    if (state.filter === "all") {
      const add = make("article", "student-add");
      add.append(icon("plus", 48), make("h2", "", "Add a student"),
                 make("p", "", "A student is one folder in extensions/students/: her student.yml (name, models, " +
                               "description), her prompt, her pictures; her memory comes with her."));
      grid.append(add);
    }
    main.replaceChildren(head, grid);
    document.title = `Students · ${state.title || ""}`;
  }

  function studentCard(student) {
    const lacking = student.own_ready === false;
    const card = make("article", `student-card${lacking ? " is-lacking" : ""}`);
    card.style.setProperty("--plate", plateOf(student));
    const top = make("div", "student-portrait");
    top.append(portrait(student, "portrait-image"));
    const record = level(student);
    top.append(make("span", "badge badge-level", `Lv ${record.level}`));
    if (student.bond) {
      const bond = make("span", "badge badge-bond");
      bond.append(icon("heart"), make("span", "", String(student.bond.closeness)));
      bond.title = `${student.bond.role}, closeness ${student.bond.closeness}/100`;
      top.append(bond);
    }
    top.append(make("span", "badge badge-stage", `Stage ${record.stage} of ${record.stages}`));
    if (lacking) top.append(make("span", "badge badge-key", `No ${companyOf(student) || "own"} key`));
    const plate = make("div", "student-plate");
    const nameRow = make("div", "plate-row");
    nameRow.append(make("span", "plate-name", shown(student)));
    if (student.alias) nameRow.append(make("span", "plate-note", "your name for her"));
    const models = [...new Set([student.own?.[0], student.strong?.[0]].filter(Boolean).map(modelName))];  // her first of each
    plate.append(nameRow, make("div", "plate-line", [student.alias ? student.display : "", companyOf(student),
      models.join(" and ")].filter(Boolean).join(" · ")));
    const body = make("div", "student-body");
    if (student.description) body.append(make("p", "student-line student-description", student.description));
    const chips = make("div", "chips");
    chips.append(make("span", "chip chip-cafe", "Cafe"), make("span", "chip chip-momo", `MomoChat · ${student.chats}`));
    const working = classroomNow(student);
    if (working) chips.append(make("span", "chip chip-class", `Classroom · ${working}`));
    body.append(chips);
    if (lacking) body.append(make("p", "student-warn", "Answers on a classmate's model until you add her key."));
    else body.append(...staminaRows(student));
    const run = aloneRun(student);
    if (run) {
      const box = make("div", "bench-box");
      box.append(make("div", "bench-label", `Alone on the benchmark${run.model ? ` (${run.model})` : ""}`),
                 make("div", "bench-main", `${pointsOf(run)} points · ${run.solved} of ${run.exercises} solved`),
                 make("div", "bench-note", `${money(run.session_offpeak)} a session · ${minutes(run.median_min)}`));
      body.append(box);
    }
    const open = make("a", "card-link", "Open her card");
    open.href = `#${encodeURIComponent(student.name)}`;
    body.append(open);
    card.append(top, plate, body);
    return card;
  }
  // Her stamina on her card: her account (the thick bar) and what is left of today (the thin bar, in her colour).
  function staminaRows(student) {
    const view = window.staminaOf?.(student.stamina);
    if (!view || (view.account == null && view.today == null && !view.accountText)) return [];
    const rows = make("div", "stamina-rows");
    const line = (label, text, fraction, kind) => {
      const head = make("div", "stamina-line");
      head.append(make("span", "", label), make("span", "", text));
      const bar = make("div", `stamina-meter is-${kind}`);
      const fill = make("div", "meter-fill");
      fill.style.width = `${Math.round((fraction ?? 0) * 100)}%`;
      bar.append(fill);
      rows.append(head, bar);
    };
    if (view.accountText) line("Stamina", view.accountText, view.account, "account");
    if (view.today != null) line("Today", view.todayText, view.today, "today");
    return [rows];
  }
  function classroomNow(student) {  // what she is doing in the Classroom now, from the places it publishes
    const classroom = (state.places?.others || []).find((place) => place.place?.name === "Classroom");
    const there = (classroom?.people || []).find((person) => person.key === student.name);
    return there ? there.doing || "working" : "";
  }

  // ---- one student ---------------------------------------------------------------------------------------
  function section(title, className = "") {
    const box = make("section", `panel ${className}`.trim());
    box.append(make("h2", "", title));
    return box;
  }
  function meter(fraction, tone) {
    const bar = make("div", `meter-bar meter-${tone}`);
    const fill = make("div", "meter-fill");
    fill.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
    bar.append(fill);
    return bar;
  }
  function renderCard(student) {
    const main = $("#students-main");
    const record = level(student);
    const back = make("a", "back-link");
    back.href = "#";
    back.append(icon("back", 18), make("span", "", "Students"));
    const page = make("div", "student-page");
    page.style.setProperty("--plate", plateOf(student));
    const left = make("div", "student-left");
    const right = make("div", "student-right");

    const hero = make("div", "hero");
    const picture = make("div", "hero-portrait");
    const showing = state.showing[student.name] || "card";  // a stage or memory picked below shows here
    picture.append(portrait(student, "portrait-image", showing), make("span", "badge badge-level big", `Lv ${record.level}`));
    const plate = make("div", "student-plate hero-plate");
    const row = make("div", "plate-row");
    row.append(make("span", "plate-name big", shown(student)));
    const rename = make("button", "plate-button", "Rename");
    rename.type = "button";
    rename.onclick = () => renameHer(student);
    row.append(rename);
    plate.append(row, make("div", "plate-line strong", student.alias ? `${student.display} · the name you gave her, kept by mneme-llm`
                                                                      : `${student.display} · give her a name of your own`));
    if (student.description) plate.append(make("div", "plate-line student-description", student.description));
    hero.append(picture, plate);

    const stages = section("Art stages");
    stages.append(make("p", "panel-note", "Each stage is a new, better illustration. You can show any stage she has unlocked."));
    const strip = make("div", "stage-strip");
    for (let number = 1; number <= record.stages; number++) {
      const opens = [1, 10, 20, 30][number - 1];
      const open = record.stage >= number;
      const kind = number === 1 ? "card" : `stage-${number}`;
      const slot = make("div", `stage${open ? "" : " is-locked"}${showing === kind || (showing === "card" && number === 1) ? " is-current" : ""}`);
      const kinds = new Set(student.pictures || []);
      if (number === 1 || (open && kinds.has(kind))) {
        slot.append(pickable(student, kind, "stage-image", `Stage ${number}`));
      } else if (kinds.has(kind)) {  // drawn, not opened yet: a glimpse, as gacha games tease the next stage
        slot.append(glimpse(student, kind, "stage-image", icon("lock", 20), `Lv ${opens}`));
        slot.title = `Stage ${number}, unlocks at level ${opens}`;
      } else {
        const empty = make("span", "stage-empty");
        if (!open) empty.append(icon("lock", 20));
        empty.append(make("span", "", open ? "to draw" : `Lv ${opens}`));
        slot.append(empty);
        slot.title = open ? `Stage ${number}: unlocked, not drawn yet` : `Stage ${number}, unlocks at level ${opens}`;
      }
      slot.append(make("span", "stage-name", `Stage ${number}`));
      strip.append(slot);
    }
    stages.append(strip);

    const moods = section("Moods");
    moods.querySelector("h2").append(make("span", "panel-aside", " · follow mneme-llm"));
    const faces = make("div", "mood-strip");
    const current = MOODS.find(([, faces]) => faces.includes(student.mood?.face))?.[0];
    for (const [word] of MOODS) {
      const cell = make("div", `mood${word === current ? " is-current" : ""}`);
      const kinds = new Set(student.pictures || []);
      if (kinds.has(`mood-${word}`) || word === current) cell.append(portrait(student, "mood-image", `mood-${word}`));
      else cell.append(make("span", "mood-empty", "to draw"));
      cell.append(make("span", "", word));
      faces.append(cell);
    }
    moods.append(faces);
    const memories = section("Bond memories");
    memories.append(make("p", "panel-note", "Closer, she lets you see her off duty: each picture opens as your bond grows."));
    const shelf = make("div", "memory-strip");
    const closeness = student.bond?.closeness || 0;
    BOND_OPENS.forEach((opens, index) => {
      const kind = `bond-${index + 1}`;
      const kinds = new Set(student.pictures || []);
      const slot = make("div", `stage${closeness >= opens ? "" : " is-locked"}${showing === kind ? " is-current" : ""}`);
      if (!kinds.has(kind)) {
        const empty = make("span", "stage-empty");
        empty.append(make("span", "", `Bond ${opens}`));
        slot.append(empty);
      } else if (closeness >= opens) {
        slot.append(pickable(student, kind, "stage-image", `Bond ${opens}`));
      } else {
        slot.append(glimpse(student, kind, "stage-image", icon("heart", 18), `Bond ${opens}`));
        slot.title = `Opens at bond ${opens}`;
      }
      slot.append(make("span", "stage-name", ["Off duty", "At ease", "A memory"][index]));
      shelf.append(slot);
    });
    memories.append(shelf);
    left.append(hero, stages, moods, memories);

    const trio = make("div", "trio");
    const levelBox = section(`Level `);
    levelBox.querySelector("h2").append(make("span", "big-number", String(record.level)));
    levelBox.append(meter(record.toward / record.per_level, "level"),
                    make("div", "panel-meta", `${record.toward} of ${record.per_level} landed sessions to Lv ${record.level + 1}` +
                                              (record.next_stage_at ? ` · Stage ${record.stage + 1} at Lv ${record.next_stage_at}` : "")),
                    make("p", "panel-note", "She levels up with the Classroom sessions you land. Work you discard gives nothing."));
    const bondBox = section("");
    const bondTitle = bondBox.querySelector("h2");
    bondTitle.append(icon("heart", 20), make("span", "", "Bond "), make("span", "big-number bond", student.bond ? String(student.bond.closeness) : "–"));
    if (student.bond) {
      bondBox.append(meter(student.bond.closeness / 100, "bond"),
                     make("div", "panel-meta", `of 100 · ${student.bond.role}${current ? ` · mood: ${current}` : ""}`));
    } else {
      bondBox.append(make("div", "panel-meta", "Not yet: it starts the first time you talk with her in MomoChat."));
    }
    bondBox.append(make("p", "panel-note", "Grows and falls in MomoChat, as mneme-llm reads how your talks with her go."));
    trio.append(levelBox, bondBox);
    const view = window.staminaOf?.(student.stamina);
    if (view && student.own_ready !== false && (view.accountText || view.today != null)) {
      const staminaBox = section("");
      const title = staminaBox.querySelector("h2");
      const account = student.stamina?.account;
      title.append(make("span", "", "⚡ Stamina "), make("span", "big-number is-stamina",
        account?.left != null ? view.amount(account.left, account.currency) : "–"));
      if (view.account != null) staminaBox.append(meter(view.account, "stamina"));
      staminaBox.append(make("div", "panel-meta", !account ? "No account set up: core.yml stamina: accounts"
        : account.kind === "budget" ? `of the ${view.amount(account.total)} ${companyOf(student)} budget you entered`
        : `in her ${companyOf(student)} account, as ${companyOf(student)} reports it`));
      if (view.today != null) {
        const today = make("div", "panel-meta today-line");
        today.append(make("span", "", "Today"), make("span", "", `${view.todayText} · refills at midnight`));
        staminaBox.append(today, meter(view.today, "today"));
      }
      staminaBox.append(make("p", "panel-note", account?.kind === "budget"
        ? `${companyOf(student)} shows no balance to an ordinary key, so this counts down from what she spends.`
        : "When today's allowance is spent, her work stops and asks you before she spends more."));
      const changes = make("div", "panel-buttons");
      const change = (label, ask, body) => {
        const button = make("button", "small-button", label);
        button.type = "button";
        button.onclick = async () => {
          const value = prompt(ask);
          if (value == null || !value.trim() || !Number.isFinite(parseFloat(value))) return;
          try {
            const response = await fetch("api/stamina", { method: "POST", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify(body(parseFloat(value))) });
            toast((await response.json()).text || "Done.");
          } catch { toast("Not changed."); }
          refresh();
        };
        changes.append(button);
      };
      change("Her daily allowance", `${shown(student)}'s daily allowance, in dollars (0: none)`,
             (value) => ({ persona: student.name, allowance: value }));
      if (account?.kind === "budget" || !account) {
        change("Account budget", `The ${companyOf(student) || "account"} budget, in dollars (counted from today)`,
               (value) => ({ account: account?.provider || String(student.own?.[0] || "").split("/")[0], budget: value }));
      }
      staminaBox.append(changes);
      trio.append(staminaBox);
    }

    const steps = ladderOf(student);
    const models = section(steps.length > 1 ? "Her models, weakest to strongest" : (student.strong || []).length ? "Two models, one student" : "Her model");
    if (steps.length) {
      models.append(make("p", "panel-note", "Each of her models is drawn as she would be on it: a chibi on the lightest, a " +
                                            "high-school or university student on the strongest."));
      const ladderRow = make("div", "ladder");
      const hers = new Set([...(student.own || []), ...(student.strong || [])].map((id) => norm(modelName(id))));
      for (const step of steps) {
        const rung = make("div", `rung${hers.has(step.model) ? " is-hers" : ""}`);
        const image = make("img", "rung-image");
        image.src = `pictures/${encodeURIComponent(student.name)}/${step.kind}`;
        image.alt = `${shown(student)} as ${TIERS[step.tier]}`;
        rung.append(image, make("span", "rung-tier", TIERS[step.tier]), make("span", "rung-model", step.label),
                    make("span", "rung-note", hers.has(step.model) ? "hers here" : "not set up"));
        ladderRow.append(rung);
      }
      models.append(ladderRow);
    } else {
      const list = make("div", "model-list");
      for (const [id, use] of [[student.own?.[0], "Everyday: chatting, building, testing"], [student.strong?.[0], "Strong seats: planning, taking over"]]) {
        if (!id) continue;
        const item = make("div", "soft-box");
        item.append(make("div", "soft-title", modelName(id)), make("div", "soft-note", use));
        list.append(item);
      }
      models.append(list);
    }
    if (student.own_ready === false) models.append(make("p", "student-warn", `No ${companyOf(student) || "own"} key here: she answers on a classmate's model (her fallback) until you add one.`));
    right.append(trio, models);

    const rows = student.benchmark || [];
    if (rows.length) {
      const bench = section("On the benchmark");
      const first = rows[0];
      bench.append(make("p", "panel-note", `${first.benchmark}: ${first.exercises} Python exercises, hidden tests, one run per team, ` +
                                           `${first.date}. Off-peak prices. On ${[...new Set(rows.map((row) => row.model).filter(Boolean))].join(" and ")}.`));
      const scroll = make("div", "table-scroll");
      const table = make("table", "bench-table");
      const headRow = make("tr");
      for (const label of ["Her seat", "Points", "Fully solved", "Cost a session", "Median time"]) {
        const cell = make("th", "", label);
        cell.scope = "col";
        headRow.append(cell);
      }
      const thead = make("thead");
      thead.append(headRow);
      const tbody = make("tbody");
      for (const row_ of rows) {
        const line = make("tr");
        const seat = make("th", "", seatLabel(student, row_));
        seat.scope = "row";
        const team = Object.keys(row_.team || {}).length > 1 && new Set(Object.values(row_.team)).size > 1;
        line.append(seat, make("td", "", pointsOf(row_)), make("td", "", `${row_.solved} of ${row_.exercises}`),
                    make("td", "", `${money(row_.session_offpeak)}${team ? ` (team${row_.share ? `; ${Math.round(row_.share * 100)}% hers` : ""})` : ""}`),
                    make("td", "", minutes(row_.median_min)));
        tbody.append(line);
      }
      table.append(thead, tbody);
      scroll.append(table);
      bench.append(scroll);
      right.append(bench);
    }

    const duo = make("div", "duo");
    const you = section("With you");
    const counts = make("div", "count-row");
    for (const [value, label] of [[record.landed, "landed"], [record.open, "open"], [record.discarded, "discarded"]]) {
      const cell = make("div", "soft-box count");
      cell.append(make("div", "count-number", String(value || 0)), make("div", "soft-note", label));
      counts.append(cell);
    }
    you.append(counts);
    const folder = section("Her folder");
    folder.append(make("pre", "folder", [`students/${student.name}/`, ...(student.folder || []).map((name) => `  ${name}`)].join("\n")));
    duo.append(you, folder);
    right.append(duo);

    page.append(left, right);
    main.replaceChildren(back, page);
    document.title = `${shown(student)} · Students · ${state.title || ""}`;
  }

  // A picture you can show in her portrait (a click), and a glimpse of one not opened yet (blurred, what opens it).
  function pickable(student, kind, className, label) {
    const button = make("button", "pick-picture");
    button.type = "button";
    button.setAttribute("aria-label", `Show ${label}`);
    button.append(portrait(student, className, kind));
    button.onclick = () => { state.showing[student.name] = kind; renderCard(student); };
    return button;
  }
  function glimpse(student, kind, className, mark, text) {
    const holder = make("span", "glimpse");
    const image = portrait(student, `${className} is-glimpse`, kind);
    const over = make("span", "glimpse-over");
    over.append(mark, make("span", "", text));
    holder.append(image, over);
    return holder;
  }
  // Her model ladder (the students module's `ladder-<n>-<tier>-<model>` pictures), weakest first.
  const TIERS = { chibi: "Chibi", chuugakusei: "Middle-schooler", koukousei: "High-schooler", daigakusei: "University student" };
  const norm = (text) => String(text || "").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  function ladderOf(student) {
    return (student.pictures || []).map((kind) => /^ladder-(\d+)-([a-z]+)-(.+)$/.exec(kind)).filter(Boolean)
      .map(([kind, index, tier, model]) => ({ kind, index: Number(index), tier, model,
                                             label: [...(student.own || []), ...(student.strong || [])].map(modelName)
                                               .find((id) => norm(id) === model) || model }))
      .sort((a, b) => a.index - b.index);
  }

  async function renameHer(student) {
    const name = prompt(`What do you call ${student.display}? (“reset” takes your name for her back)`, student.alias || "");
    if (name == null || !name.trim()) return;
    try {
      const response = await fetch("api/alias", { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ persona: student.name, name: name.trim() }) });
      const answer = response.ok ? await response.json() : { ok: false, text: "Not changed." };
      toast(answer.text || (answer.ok ? "Done." : "Not changed."));
    } catch {
      toast("Could not reach the page's server.");
    }
    setTimeout(refresh, 600);  // her facts change a moment after
  }

  // ---- the page ------------------------------------------------------------------------------------------
  function toast(text) {
    const node = $("#toast");
    node.textContent = text;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3600);
  }
  function route() {
    let wanted = "";
    try { wanted = decodeURIComponent(location.hash.slice(1)); } catch { wanted = ""; }
    const student = wanted && byName(wanted);
    if (student) renderCard(student);
    else renderList();
    window.scrollTo(0, 0);
  }
  function show(data) {
    state.title = data.title;
    state.students = data.students || [];
    state.avatars = new Set(data.avatars || []);
    state.places = data.places;
    window.renderPlaces?.($("#places-nav"), data.places || { here: data.here, others: [] }, "Students");
    window.renderStaminaTotal?.($("#stamina"), data.stamina, { set: async (total) => {
      try {
        await fetch("api/stamina", { method: "POST", headers: { "Content-Type": "application/json" },
                                     body: JSON.stringify({ total }) });
      } catch { /* shown again below */ }
      refresh();
    } });
    route();
  }
  async function refresh() {
    try {
      const response = await fetch("api/students", { headers: { Accept: "application/json" } });
      if (response.status === 401) { location.href = "login"; return; }
      show(await response.json());
    } catch {
      toast("Could not reach the page's server. Reload to try again.");
    }
  }
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  let given = null;
  try { given = JSON.parse($("#students-data")?.textContent || "null"); } catch { given = null; }
  if (given && typeof given === "object") show(given);
  else refresh();
})();
