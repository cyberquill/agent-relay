// agent-relay PWA. No build step, and no HTML parsing: every value from the relay goes in via textContent.
"use strict";

const TOKEN_KEY = "agent-relay.token";
const TOP = "";
const state = { current: TOP, threads: new Map([[TOP, { root: "Top level", items: [] }]]), live: new Map() };
const $ = (id) => document.getElementById(id);

function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }

async function api(path, body) {
  const init = { headers: { Authorization: `Bearer ${token()}` } };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (response.status === 401) { logout("The token was rejected."); throw new Error("unauthorized"); }
  if (!response.ok) { throw new Error(`${response.status} ${(await response.text()).slice(0, 200)}`); }
  return response.json();
}

// SSE over fetch, so the token travels in a header and never in a URL.
async function listen() {
  const decoder = new TextDecoder();
  while (token()) {
    try {
      const response = await fetch("/api/events", { headers: { Authorization: `Bearer ${token()}` } });
      if (response.status === 401) { logout("The token was rejected."); return; }
      if (!response.ok || !response.body) { throw new Error(String(response.status)); }
      setOnline(true);
      await refreshSessions();
      const reader = response.body.getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) { break; }
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const data = frame.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          if (data) { onPayload(JSON.parse(data)); }
        }
      }
    } catch (error) {
      console.warn("event stream dropped", error);
    }
    setOnline(false);
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

function setOnline(on) {
  $("status").classList.toggle("on", on);
  $("status").title = on ? "connected" : "reconnecting";
}

function thread(key) {
  const id = key || TOP;
  if (!state.threads.has(id)) { state.threads.set(id, { root: id, items: [] }); }
  return state.threads.get(id);
}

function push(key, item) {
  thread(key).items.push(item);
  if ((key || TOP) === state.current) { $("messages").append(renderItem(item)); scrollDown(); }
  else { renderThreads(); }
}

function onPayload(payload) {
  if (payload.kind === "message") {
    push(payload.session, { type: "message", markdown: payload.markdown, card: payload.card, file: payload.file });
  } else if (payload.kind === "session") {
    thread(payload.session).root = payload.root;
    renderThreads();
  } else if (payload.kind === "event") {
    onEvent(payload.event);
  }
}

function onEvent(event) {
  const key = event.session_key;
  switch (event.type) {
    case "text.delta": {
      const text = (state.live.get(key) || "") + event.text;
      state.live.set(key, text);
      if (key === state.current) { renderLive(); }
      break;
    }
    case "turn.completed":
      state.live.delete(key);
      if (event.text) { push(key, { type: "message", markdown: event.text }); }
      else if (key === state.current) { renderLive(); }
      break;
    case "tool.started":
      push(key, { type: "tool", text: `▶ ${event.name} ${event.detail || ""}` });
      break;
    case "tool.finished":
      push(key, { type: "tool", text: `${event.ok ? "✓" : "✗"} ${event.name} ${event.summary || ""}` });
      break;
    case "session.started":
      thread(key).root = `${event.harness} · ${event.workspace || ""}`;
      refreshSessions();
      break;
    case "error":
      push(key, { type: "tool", text: `⚠️ ${event.message}` });
      break;
    default:
      break;
  }
}

// rendering

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

const OUTCOME_NOTE = {
  already_answered: "Already answered.",
  expired: "This card has expired or was already answered.",
  failed: "That did not go through; see the reply above.",
};

// Adaptive Cards 1.3, drawn generically. `root` is one drawn card (its box and elements by id); `scope`
// holds the inputs of one card level, so a nested Action.ShowCard sees its parent's inputs as well as its own.

let groupSeq = 0;

const list = (value) => (Array.isArray(value) ? value : []);
const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const note = (text) => el("div", "subtle note", text);

function renderCard(card) {
  const box = el("div", "card");
  try {
    fillCard(box, card, { box, byId: new Map(), busy: false }, { inputs: new Map(), parent: null });
  } catch (error) {
    console.warn("card could not be drawn", error);
    box.append(note("This card could not be drawn."));
  }
  return box;
}

function fillCard(into, card, root, scope) {
  drawItems(into, card.body, root, scope);
  if (list(card.actions).length) { into.append(actionRow(card.actions, root, scope)); }
}

function drawItems(into, items, root, scope) {
  for (const block of list(items)) {
    let node;
    try { node = drawElement(block, root, scope); }
    catch (error) {
      console.warn("card element could not be drawn", error);
      node = note(`Could not draw ${isObject(block) ? block.type : "an element"}.`);
    }
    into.append(node);
  }
}

function drawElement(block, root, scope) {
  if (!isObject(block)) { return note("Unsupported element."); }
  const node = drawKnown(block, root, scope);
  if (block.id !== undefined && block.id !== null) { root.byId.set(String(block.id), node); }
  if (block.isVisible === false) { node.hidden = true; }
  return node;
}

