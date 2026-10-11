const stream = document.querySelector("#message-stream");
const welcomeState = document.querySelector("#welcome-state");
const composer = document.querySelector("#composer");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const stopButton = document.querySelector("#stop-button");
const socketState = document.querySelector("#socket-state");
const engineState = document.querySelector("#engine-state");
const providerState = document.querySelector("#provider-state");
const approvalState = document.querySelector("#approval-state");
const approvalTray = document.querySelector("#approval-tray");
const approvalList = document.querySelector("#approval-list");
const tokenState = document.querySelector("#token-state");
const workspaceTitle = document.querySelector("#workspace-title");
const modeButtons = Array.from(document.querySelectorAll(".mode-chip"));
const expandToggle = document.querySelector("#expand-toggle");
const agentsPanel = new AgentsPanel({ send: sendSocketEvent, requestId: createMessageId });

const MAX_ACTIVITY_ITEMS = 40;
let socket = null;
let sessionReady = false;
let busy = false;
let idleEngineState = "Starting";
let idleApprovalState = "normal";
let pendingSubmission = null;
let assistantStream = null;
let followingLatest = true;
const inventoryPanel = new InventoryPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
  onPendingChange: syncControls,
  onViewChange: (view) => {
    resizeInput();
    agentsPanel.visibilityChanged();
    // A hidden stream ignores scrollTop, so catch up on output and resizes from while it was away.
    if (view === "conversation" && followingLatest) scrollToLatest();
    if (view === "network") networkMap.show();
  },
  onSnapshot: (payload) => {
    networkMap.receiveSnapshot(payload);
    networkMap.setState({ connected: socketIsOpen() && sessionReady, canWrite: inventoryPanel.canWrite() });
  },
  onStatus: (detail) => networkMap.setStatus(detail),
  useInConversation: (entry) => {
    const resource = entry.entry;
    const role = entry.section === "assets" ? "asset" : "target";
    const reference = resource.name ? `${resource.name} (${resource.value})` : resource.value;
    const next = `${input.value}${input.value.trim() ? "\n\n" : ""}Review ${role}: ${reference}`;
    if (next.length > input.maxLength) {
      inventoryPanel.setText("inventory-status", "The conversation draft is too long. Shorten it before adding a resource.");
      return false;
    }
    input.value = next;
    window.requestAnimationFrame(() => {
      resizeInput();
      input.focus();
    });
    return true;
  },
});
const providerPanel = new ProviderPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
  onPendingChange: syncControls,
  onChanged: (payload) => {
    idleEngineState = payload.provider_ready ? "Connected" : "Provider needed";
    providerState.textContent = payload.active.model;
    providerState.title = payload.active.provider;
  },
});

const networkMap = new NetworkMap({
  refresh: () => inventoryPanel.refresh(),
  openResource: (id) => {
    inventoryPanel.showView("inventory");
    if (!inventoryPanel.editing) inventoryPanel.select(id);
  },
  addResource: () => {
    inventoryPanel.showView("inventory", false);
    if (!inventoryPanel.editing) inventoryPanel.openForm();
  },
});

const engagementPanel = new EngagementPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
  onPendingChange: syncControls,
  canLeave: () => {
    if (input.value.trim()) return "Send or clear your message draft before switching engagements.";
    if (inventoryPanel.editing) return "Save or cancel the resource edit before switching engagements.";
    return "";
  },
  onResumed: (payload) => {
    finishAssistantStream();
    stream.replaceChildren();
    approvalList.replaceChildren();
    syncApprovalTray();
    agentsPanel.reset();
    inventoryPanel.showView("conversation", false);
    setMode(payload.mode, payload.mode_name);
    idleApprovalState = payload.approval_mode;
    approvalState.textContent = idleApprovalState;
    tokenState.textContent = "0";
    renderSavedHistory(payload);
    appendActivity("Engagement resumed", false, `Saved model: ${payload.saved_model}. Your currently selected model remains active. Token usage starts from this resume.`);
    inventoryPanel.refresh();
    window.requestAnimationFrame(() => input.focus());
  },
  onHistory: (payload) => {
    const button = stream.querySelector('.history-earlier');
    if (!payload) {
      if (button) { button.disabled = false; button.textContent = "Retry earlier messages"; }
      return;
    }
    renderSavedHistory(payload, true);
  },
});

