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
    case "approval.requested":
      push(key, { type: "message", markdown: "", card: approvalCard(event) });
      break;
    case "question.asked":
      push(key, { type: "message", markdown: "", card: questionCard(event) });
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

function approvalCard(event) {
  const base = { relay: "approval", request_id: event.request_id };
  return {
    body: [{ type: "TextBlock", text: "Approval needed", weight: "Bolder" }, { type: "TextBlock", text: `${event.tool} ${event.detail || ""}` }],
    actions: [
      { type: "Action.Submit", title: "Allow", style: "positive", data: { ...base, decision: "allow" } },
      { type: "Action.Submit", title: "Allow for session", data: { ...base, decision: "allow_session" } },
      { type: "Action.Submit", title: "Deny", style: "destructive", data: { ...base, decision: "deny" } },
    ],
  };
}

function questionCard(event) {
  const base = { relay: "question", request_id: event.request_id };
  const body = [{ type: "TextBlock", text: "Question", weight: "Bolder" }, { type: "TextBlock", text: event.question }];
  if (event.options && event.options.length) {
    body.push({ type: "Input.ChoiceSet", id: "choice", style: "expanded", choices: event.options.map((o) => ({ title: o, value: o })) });
  }
  body.push({ "type": "Input.Text", "id": "custom", "isMultiline": true, "placeholder": "Or type an answer" });
  return {
    body,
    actions: [
      { type: "Action.Submit", title: "Answer", style: "positive", data: { ...base, op: "answer" } },
      { type: "Action.Submit", title: "Skip", data: { ...base, op: "skip" } },
    ],
  };
}

// rendering

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined) { node.textContent = text; }
  return node;
}

function renderCard(card) {
  const box = el("div", "card");
  const inputs = new Map();
  for (const block of card.body || []) {
    if (block.type === "TextBlock") {
      box.append(el("div", [block.weight === "Bolder" ? "bold" : "", block.isSubtle ? "subtle" : ""].join(" "), block.text));
    } else if (block.type === "Input.ChoiceSet") {
      const select = el("select");
      for (const choice of block.choices || []) {
        const option = el("option", "", choice.title);
        option.value = choice.value;
        select.append(option);
      }
      if (block.value) { select.value = block.value; }
      inputs.set(block.id, select);
      box.append(select);
    } else if (block.type === "Input.Text") {
      const input = el("textarea");
      input.placeholder = block.placeholder || "";
      inputs.set(block.id, input);
      box.append(input);
    }
  }
  const actions = el("div", "actions");
  for (const action of card.actions || []) {
    if (action.type !== "Action.Submit") { continue; }
    const button = el("button", action.style || "", action.title);
    button.type = "button";
    button.addEventListener("click", async () => {
      const values = { ...(action.data || {}) };
      for (const [id, input] of inputs) { values[id] = input.value; }
      actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        const reply = await api("/api/card", { inputs: values });
        if (reply.outcome === "already_answered") { box.append(el("div", "subtle", "Already answered.")); }
      } catch (error) {
        box.append(el("div", "error", `Failed: ${error.message}`));
        actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    });
    actions.append(button);
  }
  box.append(actions);
  return box;
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