function drawKnown(block, root, scope) {
  switch (block.type) {
    case "TextBlock": return drawText(block);
    case "Container": return drawContainer(block, "container", root, scope);
    case "Column": return drawColumn(block, root, scope);
    case "ColumnSet": {
      const set = el("div", "columns");
      drawItems(set, block.columns, root, scope);
      return set;
    }
    case "FactSet": return drawFacts(block);
    case "ActionSet": return actionRow(block.actions, root, scope);
    case "Input.Text": return drawTextInput(block, scope);
    case "Input.ChoiceSet": return drawChoiceSet(block, scope);
    case "Input.Toggle": return drawToggle(block, scope);
    default: return note(`Unsupported element: ${block.type}`);
  }
}

function drawText(block) {
  const classes = [
    block.weight === "Bolder" ? "bold" : "",
    block.isSubtle ? "subtle" : "",
    block.fontType === "Monospace" ? "mono" : "",
    block.wrap === false ? "nowrap" : "",
  ];
  return el("div", classes.filter(Boolean).join(" "), String(block.text ?? ""));
}

function drawContainer(block, className, root, scope) {
  const box = el("div", className);
  drawItems(box, block.items, root, scope);
  return box;
}

function drawColumn(block, root, scope) {
  const column = drawContainer(block, "column", root, scope);
  const width = block.width;
  if (width === "auto") { column.classList.add("auto"); }
  else if (typeof width === "number" && width > 0) { column.style.flex = `${width} 1 0`; }
  else if (typeof width === "string" && /^\d+px$/.test(width)) { column.style.flex = `0 0 ${width}`; }
  return column;
}

function drawFacts(block) {
  const facts = el("div", "facts");
  for (const fact of list(block.facts)) {
    if (!isObject(fact)) { continue; }
    facts.append(el("span", "bold", String(fact.title ?? "")), el("span", "", String(fact.value ?? "")));
  }
  return facts;
}

// inputs: each registers a reader that returns the string a submit carries for its id

function field(block, scope, control, read) {
  const wrap = el("div", "field");
  if (block.label) {
    wrap.append(el("div", "bold", String(block.label)));
    control.setAttribute("aria-label", String(block.label));
  }
  wrap.append(control);
  if (block.id !== undefined && block.id !== null) { scope.inputs.set(String(block.id), read); }
  return wrap;
}

function drawTextInput(block, scope) {
  const multiline = block.isMultiline === true;
  const input = el(multiline ? "textarea" : "input");
  if (!multiline) { input.type = "text"; }
  input.placeholder = String(block.placeholder || "");
  if (Number(block.maxLength) > 0) { input.maxLength = Number(block.maxLength); }
  input.value = String(block.value ?? "");
  return field(block, scope, input, () => input.value);
}

function choiceOf(raw) {
  const choice = isObject(raw) ? raw : { title: raw, value: raw };
  return { title: String(choice.title ?? choice.value ?? ""), value: String(choice.value ?? "") };
}

function drawChoiceSet(block, scope) {
  const choices = list(block.choices).map(choiceOf);
  const wanted = String(block.value ?? "");
  if (block.isMultiSelect === true) { return drawChecks(block, scope, choices, wanted.split(",")); }
  if (block.style === "expanded") { return drawRadios(block, scope, choices, wanted); }
  const select = el("select");
  if (!choices.some((choice) => choice.value === wanted)) {
    const blank = el("option", "", String(block.placeholder || ""));
    blank.value = "";
    select.append(blank);
  }
  for (const choice of choices) {
    const option = el("option", "", choice.title);
    option.value = choice.value;
    select.append(option);
  }
  select.value = wanted;
  return field(block, scope, select, () => select.value);
}

function drawChecks(block, scope, choices, wanted) {
  const group = el("div", "choices");
  const boxes = choices.map((choice) => {
    const box = el("input");
    box.type = "checkbox";
    box.value = choice.value;
    box.checked = wanted.includes(choice.value);
    const label = el("label", "choice");
    label.append(box, el("span", "", choice.title));
    group.append(label);
    return box;
  });
  return field(block, scope, group, () => boxes.filter((b) => b.checked).map((b) => b.value).join(","));
}

function drawRadios(block, scope, choices, wanted) {
  const group = el("div", "choices");
  const name = `choice-${++groupSeq}`;
  const radios = choices.map((choice) => {
    const radio = el("input");
    radio.type = "radio";
    radio.name = name;
    radio.value = choice.value;
    radio.checked = choice.value === wanted;
    const label = el("label", "choice");
    label.append(radio, el("span", "", choice.title));
    group.append(label);
    return radio;
  });
  return field(block, scope, group, () => (radios.find((r) => r.checked) || { value: "" }).value);
}

function drawToggle(block, scope) {
  const on = String(block.valueOn ?? "true");
  const off = String(block.valueOff ?? "false");
  const box = el("input");
  box.type = "checkbox";
  box.checked = String(block.value ?? off) === on;
  const label = el("label", "choice");
  label.append(box, el("span", "", String(block.title ?? "")));
  return field(block, scope, label, () => (box.checked ? on : off));
}

// actions