function renderSavedHistory(payload, prepend = false) {
  const previousHeight = stream.scrollHeight;
  const previousTop = stream.scrollTop;
  stream.querySelector('.history-earlier')?.remove();
  const fragment = document.createDocumentFragment();
  for (const message of payload.messages || []) {
    const labels = { user: "You", assistant: "Kerkov", tool: "Tool output" };
    const role = Object.hasOwn(labels, message.role) ? message.role : "assistant";
    const article = appendMessage(role, labels[role], message.content +
      (message.truncated ? "\n[Preview clipped. The full message remains in the saved engagement.]" : "") +
      (message.partial ? "\n[Interrupted response]" : ""));
    fragment.append(article);
  }
  if (prepend) stream.prepend(fragment);
  else stream.append(fragment);
  if (payload.before !== null && payload.before !== undefined) {
    const earlier = document.createElement('button');
    earlier.type = 'button';
    earlier.className = 'history-earlier';
    earlier.textContent = 'Load earlier messages';
    earlier.addEventListener('click', () => {
      if (engagementPanel.loadEarlier(payload.before)) {
        earlier.disabled = true;
        earlier.textContent = 'Loading earlier messages…';
      }
    });
    stream.prepend(earlier);
  }
  stream.scrollTop = prepend ? previousTop + stream.scrollHeight - previousHeight : stream.scrollHeight;
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function appendMessage(kind, label, text) {
  if (welcomeState) {
    welcomeState.hidden = true;
  }
  const article = document.createElement("article");
  article.className = `message ${kind}`;

  const heading = document.createElement("span");
  heading.className = "message-label";
  heading.textContent = label;

  const body = document.createElement("p");
  body.textContent = text;

  article.append(heading, body);
  stream.append(article);
  scrollToLatest();
  return article;
}

function finishAssistantStream() {
  if (assistantStream && assistantStream.article) {
    assistantStream.article.querySelector(".message-label").textContent = "Kerkov · Partial response";
    assistantStream.article.removeAttribute("aria-busy");
  }
  assistantStream = null;
}

function handleAssistantStream(type, payload) {
  if (!sessionReady || !socketIsOpen() || typeof payload.stream_id !== "string" || !payload.stream_id) return;
  if (type === "assistant.stream.start") {
    finishAssistantStream();
    assistantStream = { id: payload.stream_id, article: null, open: true, leadingWhitespace: "" };
    return;
  }
  if (!assistantStream || assistantStream.id !== payload.stream_id) return;
  if (type === "assistant.stream.end") {
    assistantStream.open = false;
    return;
  }
  if (type !== "assistant.stream.delta" || !assistantStream.open || typeof payload.text !== "string" || !payload.text) return;
  let text = payload.text;
  if (!assistantStream.article) {
    text = assistantStream.leadingWhitespace + text;
    if (!text.trim()) {
      assistantStream.leadingWhitespace = text;
      return;
    }
    assistantStream.leadingWhitespace = "";
  }
  const followLatest = isFollowingLatest();
  if (!assistantStream.article) {
    const previousScroll = stream.scrollTop;
    assistantStream.article = appendMessage("assistant", "Kerkov · Responding…", "");
    assistantStream.article.setAttribute("aria-busy", "true");
    if (!followLatest) {
      stream.scrollTop = previousScroll;
      followingLatest = false;
    }
  }
  const body = assistantStream.article.querySelector("p");
  // Keep provider output inert, including HTML-looking text, and append only
  // the new text instead of replacing the growing response on every delta.
  if (!body.firstChild) body.append(document.createTextNode(""));
  body.firstChild.appendData(text);
  if (followLatest) scrollToLatest();
}

function completeAssistantMessage(payload) {
  if (payload.stream_id) {
    if (!assistantStream || assistantStream.id !== payload.stream_id) return;
    const article = assistantStream.article;
    if (article) {
      const followLatest = isFollowingLatest();
      article.querySelector("p").textContent = payload.message;
      article.querySelector(".message-label").textContent = "Kerkov";
      article.removeAttribute("aria-busy");
      if (followLatest) scrollToLatest();
    } else {
      appendMessage("assistant", "Kerkov", payload.message);
    }
    assistantStream = null;
    return;
  }
  appendMessage("assistant", "Kerkov", payload.message);
}

function scrollToLatest() {
  followingLatest = true;
  stream.scrollTop = stream.scrollHeight;
}

function isFollowingLatest() {
  // A hidden stream measures zero, so answer with where the reader was when it was last shown.
  if (!stream.getClientRects().length) {
    return followingLatest;
  }
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 48;
}

function appendActivity(text, idle = false, detail = "", failed = false) {
  const followLatest = isFollowingLatest();
  if (welcomeState) {
    welcomeState.hidden = true;
  }

  const row = document.createElement("div");
  row.className = "activity-item";
  row.dataset.failed = String(failed);
  row.dataset.attention = String(idle && !failed);

  const dot = document.createElement("span");
  dot.className = idle ? "dot idle" : "dot";
  dot.setAttribute("aria-hidden", "true");

  const label = document.createElement("span");
  label.textContent = text;

  if (detail) {
    const disclosure = document.createElement("details");
    disclosure.className = "activity-disclosure";
    disclosure.open = idle;
    const summary = document.createElement("summary");
    summary.className = "activity-title";
    summary.textContent = text;
    const body = document.createElement("div");
    body.className = "activity-detail";
    body.textContent = detail;
    disclosure.append(summary, body);
    row.append(dot, disclosure);
  } else {
    row.append(dot, label);
  }
  let group = stream.lastElementChild;
  if (!group || !group.classList.contains("activity-group")) {
    group = document.createElement("details");
    group.className = "activity-group";
    const summary = document.createElement("summary");
    const events = document.createElement("div");
    events.className = "activity-events";
    group.append(summary, events);
    stream.append(group);
  }
  group.querySelector(".activity-events").append(row);
  trimActivity();
  updateActivitySummary(group);
  if (followLatest) {
    scrollToLatest();
  }
}

function updateActivitySummary(group) {
  const rows = Array.from(group.querySelector(".activity-events").children);
  const failures = rows.filter((row) => row.dataset.failed === "true").length;
  const alerts = rows.filter((row) => row.dataset.attention === "true").length;
  const parts = ["Activity", `${rows.length} ${rows.length === 1 ? "event" : "events"}`];
  if (failures) {
    parts.push(`${failures} failed`);
  }
  if (alerts) {
    parts.push(`${alerts} ${alerts === 1 ? "alert" : "alerts"}`);
  }
  group.querySelector("summary").textContent = parts.join(" · ");
  group.classList.toggle("has-issues", failures > 0 || alerts > 0);
}

function findApproval(approvalId) {
  return Array.from(approvalList.querySelectorAll("[data-approval-id]")).find(
    (row) => row.dataset.approvalId === approvalId,
  );
}

function syncApprovalTray() {
  approvalTray.hidden = approvalList.children.length === 0;
  document.querySelector("#conversation-approval-indicator").hidden = approvalTray.hidden;
}

function disableApprovalActions(row) {
  for (const button of row.querySelectorAll("button")) {
    button.disabled = true;
  }
}

function appendApproval(payload) {
  const wasViewingInventory = inventoryPanel.view !== "conversation";
  inventoryPanel.showView("conversation", false);
  const row = document.createElement("div");
  row.className = "activity-item approval";
  row.dataset.approvalId = payload.approval_id;

  const title = document.createElement("div");
  title.className = "activity-title";
  title.textContent = `Approval required: ${payload.tool}`;
  title.tabIndex = -1;

  const detail = document.createElement("div");
  detail.className = "activity-detail";
  detail.textContent = payload.call || safeJson(payload.params || {});

  const actions = document.createElement("div");
  actions.className = "approval-actions";

  const approve = document.createElement("button");
  approve.className = "primary";
  approve.type = "button";
  approve.textContent = "Approve";

  const deny = document.createElement("button");
  deny.className = "danger";
  deny.type = "button";
  deny.textContent = "Deny";

  function submitDecision(decision) {
    disableApprovalActions(row);
    if (
      !sendSocketEvent({
        type: "approval.response",
        approval_id: payload.approval_id,
        decision,
      })
    ) {
      title.textContent = `Unable to submit approval: ${payload.tool}`;
      approvalState.textContent = "Unavailable";
      return;
    }
    title.textContent = `Decision submitted: ${payload.tool}`;
    approvalState.textContent = "Processing";
  }

  approve.addEventListener("click", () => submitDecision("approve"));
  deny.addEventListener("click", () => submitDecision("deny"));

  actions.append(approve, deny);
  row.append(title, detail, actions);
  for (const choice of payload.allow_rule_choices || []) {
    const option = document.createElement("div");
    option.className = "approval-actions approval-rule-option";
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = `Approve and allow ${choice.label}`;
    save.addEventListener("click", () => submitDecision(choice.decision));
    const rule = document.createElement("code");
    rule.className = "activity-detail";
    rule.textContent = choice.rule;
    option.append(save, rule);
    row.append(option);
  }
  approvalList.prepend(row);
  syncApprovalTray();
  approvalState.textContent = "Awaiting decision";
  if (wasViewingInventory) title.focus();
}

function resolveApproval(payload, accepted) {
  const row = findApproval(payload.approval_id);
  if (!row) {
    return;
  }
  disableApprovalActions(row);
  const title = row.querySelector(".activity-title");
  if (accepted) {
    const label = payload.decision === "deny" ? "Denied" : "Approved";
    title.textContent = `${label}: approval response accepted`;
    appendActivity(`${label}: approval response accepted`);
  } else {
    title.textContent = "Approval response rejected";
    appendActivity("Approval response rejected", true, payload.detail || "Unknown approval error");
  }
  row.remove();
  syncApprovalTray();
  approvalState.textContent = idleApprovalState;
}

function expireApprovals() {
  for (const row of approvalList.querySelectorAll("[data-approval-id]")) {
    disableApprovalActions(row);
    appendActivity("Approval expired: session disconnected", true);
    row.remove();
  }
  syncApprovalTray();
  approvalState.textContent = "Unavailable";
}

function cancelApproval(payload) {
  const row = findApproval(payload.approval_id);
  if (!row) {
    return;
  }
  row.remove();
  syncApprovalTray();
  approvalState.textContent = approvalTray.hidden ? idleApprovalState : "Awaiting decision";
  appendActivity("Approval cancelled", false, payload.detail || "The pending action was cancelled.");
}

function setSocketState(label, stateClass = "") {
  socketState.textContent = label;
  socketState.className = ["socket-state", stateClass].filter(Boolean).join(" ");
}

function socketIsOpen() {
  return socket !== null && socket.readyState === WebSocket.OPEN;
}

function syncControls() {
  const connected = socketIsOpen() && sessionReady;
  engagementPanel.setState({ connected, blocked: busy || inventoryPanel.saving || providerPanel.switching || providerPanel.saving ||
    Array.from(agentsPanel.agents.values()).some((agent) => agentsPanel.isActive(agent)) });
  providerPanel.setState({ connected, busy: busy || engagementPanel.switching, saving: inventoryPanel.saving,
    activeAgents: Array.from(agentsPanel.agents.values()).some((agent) => agentsPanel.isActive(agent)) });
  inventoryPanel.setState({ connected, busy: busy || providerPanel.switching || providerPanel.saving || engagementPanel.switching });
  networkMap.setState({ connected, canWrite: inventoryPanel.canWrite() });
  const canInteract = connected && !engagementPanel.switching && !busy && !inventoryPanel.saving && !providerPanel.switching && !providerPanel.saving;
  input.disabled = !canInteract;
  sendButton.disabled = !canInteract;
  const canStop = connected && busy && pendingSubmission && pendingSubmission.canStop;
  const stopping = Boolean(pendingSubmission && pendingSubmission.stopping);
  stopButton.hidden = !canStop;
  stopButton.disabled = !canStop || stopping;
  stopButton.textContent = stopping ? "Stopping…" : "Stop";
  stopButton.setAttribute("aria-label", stopping ? "Stopping main run" : "Stop main run");
  for (const button of modeButtons) {
    button.disabled = !canInteract;
  }

  if (stopping) {
    engineState.textContent = "Stopping…";
  } else if (engagementPanel.switching) {
    engineState.textContent = "Opening engagement";
  } else if (providerPanel.switching) {
    engineState.textContent = "Switching model";
  } else if (providerPanel.saving) {
    engineState.textContent = "Saving provider";
  } else if (busy) {
    engineState.textContent = "Working";
  } else if (sessionReady) {
    engineState.textContent = idleEngineState;
  } else if (socketIsOpen()) {
    engineState.textContent = "Starting";
  } else {
    engineState.textContent = "Offline";
  }
}

function setMode(modeId, modeName) {
  for (const button of modeButtons) {
    const active = button.dataset.mode === modeId;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  if (modeName) {
    workspaceTitle.textContent = `${modeName} Workspace`;
  }
}

function trimActivity() {
  const rows = stream.querySelectorAll(":scope > .activity-group > .activity-events > .activity-item");
  for (let index = 0; index < rows.length - MAX_ACTIVITY_ITEMS; index += 1) {
    const events = rows[index].parentElement;
    const group = events.parentElement;
    rows[index].remove();
    if (events.children.length) {
      updateActivitySummary(group);
    } else {
      group.remove();
    }
  }
}

function sendSocketEvent(payload) {
  if (!socketIsOpen()) {
    return false;
  }
  try {
    socket.send(JSON.stringify(payload));
    return true;
  } catch (_error) {
    appendActivity("Unable to send WebSocket message", true);
    return false;
  }
}

function createMessageId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function resizeInput() {
  input.style.height = "auto";
  const styles = window.getComputedStyle(input);
  const borderHeight = parseFloat(styles.borderTopWidth) + parseFloat(styles.borderBottomWidth);
  const maxHeight = parseFloat(styles.maxHeight) || Infinity;
  const contentHeight = input.scrollHeight + borderHeight;
  input.style.height = `${Math.min(contentHeight, maxHeight)}px`;
  input.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
}

function setConsoleExpanded(expanded) {
  const followLatest = isFollowingLatest();
  document.body.classList.toggle("console-expanded", expanded);
  const label = expanded ? "Exit expanded console" : "Expand console";
  expandToggle.setAttribute("aria-label", label);
  expandToggle.title = expanded ? `${label} (Esc)` : label;
  resizeInput();
  // Collapsing shrinks the stream without moving scrollTop, which would leave the latest output below the fold.
  if (followLatest) {
    scrollToLatest();
  }
}

function restoreSubmissionFocus(target) {
  if (!(target instanceof HTMLElement)) {
    return;
  }
  window.requestAnimationFrame(() => {
    if (
      target.isConnected &&
      document.activeElement === document.body &&
      !target.matches(":disabled")
    ) {
      target.focus();
    }
  });
}

function finishSubmission(messageId) {
  if (pendingSubmission && pendingSubmission.id !== messageId) {
    return false;
  }
  const focusTarget = document.activeElement === stopButton ? input : pendingSubmission ? pendingSubmission.focusTarget : null;
  if (document.activeElement === stopButton) stopButton.blur();
  pendingSubmission = null;
  busy = false;
  syncControls();
  restoreSubmissionFocus(focusTarget);
  return true;
}

function submitUserMessage(message, { showInConversation = true } = {}) {
  const normalized = message.trim();
  if (!normalized || busy || engagementPanel.switching || inventoryPanel.saving || providerPanel.switching || providerPanel.saving || !sessionReady || !socketIsOpen()) {
    return false;
  }

  const messageId = createMessageId();
  pendingSubmission = {
    id: messageId,
    message: normalized,
    showInConversation,
    canStop: false,
    stopping: false,
    focusTarget: document.activeElement,
  };
  busy = true;
  syncControls();

  if (
    !sendSocketEvent({
      type: "user.message",
      message_id: messageId,
      message: normalized,
    })
  ) {
    finishSubmission(messageId);
    return false;
  }
  return true;
}

function stopSubmission() {
  if (!busy || !pendingSubmission || !pendingSubmission.canStop || pendingSubmission.stopping) {
    return;
  }
  if (document.activeElement === stopButton) pendingSubmission.focusTarget = input;
  pendingSubmission.stopping = true;
  syncControls();
  if (!sendSocketEvent({ type: "user.message.stop", message_id: pendingSubmission.id })) {
    pendingSubmission.stopping = false;
    syncControls();
  }
}

function handleSocketEvent(data) {
  const payload = data.payload || {};

  if (data.type.startsWith("engagement.")) {
    engagementPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type.startsWith("inventory.")) {
    inventoryPanel.handleEvent(data.type, payload);
    return;
  }

  if (["provider.catalog", "provider.models", "provider.changed", "provider.settings", "provider.tested", "provider.configured", "provider.default_saved", "provider.selection_error"].includes(data.type)) {
    providerPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type === "session.ready") {
    sessionReady = true;
    agentsPanel.setConnected(true);
    idleEngineState = payload.provider_ready ? "Connected" : "Provider needed";
    idleApprovalState = payload.approval_mode || "normal";
    approvalState.textContent = idleApprovalState;
    providerState.textContent = payload.provider ? payload.provider.model : "Not ready";
    if (payload.provider) {
      providerState.title = payload.provider.provider || "";
    }
    providerPanel.setInitial(payload.provider, payload.provider_ready);
    setMode(payload.mode, payload.mode_name);
    if (payload.error) {
      appendActivity("Provider not ready", true, payload.error);
    }
    syncControls();
    inventoryPanel.refresh();
    providerPanel.loadCatalog();
    engagementPanel.refresh();
    return;
  }

  if (data.type === "agent.updated") {
    agentsPanel.update(payload.agent);
    syncControls();
    inventoryPanel.refresh();
    return;
  }

  if (["agent.output", "agent.message_sent", "agent.stopped", "agent.control_error"].includes(data.type)) {
    agentsPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type === "agents.reset") {
    agentsPanel.reset();
    syncControls();
    return;
  }

  if (data.type === "workspace.ready") {
    appendActivity("Workspace ready", false, payload.path);
    inventoryPanel.refresh();
    engagementPanel.refresh();
    return;
  }

  if (data.type === "user.message.accepted") {
    if (pendingSubmission && pendingSubmission.id === payload.message_id) {
      pendingSubmission.canStop = payload.stoppable === true;
      syncControls();
      if (pendingSubmission.showInConversation) {
        appendMessage("user", "You", pendingSubmission.message);
        input.value = "";
        resizeInput();
      }
    }
    return;
  }

  if (data.type === "user.message.rejected") {
    appendActivity("Message rejected", true, payload.detail || "Unknown message error");
    finishSubmission(payload.message_id);
    return;
  }

  if (data.type === "user.message.complete") {
    const stopped = pendingSubmission && pendingSubmission.id === payload.message_id && payload.stopped;
    if (finishSubmission(payload.message_id)) finishAssistantStream();
    if (stopped) appendActivity("Run stopped");
    inventoryPanel.refresh();
    engagementPanel.refresh();
    return;
  }

  if (data.type === "user.message.stopping" || data.type === "user.message.stop_rejected") {
    if (!pendingSubmission || pendingSubmission.id !== payload.message_id || !pendingSubmission.canStop) {
      return;
    }
    pendingSubmission.stopping = data.type === "user.message.stopping";
    if (!pendingSubmission.stopping) {
      appendActivity("Unable to stop run", true, payload.detail || "Try stopping the run again.");
    }
    syncControls();
    return;
  }

  if (data.type.startsWith("assistant.stream.")) {
    handleAssistantStream(data.type, payload);
    return;
  }

  if (data.type === "assistant.message") {
    completeAssistantMessage(payload);
    return;
  }

  if (data.type === "turn.start") {
    busy = true;
    syncControls();
    return;
  }

  if (data.type === "turn.end") {
    finishAssistantStream();
    engineState.textContent = pendingSubmission && pendingSubmission.stopping ? "Stopping…" : idleEngineState;
    return;
  }

  if (data.type === "status.start") {
    engineState.textContent = pendingSubmission && pendingSubmission.stopping ? "Stopping…" : payload.label || "Working";
    return;
  }

  if (data.type === "status.end") {
    syncControls();
    return;
  }

  if (data.type === "tool.start") {
    appendActivity(`Tool requested: ${payload.name}`, false, safeJson(payload.params));
    return;
  }

  if (data.type === "tool.pending_approval") {
    appendActivity(`Tool awaiting approval: ${payload.name}`, false);
    return;
  }

  if (data.type === "approval.request") {
    appendApproval(payload);
    return;
  }

  if (data.type === "approval.resolved") {
    resolveApproval(payload, true);
    return;
  }

  if (data.type === "approval.rejected") {
    resolveApproval(payload, false);
    return;
  }

  if (data.type === "approval.cancelled") {
    cancelApproval(payload);
    return;
  }

  if (data.type === "approval.auto") {
    const action = payload.approved ? "Auto-approved" : "Auto-denied";
    appendActivity(`${action}: ${payload.tool}`, !payload.approved, payload.reason || "");
    return;
  }

  if (data.type === "tool.result") {
    appendActivity(
      `${payload.success ? "Tool finished" : "Tool failed"}: ${payload.name}`,
      !payload.success,
      payload.error || payload.output || "",
      !payload.success,
    );
    inventoryPanel.refresh();
    return;
  }

  if (data.type === "token.usage") {
    tokenState.textContent = Number(payload.total_tokens || 0).toLocaleString();
    return;
  }

  if (data.type === "mode.info") {
    setMode(payload.current, payload.name);
    appendActivity(`Mode: ${payload.name}`, false, payload.description);
    return;
  }

  if (data.type === "mode.changed") {
    finishAssistantStream();
    engagementPanel.contextChanged();
    agentsPanel.reset();
    syncControls();
    inventoryPanel.refresh();
    setMode(payload.to, payload.name);
    tokenState.textContent = "0";
    stream.replaceChildren();
    appendMessage(
      "assistant",
      "Kerkov",
      `Mode switched to ${payload.name}. The previous conversation was cleared.`,
    );
    appendActivity(`Mode switched to ${payload.name}`, false, payload.description);
    return;
  }

  if (data.type === "session.cleared") {
    finishAssistantStream();
    engagementPanel.contextChanged();
    agentsPanel.reset();
    syncControls();
    inventoryPanel.refresh();
    tokenState.textContent = "0";
    stream.replaceChildren();
    appendMessage("assistant", "Kerkov", "Conversation history cleared.");
    appendActivity("Session cleared");
    return;
  }

  if (data.type === "provider.error") {
    appendActivity("Provider error", true, payload.detail);
    return;
  }

  if (data.type === "log.info" || data.type === "log.warning" || data.type === "log.error") {
    appendActivity(payload.message, data.type !== "log.info");
    return;
  }

  if (data.type === "error") {
    appendActivity(payload.detail || "Unknown socket error", true);
  }
}

function connectSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/ws/console/`;
  try {
    socket = new WebSocket(url);
  } catch (_error) {
    setSocketState("Offline", "offline");
    appendActivity("Unable to open WebSocket", true);
    syncControls();
    return;
  }

  setSocketState("Connecting");
  syncControls();

  socket.addEventListener("open", () => {
    setSocketState("Online", "online");
    syncControls();
  });

  socket.addEventListener("error", () => {
    setSocketState("Error", "offline");
    appendActivity("WebSocket connection error", true);
    syncControls();
  });

  socket.addEventListener("close", () => {
    finishAssistantStream();
    agentsPanel.setConnected(false);
    sessionReady = false;
    busy = false;
    pendingSubmission = null;
    setSocketState("Offline", "offline");
    expireApprovals();
    appendActivity("WebSocket disconnected", true);
    syncControls();
  });

  socket.addEventListener("message", (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_error) {
      appendActivity("Malformed WebSocket event", true);
      return;
    }
    if (!data || typeof data !== "object" || typeof data.type !== "string") {
      appendActivity("Invalid WebSocket event", true);
      return;
    }
    handleSocketEvent(data);
  });
}

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  submitUserMessage(input.value);
});

stopButton.addEventListener("click", stopSubmission);

input.addEventListener("input", resizeInput);
document.querySelector(".skip-link").addEventListener("click", () => {
  inventoryPanel.showView("conversation", false);
});
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    if (!event.repeat) {
      composer.requestSubmit();
    }
  }
});
window.addEventListener("resize", resizeInput);
resizeInput();
stream.addEventListener("scroll", () => {
  followingLatest = isFollowingLatest();
});

expandToggle.addEventListener("click", () => {
  setConsoleExpanded(!document.body.classList.contains("console-expanded"));
});
document.addEventListener("keydown", (event) => {
  if (
    event.key === "Escape" &&
    !event.defaultPrevented &&
    !event.isComposing &&
    // An open dialog closes on this same Escape.
    !document.querySelector("dialog[open]") &&
    document.body.classList.contains("console-expanded")
  ) {
    setConsoleExpanded(false);
  }
});

for (const button of modeButtons) {
  button.addEventListener("click", () => {
    const mode = button.dataset.mode;
    if (mode && button.getAttribute("aria-pressed") !== "true") {
      submitUserMessage(`/mode ${mode}`, { showInConversation: false });
    }
  });
}

connectSocket();
