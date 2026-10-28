"use strict";
// Your page (/me): what the students call you (for all of them, or one: `$callme` through api/callme), your accounts
// (another one joins with a code: `$link` through api/link), and this browser's settings (prefs.js): theme, the
// pages' language, reading replies aloud (voice.js), and the day's stamina total. The data is `you()` of the web UI.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const t = (text, values) => (window.t ? window.t(text, values) : String(text).replace(/\{(\w+)\}/g, (all, key) => values?.[key] ?? all));
  const state = { data: null, editing: "" };

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  function button(text, onclick, quiet = false) {
    const node = make("button", `me-button${quiet ? " is-quiet" : ""}`, text);
    node.type = "button";
    node.onclick = onclick;
    return node;
  }
  function card(title, intro, ...children) {
    const node = make("section", "me-card");
    node.append(make("h2", "", title));
    if (intro) node.append(make("p", "", intro));
    node.append(...children);
    return node;
  }
  function choice(label, options, current, pick) {
    const group = make("div", "me-choice");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", label);
    for (const [value, text] of options) {
      const option = make("button", "", text);
      option.type = "button";
      option.setAttribute("aria-pressed", String(value === current));
      option.onclick = () => pick(value);
      group.append(option);
    }
    return group;
  }
  let toastTimer = 0;
  function toast(text) {
    const node = $("#toast");
    node.textContent = text;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
  }
  async function post(path, body) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(body || {}) });
    if (response.status === 401) { location.href = "login"; return null; }
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  // ---- what the students call you ---------------------------------------------------------------------------------
  const WHY = { asked: "you asked her", setting: "her setting", everyone: "for everyone", here: "your name here" };
  function whyOf(student, data) {
    if (student.asked) return "asked";
    if (student.called && student.called === student.setting) return "setting";
    if (student.called && student.called === data.everyone) return "everyone";
    return "here";
  }
  async function callMe(persona, name) {
    try {
      const answer = await post("api/callme", { persona, name });
      if (!answer) return;
      toast(answer.text || (answer.ok ? t("Saved.") : t("Could not save it.")));
      if (answer.you) show(answer.you);
    } catch {
      toast(t("Could not reach the page's server."));
    }
  }
  function namesCard(data) {
    const field = make("input");
    field.type = "text";
    field.maxLength = 40;
    field.value = data.everyone || "";
    field.placeholder = data.user_name;
    field.setAttribute("aria-label", t("What every student calls you"));
    const row = make("form", "me-row");
    row.append(field, button(t("Save"), null));
    row.lastChild.type = "submit";
    if (data.everyone) row.append(button(t("Use my name here"), () => callMe("", "reset"), true));
    row.onsubmit = (event) => { event.preventDefault(); if (field.value.trim()) callMe("", field.value.trim()); };
    const list = make("div", "me-students");
    for (const student of data.students) {
      const item = make("div", "me-student");
      const key = student.avatar;
      let face;
      if (state.avatars.has(key)) { face = make("img"); face.src = `avatars/${encodeURIComponent(key)}`; face.alt = ""; }
      else face = make("span", "me-initial", [...student.display][0] || "?");
      const text = make("div");
      const name = make("div", "me-student-name", student.display);
      name.setAttribute("data-no-i18n", "");
      const line = make("div", "me-student-line");
      const why = whyOf(student, data);
      line.append(t("calls you {name}", { name: student.called || data.user_name }), make("span", "me-why", t(WHY[why])));
      if (student.role) line.append(make("span", "me-why", t(student.role)));
      text.append(name, line);
      const edit = button(t("Change"), () => { state.editing = state.editing === student.name ? "" : student.name; render(); }, true);
      item.append(face, text, edit);
      if (state.editing === student.name) {
        const form = make("form");
        const own = make("input");
        own.type = "text";
        own.maxLength = 40;
        own.value = student.asked || "";
        own.placeholder = t("What {name} calls you", { name: student.display });
        form.append(own, button(t("Save"), null));
        form.lastChild.type = "submit";
        if (student.asked) form.append(button(t("Back to the usual"), () => { state.editing = ""; callMe(student.name, "reset"); }, true));
        form.onsubmit = (event) => {
          event.preventDefault();
          if (own.value.trim()) { state.editing = ""; callMe(student.name, own.value.trim()); }
        };
        item.append(form);
        setTimeout(() => own.focus(), 0);
      }
      list.append(item);
    }
    const note = make("p", "me-note", t("Her own name for you comes first, then a setting of hers (call_owner in the " +
      "students' settings), then the name you give everyone, then your name here."));
    return card(t("What the students call you"), t("They use it when they talk with you and in what they remember of you."),
                row, list, note);
  }

  // ---- this browser: theme, language, voice -----------------------------------------------------------------------
  function lookCard() {
    const theme = choice(t("Theme"), [["system", t("System")], ["light", t("Light")], ["dark", t("Dark")]],
                         window.prefs.get("theme"), (value) => { window.prefs.set("theme", value); render(); });
    const languages = [["auto", t("Same as the browser")], ["en", "English"], ["zh", "中文"]];
    const language = choice(t("Language"), languages, window.prefs.get("lang"), (value) => {
      window.prefs.set("lang", value);
      location.reload();  // (the pages translate as they load)
    });
    language.setAttribute("data-no-i18n", "");
    const both = make("div", "me-row");
    both.append(theme);
    return [
      card(t("Theme"), t("For this browser, on every page here and in the Classroom."), both),
      card(t("Language"), t("The pages' own words. The students answer in the language you write to them in."), language),
    ];
  }
  function voiceCard() {
    const voice = window.studentVoice;
    const label = make("label", "me-switch");
    const box = make("input");
    box.type = "checkbox";
    box.checked = Boolean(window.prefs.get("voice"));
    box.disabled = !voice?.available;
    box.onchange = () => { window.prefs.set("voice", box.checked); if (!box.checked) voice?.stop(); };
    label.append(box, t("Read the students' replies aloud in MomoChat"));
    const range = make("label", "me-range");
    const speed = make("input");
    speed.type = "range";
    speed.min = "0.6";
    speed.max = "1.6";
    speed.step = "0.1";
    speed.value = String(window.prefs.get("rate") || 1);
    const shown = make("span", "", `${Number(speed.value).toFixed(1)}×`);
    speed.oninput = () => { shown.textContent = `${Number(speed.value).toFixed(1)}×`; };
    speed.onchange = () => window.prefs.set("rate", Number(speed.value));
    range.append(t("Speed"), speed, shown);
    const first = state.data?.students?.[0];
    const sample = window.uiLanguage === "zh" ? "你好呀，今天也一起加油吧！" : "Hi! Shall we get some work done together today?";
    const row = make("div", "me-row");
    row.append(button(t("Try it"), () => voice?.speak(first?.name || "student", sample, { force: true }), true));
    row.style.marginTop = "12px";
    const note = make("p", "me-note", voice?.available
      ? t("This device's own voices: free, and nothing is sent anywhere. Each student has a pitch of her own.")
      : t("This browser cannot speak (no speech voices)."));
    return card(t("Voice"), "", label, range, row, note);
  }

  // ---- stamina and accounts -----------------------------------------------------------------------------------------
  function staminaCard(data) {
    const today = data.stamina?.today;
    const field = make("input");
    field.type = "number";
    field.min = "0";
    field.step = "0.10";
    field.value = today?.allowance != null ? String(today.allowance) : "";
    field.placeholder = "2.00";
    field.setAttribute("aria-label", t("Today's total for everyone, in dollars"));
    const row = make("form", "me-row");
    row.append(make("span", "", "$"), field, button(t("Set"), null));
    row.lastChild.type = "submit";
    row.onsubmit = async (event) => {
      event.preventDefault();
      try {
        const answer = await post("api/stamina", { total: Math.max(0, parseFloat(field.value) || 0) });
        if (answer) { toast(answer.text || t("Saved.")); state.data.stamina = answer.total; show(state.data); }
      } catch { toast(t("Could not reach the page's server.")); }
    };
    const spent = today ? t("Spent today: {spent}", { spent: window.staminaOf?.({ today })?.amount?.(today.spent) ?? `$${today.spent}` }) : "";
    const link = make("a", "", t("Each student's allowance and her account: the Students page"));
    link.href = "students";
    const note = make("p", "me-note");
    note.append(link);
    const intro = make("p", "", t("Everyone's spending each day, all students and agents together. At the total, work " +
                                  "stops and asks you (0: no total)."));
    if (spent) intro.append(make("span", "me-why", spent));
    return card(t("Stamina"), "", intro, row, note);
  }
  function accountsCard(data) {
    const chips = make("div", "me-chips");
    for (const account of data.accounts) chips.append(make("span", "me-chip", account));
    chips.setAttribute("data-no-i18n", "");
    const out = make("div");
    const row = make("div", "me-row");
    row.style.marginTop = "12px";
    row.append(button(t("Link another account"), async () => {
      try {
        const answer = await post("api/link", {});
        if (!answer) return;
        if (!answer.ok) { toast(answer.text || t("Could not make a code.")); return; }
        const code = make("span", "me-code", answer.code);
        code.setAttribute("data-no-i18n", "");
        const said = make("p", "me-note", t("From the other account (a Discord message to her bot, WeChat, another " +
          "page), send this within ten minutes:"));
        const command = make("p", "");
        command.append(make("span", "me-chip", `$link ${answer.code}`));
        command.lastChild.setAttribute("data-no-i18n", "");
        out.replaceChildren(said, code, command);
      } catch { toast(t("Could not reach the page's server.")); }
    }, true));
    const note = make("p", "me-note", t("Every student keeps one memory of you across them. Who you are to them " +
      "(creator, family, friend) is the owner's setting: roles in the students' settings."));
    return card(t("Your accounts"), t("You here: {account}", { account: data.account }), chips, row, out, note);
  }

  // ---- the page ---------------------------------------------------------------------------------------------------
  function render() {
    const data = state.data;
    if (!data) return;
    const name = data.everyone || data.user_name;
    const hero = make("div", "me-hero");
    const face = make("span", "me-face", [...name.trim()][0]?.toUpperCase() || "?");
    face.setAttribute("aria-hidden", "true");
    const words = make("div");
    const title = make("h1", "", name);
    title.setAttribute("data-no-i18n", "");
    words.append(title, make("p", "", t("Your page: what the students call you, and how these pages look and sound.")));
    hero.append(face, words);
    $("#me-main").replaceChildren(hero, namesCard(data), ...lookCard(), voiceCard(), staminaCard(data), accountsCard(data));
  }
  function show(data) {
    state.data = data;
    state.avatars = new Set(data.avatars || []);
    window.renderPlaces?.($("#places-nav"), data.places || { here: data.here, others: [] }, "You");
    window.renderStaminaTotal?.($("#stamina"), data.stamina, { set: async (total) => {
      try { await post("api/stamina", { total }); } catch { /* shown again below */ }
      refresh();
    } });
    render();
  }
  async function refresh() {
    try {
      const response = await fetch("api/me", { headers: { Accept: "application/json" } });
      if (response.status === 401) { location.href = "login"; return; }
      show(await response.json());
    } catch {
      toast(t("Could not reach the page's server. Reload to try again."));
    }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.editing) refresh(); });
  let given = null;
  try { given = JSON.parse($("#me-data")?.textContent || "null"); } catch { given = null; }
  if (given && typeof given === "object") show(given);
  else refresh();
})();