function actionRow(actions, root, scope) {
  const wrap = el("div", "actionset");
  const row = el("div", "actions");
  const host = el("div", "showcards");
  const panels = [];
  for (const action of list(actions)) {
    try { row.append(drawAction(action, { root, scope, host, panels })); }
    catch (error) {
      console.warn("card action could not be drawn", error);
      row.append(note("Could not draw an action."));
    }
  }
  wrap.append(row, host);
  return wrap;
}

function drawAction(action, group) {
  if (!isObject(action)) { return note("Unsupported action."); }
  switch (action.type) {
    case "Action.Submit": return actionButton(action, "Submit", () => submitCard(action, group.root, group.scope));
    case "Action.ShowCard": return showCardButton(action, group);
    case "Action.ToggleVisibility":
      return actionButton(action, "Toggle", () => toggleVisibility(action, group.root));
    default: return note(`Unsupported action: ${action.type}`);
  }
}

function actionButton(action, fallback, onClick) {
  const button = el("button", action.style === "positive" || action.style === "destructive" ? action.style : "", String(action.title || fallback));
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

// One sub-card open at a time per row; it is drawn once and shown or hidden.
function showCardButton(action, group) {
  const panel = el("div", "showcard");
  panel.hidden = true;
  fillCard(panel, isObject(action.card) ? action.card : {}, group.root, { inputs: new Map(), parent: group.scope });
  group.host.append(panel);
  group.panels.push(panel);
  const button = actionButton(action, "Show", () => {
    const open = panel.hidden;
    for (const other of group.panels) { other.hidden = true; }
    panel.hidden = !open;
  });
  return button;
}

function toggleVisibility(action, root) {
  for (const target of list(action.targetElements)) {
    const id = String(isObject(target) ? target.elementId : target);
    const node = root.byId.get(id);
    if (!node) { continue; }
    node.hidden = isObject(target) && typeof target.isVisible === "boolean" ? !target.isVisible : !node.hidden;
  }
}

async function submitCard(action, root, scope) {
  if (root.busy) { return; }
  // The submit data carries the card's one-use `nonce`; the server refuses a submit without it.
  const values = { ...(action.data || {}) };
  const levels = [];
  for (let level = scope; level; level = level.parent) { levels.unshift(level); }
  for (const level of levels) {
    for (const [id, read] of level.inputs) { values[id] = read(); }
  }
  if (isObject(action.data) && "nonce" in action.data) { values.nonce = action.data.nonce; }
  root.busy = true;
  root.box.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const reply = await api("/api/card", { inputs: values });
    if (OUTCOME_NOTE[reply.outcome]) { root.box.append(el("div", "subtle", OUTCOME_NOTE[reply.outcome])); }
  } catch (error) {
    root.box.append(el("div", "error", `Failed: ${error.message}`));
    root.box.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    root.busy = false;
  }
}

function renderItem(item) {
  const li = el("li", item.type === "tool" ? "tool" : item.mine ? "mine" : "");
  if (item.type === "tool") { li.textContent = item.text; return li; }
  if (item.markdown) { li.append(el("div", "", item.markdown)); }
  if (item.card) { li.append(renderCard(item.card)); }
  if (item.file) {
    li.append(el("div", "subtle", `📎 ${item.file.name}`));
    li.append(el("pre", "file", item.file.text));
  }
  return li;
}

function renderLive() {
  const list = $("messages");
  let live = list.querySelector("li.live");
  const text = state.live.get(state.current);
  if (!text) { if (live) { live.remove(); } return; }
  if (!live) { live = el("li", "live"); list.append(live); }
  live.textContent = text;
  scrollDown();
}

function renderThreads() {
  const list = $("thread-list");
  list.replaceChildren();
  for (const [key, entry] of state.threads) {
    const li = el("li", key === state.current ? "current" : "", entry.root.replaceAll("**", ""));
    li.addEventListener("click", () => select(key));
    list.append(li);
  }
}

function select(key) {
  state.current = key;
  const entry = thread(key);
  $("thread-title").textContent = entry.root.replaceAll("**", "");
  $("messages").replaceChildren(...entry.items.map(renderItem));
  renderLive();
  renderThreads();
  scrollDown();
}

function scrollDown() { const list = $("messages"); list.scrollTop = list.scrollHeight; }

async function refreshSessions() {
  for (const session of await api("/api/sessions")) { thread(session.session).root = session.root; }
  renderThreads();
}

// login and composer

function logout(reason) {
  sessionStorage.removeItem(TOKEN_KEY);
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").hidden = !reason;
  $("login-error").textContent = reason || "";
}

function start() {
  $("login").hidden = true;
  $("app").hidden = false;
  select(TOP);
  listen();
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sessionStorage.setItem(TOKEN_KEY, $("token").value.trim());
    $("token").value = "";
    start();
  });
  $("logout").addEventListener("click", () => logout(""));
  $("composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("text").value.trim();
    if (!text) { return; }
    push(state.current, { type: "message", markdown: text, mine: true });
    $("text").value = "";
    try { await api("/api/send", { text, session: state.current || null }); }
    catch (error) { push(state.current, { type: "tool", text: `⚠️ ${error.message}` }); }
  });
  if (token()) { start(); } else { logout(""); }
});
