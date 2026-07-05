const stream = document.querySelector("#message-stream");
const activity = document.querySelector("#activity-list");
const composer = document.querySelector("#composer");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const socketState = document.querySelector("#socket-state");
const engineState = document.querySelector("#engine-state");
const providerState = document.querySelector("#provider-state");
const approvalState = document.querySelector("#approval-state");

const protocol = window.location.protocol === "https:" ? "wss" : "ws";
const socket = new WebSocket(`${protocol}://${window.location.host}/ws/console/`);
const MAX_ACTIVITY_ITEMS = 40;

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function appendMessage(kind, label, text) {
  const article = document.createElement("article");
  article.className = `message ${kind}`;

  const heading = document.createElement("span");
  heading.className = "message-label";
  heading.textContent = label;

  const body = document.createElement("p");
  body.textContent = text;

  article.append(heading, body);
  stream.append(article);
  stream.scrollTop = stream.scrollHeight;
}

function appendActivity(text, idle = false, detail = "") {
  clearActivityPlaceholder();

  const row = document.createElement("div");
  row.className = "activity-item";

  const dot = document.createElement("span");
  dot.className = idle ? "dot idle" : "dot";

  const label = document.createElement("span");
  label.textContent = text;

  if (detail) {
    const wrapper = document.createElement("div");
    const title = document.createElement("div");
    title.className = "activity-title";
    title.textContent = text;
    const body = document.createElement("div");
    body.className = "activity-detail";
    body.textContent = detail;
    wrapper.append(title, body);
    row.append(dot, wrapper);
  } else {
    row.append(dot, label);
  }
  activity.prepend(row);
  trimActivity();
}

function appendApproval(payload) {
  clearActivityPlaceholder();

  const row = document.createElement("div");
  row.className = "activity-item approval";
  row.dataset.approvalId = payload.approval_id;

  const title = document.createElement("div");
  title.className = "activity-title";
  title.textContent = `Approval required: ${payload.tool}`;

  const detail = document.createElement("div");
  detail.className = "activity-detail";
  detail.textContent = payload.call || safeJson(payload.params || {});

  const actions = document.createElement("div");
  actions.className = "approval-actions";

  const approve = document.createElement("button");
  approve.className = "primary";
  approve.type = "button";
  approve.textContent = "Approve";

  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "Save Allow";

  const deny = document.createElement("button");
  deny.className = "danger";
  deny.type = "button";
  deny.textContent = "Deny";

  function submitDecision(decision) {
    socket.send(
      JSON.stringify({
        type: "approval.response",
        approval_id: payload.approval_id,
        decision,
      }),
    );
    for (const button of actions.querySelectorAll("button")) {
      button.disabled = true;
    }
    title.textContent = `Approval sent: ${payload.tool}`;
  }

  approve.addEventListener("click", () => submitDecision("approve"));
  save.addEventListener("click", () => submitDecision("approve_save"));
  deny.addEventListener("click", () => submitDecision("deny"));

  actions.append(approve, save, deny);
  row.append(title, detail, actions);
  activity.prepend(row);
  trimActivity();
}

function setSocketState(label, stateClass) {
  socketState.textContent = label;
  socketState.className = `socket-state ${stateClass}`;
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  input.disabled = isBusy;
  if (isBusy) {
    engineState.textContent = "Working";
  }
  if (!isBusy) {
    engineState.textContent = socketState.classList.contains("online")
      ? "Connected"
      : "Offline";
    input.focus();
  }
}

function trimActivity() {
  while (activity.children.length > MAX_ACTIVITY_ITEMS) {
    activity.lastElementChild.remove();
  }
}

function clearActivityPlaceholder() {
  const placeholder = activity.querySelector("[data-placeholder='true']");
  if (placeholder) {
    placeholder.remove();
  }
}

socket.addEventListener("open", () => {
  setSocketState("Online", "online");
});

socket.addEventListener("close", () => {
  setSocketState("Offline", "offline");
  appendActivity("WebSocket disconnected", true);
});

socket.addEventListener("message", (event) => {
  const data = JSON.parse(event.data);
  const payload = data.payload || {};

  if (data.type === "session.ready") {
    engineState.textContent = payload.provider_ready ? "Connected" : "Provider needed";
    approvalState.textContent = payload.approval_mode || "normal";
    providerState.textContent = payload.provider ? payload.provider.model : "Not ready";
    if (payload.provider) {
      providerState.title = payload.provider.provider || "";
    }
    if (payload.error) {
      appendActivity("Provider not ready", true, payload.error);
    }
    return;
  }

  if (data.type === "workspace.ready") {
    appendActivity("Workspace ready", false, payload.path);
    return;
  }

  if (data.type === "assistant.message") {
    appendMessage("assistant", "Kerkov", payload.message);
    return;
  }

  if (data.type === "turn.start") {
    setBusy(true);
    return;
  }

  if (data.type === "turn.end") {
    setBusy(false);
    return;
  }

  if (data.type === "status.start") {
    return;
  }

  if (data.type === "tool.start") {
    appendActivity(`Tool started: ${payload.name}`, false, safeJson(payload.params));
    return;
  }

  if (data.type === "tool.pending_approval") {
    return;
  }

  if (data.type === "approval.request") {
    appendApproval(payload);
    return;
  }

  if (data.type === "approval.auto") {
    return;
  }

  if (data.type === "tool.result") {
    appendActivity(
      `${payload.success ? "Tool finished" : "Tool failed"}: ${payload.name}`,
      !payload.success,
      payload.error || payload.output || "",
    );
    return;
  }

  if (data.type === "token.usage") {
    return;
  }

  if (data.type === "mode.info") {
    appendActivity(`Mode: ${payload.name}`, false, payload.description);
    return;
  }

  if (data.type === "mode.changed") {
    appendActivity(`Mode switched to ${payload.name}`, false, payload.description);
    return;
  }

  if (data.type === "session.cleared") {
    stream.replaceChildren();
    appendMessage("assistant", "Kerkov", "Conversation history cleared.");
    appendActivity("Session cleared");
    return;
  }

  if (data.type === "provider.error") {
    appendActivity("Provider error", true, payload.detail);
    return;
  }

  if (data.type === "log.info" || data.type === "log.warning") {
    appendActivity(payload.message, data.type === "log.warning");
    return;
  }

  if (data.type === "error") {
    appendActivity(payload.detail || "Unknown socket error", true);
  }
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || socket.readyState !== WebSocket.OPEN) {
    return;
  }

  appendMessage("user", "You", message);
  socket.send(JSON.stringify({ type: "user.message", message }));
  input.value = "";
});
