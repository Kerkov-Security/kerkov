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
const approvalOptions = Array.from(document.querySelectorAll(".approval-option"));
const tokenState = document.querySelector("#token-state");
const workspaceTitle = document.querySelector("#workspace-title");
const modeButtons = Array.from(document.querySelectorAll(".mode-chip"));
const ctfObjectiveState = document.querySelector("#ctf-objective-state");
const ctfStartButton = document.querySelector("#ctf-start");
const ctfStopButton = document.querySelector("#ctf-stop");
const expandToggle = document.querySelector("#expand-toggle");
const agentsPanel = new AgentsPanel({ send: sendSocketEvent, requestId: createMessageId });
const htbSettings = new HTBSettings({ send: sendSocketEvent, requestId: createMessageId });
const mcpPanel = new McpPanel({ send: sendSocketEvent, requestId: createMessageId });
const commandSuggestions = new CommandSuggestions({
  input,
  anchor: document.querySelector(".composer-field"),
});
const composerStartHint = document.querySelector("#composer-start-hint");
// A resumed conversation may render no user message: history is paged and
// compaction summarizes early turns. Clearing or switching mode resets it.
let resumedSavedHistory = false;
function syncComposerStartHint() {
  composerStartHint.hidden = resumedSavedHistory || Boolean(stream.querySelector(".message.user"));
}
// Every path that adds, clears or replaces the transcript changes the stream's children.
new MutationObserver(syncComposerStartHint).observe(stream, { childList: true });

const MAX_ACTIVITY_ITEMS = 40;
let socket = null;
let sessionReady = false;
let busy = false;
let idleEngineState = "Starting";
let idleApprovalState = "normal";
// The last mode sent but not yet confirmed; a click is compared against this so
// reverting a pending change still reaches the server.
let requestedApprovalMode = null;
let pendingSubmission = null;
let assistantStream = null;
// The engagement's active engagement-scoped workflow ("ctf" or null).
let activeWorkflow = null;
let followingLatest = true;
const inventoryPanel = new InventoryPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
  startWorkflow: (message, workflowRequest = null, projectRequest = null, conversationWorkflow = false) =>
    submitUserMessage(message, { showInConversation: false, preserveDraft: true, workflow: true, workflowRequest, projectRequest, conversationWorkflow }) ? pendingSubmission.id : null,
  appendComplianceCard: (title, body) => {
    const article = appendCommandReply(title, { body });
    return {
      article,
      moveToLatest: () => {
        if (article.parentElement !== stream) return;
        if (article !== stream.lastElementChild) {
          const focused = document.activeElement === article;
          stream.append(article);
          if (focused) article.focus({ preventScroll: true });
        }
        if (followingLatest && inventoryPanel.view === "conversation") scrollToLatest();
      },
    };
  },
  onPendingChange: syncControls,
  onViewChange: (view) => {
    resizeInput();
    agentsPanel.visibilityChanged();
    // A hidden stream ignores scrollTop, so catch up on output and resizes from while it was away.
    if (view === "conversation" && followingLatest) scrollToLatest();
    if (view === "network") networkMap.show();
    if (view === "scripts") scriptsPanel.show();
    if (view === "browser") browserPanel.show();
  },
  onSnapshot: (payload) => {
    networkMap.receiveSnapshot(payload);
    networkMap.setState({ connected: socketIsOpen() && sessionReady, canWrite: inventoryPanel.canWrite() });
  },
  onStatus: (detail) => networkMap.setStatus(detail),
  appendDraft: appendConversationDraft,
  useInConversation: (entry, instruction) => {
    const resource = entry.entry;
    const role = entry.section === "assets" ? "asset" : "target";
    const reference = resource.name ? `${resource.name} (${resource.value})` : resource.value;
    const lines = [`Review ${role}: ${reference}`];
    if (resource.environment) lines.push(`Environment: ${resource.environment}`);
    if (resource.tags?.length) lines.push(`Tags: ${resource.tags.join(", ")}`);
    if (resource.notes) lines.push(`Notes: ${resource.notes}`);
    if (instruction) lines.push("", instruction);
    if (appendConversationDraft(lines.join("\n"))) return true;
    inventoryPanel.setText("inventory-status", "The conversation draft is too long. Shorten it before adding a resource.");
    return false;
  },
});

function appendConversationDraft(text) {
  const next = input.value + (input.value.trim() ? "\n\n" : "") + text;
  if (next.length > input.maxLength) return false;
  input.value = next;
  window.requestAnimationFrame(() => {
    resizeInput();
    input.focus();
  });
  return true;
}

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
    if (!inventoryPanel.editing) inventoryPanel.profile.openResource(id);
  },
  addResource: () => {
    inventoryPanel.showView("inventory", false);
    if (!inventoryPanel.editing) inventoryPanel.openForm();
  },
});

const scriptsPanel = new ScriptsPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
});

const browserPanel = new BrowserPanel({ send: sendSocketEvent, requestId: createMessageId });

const engagementPanel = new EngagementPanel({
  send: sendSocketEvent,
  requestId: createMessageId,
  onPendingChange: syncControls,
  canLeave: () => {
    if (input.value.trim()) return "Send or clear your message draft before switching engagements.";
    if (inventoryPanel.editing) return "Save or cancel the resource edit before switching engagements.";
    return "";
  },
  onStarted: () => {
    setWorkflow(null);
    scriptsPanel.reset();
    inventoryPanel.showView("conversation", false);
    window.requestAnimationFrame(() => input.focus());
  },
  onResumed: (payload) => {
    finishAssistantStream();
    stream.replaceChildren();
    approvalList.replaceChildren();
    syncApprovalTray();
    agentsPanel.reset();
    inventoryPanel.showView("conversation", false);
    setMode(payload.mode, payload.mode_name);
    setWorkflow(payload.workflow);
    idleApprovalState = payload.approval_mode;
    requestedApprovalMode = null;
    approvalState.textContent = idleApprovalState;
    renderApprovalPicker();
    tokenState.textContent = "0";
    resumedSavedHistory = Boolean(payload.messages?.length) || (payload.before !== null && payload.before !== undefined);
    renderSavedHistory(payload);
    appendActivity("Engagement resumed", false, `Saved model: ${payload.saved_model}. Your currently selected model remains active. Token usage starts from this resume.`);
    inventoryPanel.refresh();
    scriptsPanel.reset();
    scriptsPanel.refresh();
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
  let toolGroup = null;
  for (const message of payload.messages || []) {
    if (message.role === "tool") {
      if (!toolGroup) {
        toolGroup = createActivityGroup(true);
        fragment.append(toolGroup);
      }
      toolGroup.querySelector(".activity-events").append(savedToolActivity(message));
      // An unknown outcome may have changed the target; never hide it.
      if (message.status === "unknown") toolGroup.open = true;
      updateActivitySummary(toolGroup);
      continue;
    }
    toolGroup = null;
    const labels = { user: "You", assistant: "Kerkov" };
    const role = Object.hasOwn(labels, message.role) ? message.role : "assistant";
    const article = appendMessage(role, labels[role], message.content, message.html);
    const notes = [
      message.truncated && "[Preview clipped. The full message remains in the saved engagement.]",
      message.partial && "[Interrupted response]",
    ].filter(Boolean);
    if (notes.length) {
      const note = document.createElement("p");
      note.className = "message-note";
      note.textContent = notes.join("\n");
      article.append(note);
    }
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

const SAVED_TOOL_TITLES = {
  succeeded: "Tool finished",
  failed: "Tool failed",
  unknown: "Tool outcome unknown",
  not_completed: "Tool not completed",
};

function savedToolActivity(message) {
  const status = Object.hasOwn(SAVED_TOOL_TITLES, message.status) ? message.status : "not_completed";
  const name = typeof message.tool_name === "string" && message.tool_name ? `: ${message.tool_name}` : "";
  const content = String(message.content || "");
  const detail = message.truncated
    ? `${content}\n[Preview clipped. The full result remains in the saved engagement.]`
    : content;
  return createActivityRow(`${SAVED_TOOL_TITLES[status]}${name}`, status !== "succeeded", detail, status === "failed");
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function appendMessage(kind, label, text, html) {
  if (welcomeState) {
    welcomeState.hidden = true;
  }
  const article = document.createElement("article");
  article.className = `message ${kind}`;

  const heading = document.createElement("span");
  heading.className = "message-label";
  heading.textContent = label;

  article.append(heading, messageBody(text, html));
  stream.append(article);
  scrollToLatest();
  return article;
}

// The answer to a typed command: in the conversation, but not the model's voice
// and not folded into the activity feed with background events.
function appendCommandReply(title, { body = "", meta = "", failed = false } = {}) {
  if (welcomeState) {
    welcomeState.hidden = true;
  }
  const card = document.createElement("article");
  card.className = "command-reply";
  card.dataset.failed = String(failed);
  card.setAttribute("aria-label", title);

  const head = document.createElement("header");
  head.className = "command-reply-head";
  const heading = document.createElement("strong");
  heading.className = "command-reply-title";
  heading.textContent = title;
  head.append(heading);
  if (meta) {
    const summary = document.createElement("span");
    summary.className = "command-reply-meta";
    summary.textContent = meta;
    head.append(summary);
  }
  card.append(head);

  const content = typeof body === "string" ? (body ? textElement("p", body) : null) : body;
  if (content) {
    content.classList.add("command-reply-body");
    card.append(content);
  }
  stream.append(card);
  scrollToLatest();
  return card;
}

function textElement(tag, text) {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
}

// Only a command typed into the composer answers with a card, and only with the
// events the server tagged as its reply: a picker change made while the command
// is still running, or a refused picker request, stays activity.
function answersTypedCommand(payload) {
  return Boolean(pendingSubmission?.typedCommand) && payload.reply_to === pendingSubmission.id;
}

// Client-authored notices also use the assistant kind, so only a payload carrying
// server-rendered Markdown reaches innerHTML; everything else stays text.
function messageBody(text, html) {
  if (typeof html === "string") {
    const body = document.createElement("div");
    body.className = "message-body markdown";
    body.innerHTML = html;
    addCodeCopyButtons(body);
    return body;
  }
  const body = document.createElement("p");
  body.className = "message-body";
  body.textContent = text;
  return body;
}

const COPY_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
const COPIED_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 13 4 4L19 7"/></svg>';

// Pin a copy control to each fenced code block. The button lives in a wrapper
// beside the <pre> so it stays in place while the code scrolls horizontally.
function addCodeCopyButtons(root) {
  for (const pre of root.querySelectorAll("pre")) {
    if (pre.parentElement && pre.parentElement.classList.contains("code-block")) continue;
    const code = (pre.querySelector("code") || pre).textContent;
    const wrapper = document.createElement("div");
    wrapper.className = "code-block";
    pre.replaceWith(wrapper);
    wrapper.append(pre, createCopyButton(code));
  }
}

function createCopyButton(code) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "code-copy";
  button.title = "Copy code";
  button.setAttribute("aria-label", "Copy code");
  button.innerHTML = COPY_ICON;
  let resetTimer = null;
  button.addEventListener("click", async () => {
    let copied = true;
    try {
      await navigator.clipboard.writeText(code);
    } catch (_error) {
      copied = false;
    }
    button.innerHTML = copied ? COPIED_ICON : COPY_ICON;
    button.classList.toggle("copied", copied);
    button.title = copied ? "Copied" : "Copy failed";
    button.setAttribute("aria-label", button.title);
    window.clearTimeout(resetTimer);
    resetTimer = window.setTimeout(() => {
      button.innerHTML = COPY_ICON;
      button.classList.remove("copied");
      button.title = "Copy code";
      button.setAttribute("aria-label", "Copy code");
    }, 1600);
  });
  return button;
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
  const body = assistantStream.article.querySelector(".message-body");
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
      article.querySelector(".message-body").replaceWith(messageBody(payload.message, payload.html));
      article.querySelector(".message-label").textContent = "Kerkov";
      article.removeAttribute("aria-busy");
      if (followLatest) scrollToLatest();
    } else {
      appendMessage("assistant", "Kerkov", payload.message, payload.html);
    }
    assistantStream = null;
    return;
  }
  appendMessage("assistant", "Kerkov", payload.message, payload.html);
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
  const row = createActivityRow(text, idle, detail, failed);
  let group = stream.lastElementChild;
  if (!group || !group.classList.contains("activity-group") || group.classList.contains("saved-activity")) {
    group = createActivityGroup();
    stream.append(group);
  }
  group.querySelector(".activity-events").append(row);
  trimActivity();
  updateActivitySummary(group);
  if (followLatest) {
    scrollToLatest();
  }
}

// Saved groups belong to the restored transcript, so live trimming skips them.
function createActivityGroup(saved = false) {
  const group = document.createElement("details");
  group.className = saved ? "activity-group saved-activity" : "activity-group";
  const summary = document.createElement("summary");
  const events = document.createElement("div");
  events.className = "activity-events";
  group.append(summary, events);
  return group;
}

// Detail can be target-controlled tool output; it is only ever set as text.
function createActivityRow(text, idle = false, detail = "", failed = false) {
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
  return row;
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

function workflowUsesCompliancePanel() {
  return Boolean(pendingSubmission?.workflow && !pendingSubmission.conversationWorkflow);
}

function syncApprovalTray() {
  const workflow = workflowUsesCompliancePanel();
  const host = document.querySelector(workflow ? "#compliance-approval-host" : "#conversation-approval-host");
  if (approvalTray.parentElement !== host) host.append(approvalTray);
  approvalTray.hidden = approvalList.children.length === 0;
  document.querySelector("#conversation-approval-indicator").hidden = approvalTray.hidden || workflow;
}

function disableApprovalActions(row) {
  for (const button of row.querySelectorAll("button")) {
    button.disabled = true;
  }
}

function appendApproval(payload) {
  const wasViewingInventory = inventoryPanel.view !== "conversation";
  inventoryPanel.showView(workflowUsesCompliancePanel() ? "compliance" : "conversation", false);
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
    if (!pendingSubmission?.workflow) appendActivity(`${label}: approval response accepted`);
  } else {
    title.textContent = "Approval response rejected";
    if (!pendingSubmission?.workflow) appendActivity("Approval response rejected", true, payload.detail || "Unknown approval error");
  }
  row.remove();
  syncApprovalTray();
  approvalState.textContent = idleApprovalState;
}

function expireApprovals() {
  for (const row of approvalList.querySelectorAll("[data-approval-id]")) {
    disableApprovalActions(row);
    if (!pendingSubmission?.workflow) appendActivity("Approval expired: session disconnected", true);
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
  if (!pendingSubmission?.workflow) appendActivity("Approval cancelled", false, payload.detail || "The pending action was cancelled.");
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
  htbSettings.setConnected(connected);
  mcpPanel.setConnected(connected);
  engagementPanel.setState({ connected, blocked: busy || inventoryPanel.saving || providerPanel.switching || providerPanel.saving ||
    Array.from(agentsPanel.agents.values()).some((agent) => agentsPanel.isActive(agent)) });
  providerPanel.setState({ connected, busy: busy || engagementPanel.switching, saving: inventoryPanel.saving,
    activeAgents: Array.from(agentsPanel.agents.values()).some((agent) => agentsPanel.isActive(agent)) });
  inventoryPanel.setState({ connected, busy: busy || providerPanel.switching || providerPanel.saving || engagementPanel.switching });
  networkMap.setState({ connected, canWrite: inventoryPanel.canWrite() });
  scriptsPanel.setState({ connected });
  browserPanel.setState({ connected });
  const canInteract = connected && !engagementPanel.switching && !busy && !inventoryPanel.saving && !providerPanel.switching && !providerPanel.saving;
  input.disabled = !canInteract;
  sendButton.disabled = !canInteract;
  const canStop = connected && busy && pendingSubmission && pendingSubmission.canStop;
  const stopping = Boolean(pendingSubmission && pendingSubmission.stopping);
  stopButton.hidden = !canStop;
  stopButton.disabled = !canStop || stopping;
  stopButton.textContent = stopping ? "Stopping…" : "Stop";
  stopButton.setAttribute("aria-label", stopping ? "Stopping main run" : "Stop main run");
  const workflowStop = document.querySelector("#compliance-stop");
  workflowStop.hidden = !canStop || !workflowUsesCompliancePanel();
  workflowStop.disabled = !canStop || stopping;
  workflowStop.textContent = stopping ? "Stopping…" : "Stop workflow";
  syncApprovalTray();
  for (const button of modeButtons) {
    button.disabled = !canInteract;
  }
  ctfStartButton.disabled = !canInteract;
  ctfStopButton.disabled = !canInteract;
  renderApprovalPicker();

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
  } else if (socketIsOpen() || socket?.readyState === WebSocket.CONNECTING) {
    engineState.textContent = "Starting";
  } else {
    engineState.textContent = "Offline";
  }
}

// Gating changes stay available mid-run so a waiting run can be loosened or tightened.
function renderApprovalPicker() {
  const available = socketIsOpen() && sessionReady && !engagementPanel.switching;
  for (const button of approvalOptions) {
    const mode = button.dataset.approvalMode;
    const active = mode === idleApprovalState;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = !available;
  }
}

function requestApprovalMode(mode) {
  if (mode === (requestedApprovalMode ?? idleApprovalState)) return;
  if (sendSocketEvent({ type: "settings.approval", approval_mode: mode })) {
    requestedApprovalMode = mode;
  } else {
    appendActivity("Unable to change approval mode", true, "The console is offline.");
  }
}

function setWorkflow(name) {
  activeWorkflow = name || null;
  const ctfActive = activeWorkflow === "ctf";
  ctfObjectiveState.textContent = ctfActive ? "Active in this engagement." : "Not active in this engagement.";
  ctfStartButton.hidden = ctfActive;
  ctfStopButton.hidden = !ctfActive;
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
  const rows = stream.querySelectorAll(":scope > .activity-group:not(.saved-activity) > .activity-events > .activity-item");
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

function runClientCommand(name) {
  input.value = "";
  resizeInput();
  commandSuggestions.close();
  if (name === "/help") {
    appendMessage("user", "You", name);
    renderCommandHelp();
  }
}

function renderCommandHelp() {
  const list = document.createElement("dl");
  list.className = "command-help";
  for (const command of commandSuggestions.commands) {
    list.append(textElement("dt", command.name), textElement("dd", command.summary));
    for (const arg of command.args) {
      const term = textElement("dt", `${command.name} ${arg.value}`);
      term.className = "command-help-arg";
      list.append(term, textElement("dd", arg.summary));
    }
  }
  appendCommandReply("Commands", { meta: "Type these in the composer", body: list });
}

function submitUserMessage(message, { showInConversation = true, preserveDraft = false, workflow = false, workflowRequest = null, projectRequest = null, conversationWorkflow = false } = {}) {
  const normalized = message.trim();
  if (!normalized || busy || engagementPanel.switching || inventoryPanel.saving || providerPanel.switching || providerPanel.saving || !sessionReady || !socketIsOpen()) {
    return false;
  }

  // Here rather than in the menu, so Send, Enter and a dismissed menu agree.
  const command = commandSuggestions.commandNamed(normalized);
  if (command && command.scope === "client") {
    runClientCommand(command.name);
    return true;
  }

  const messageId = createMessageId();
  pendingSubmission = {
    id: messageId,
    message: normalized,
    showInConversation,
    preserveDraft,
    workflow,
    conversationWorkflow,
    canStop: false,
    stopping: false,
    focusTarget: document.activeElement,
  };
  busy = true;
  syncControls();
  commandSuggestions.close();

  if (
    !sendSocketEvent({
      type: projectRequest ? "compliance.project.prepare" : workflow ? "compliance.run" : "user.message",
      ...(workflow ? { context_id: inventoryPanel.contextId } : {}),
      ...(workflowRequest ? { workflow_request: workflowRequest } : {}),
      message_id: messageId,
      ...(projectRequest ? { project_request: projectRequest } : { message: normalized }),
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
  if (data.type === "compliance.event") {
    if (!pendingSubmission?.workflow || payload.message_id !== pendingSubmission.id) return;
    const detail = payload.payload || {};
    const conversation = pendingSubmission.conversationWorkflow;
    inventoryPanel.compliance.handleWorkflowEvent(payload.event_type, detail, conversation);
    if (conversation && payload.event_type === "assistant.message") {
      completeAssistantMessage(detail);
    }
    if (payload.event_type === "tool.result") inventoryPanel.refresh();
    if (payload.event_type === "token.usage") tokenState.textContent = Number(payload.payload.total_tokens || 0).toLocaleString();
    return;
  }
  inventoryPanel.compliance.handleEvent(data.type, payload);

  if (data.type.startsWith("htb.")) {
    htbSettings.handleEvent(data.type, payload);
    return;
  }

  if (data.type.startsWith("mcp.")) {
    if (data.type === "mcp.result" && answersTypedCommand(payload)) {
      appendCommandReply(payload.title, mcpPanel.commandReply(payload));
    } else if (data.type === "mcp.result") {
      appendActivity(payload.title, payload.failed === true, payload.detail, payload.failed === true);
    }
    mcpPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type === "workflow.result") {
    setWorkflow(payload.workflow);
    if (answersTypedCommand(payload)) {
      appendCommandReply(payload.title, { body: payload.detail || "" });
    } else {
      appendActivity(payload.title, payload.failed === true, payload.detail, payload.failed === true);
    }
    return;
  }

  if (data.type.startsWith("engagement.")) {
    engagementPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type.startsWith("inventory.")) {
    inventoryPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type.startsWith("scripts.")) {
    scriptsPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type.startsWith("browser.")) {
    browserPanel.handleEvent(data.type, payload);
    return;
  }

  if (["provider.catalog", "provider.models", "provider.changed", "provider.settings", "provider.tested", "provider.configured", "provider.login_ready", "provider.login_waiting", "provider.login_committing", "provider.default_saved", "provider.selection_error"].includes(data.type)) {
    providerPanel.handleEvent(data.type, payload);
    return;
  }

  if (data.type === "session.ready") {
    sessionReady = true;
    setSocketState("Online", "online");
    agentsPanel.setConnected(true);
    idleEngineState = payload.provider_ready ? "Connected" : "Provider needed";
    idleApprovalState = payload.approval_mode || "normal";
    requestedApprovalMode = null;
    approvalState.textContent = idleApprovalState;
    providerState.textContent = payload.provider ? payload.provider.model : "Not ready";
    if (payload.provider) {
      providerState.title = payload.provider.provider || "";
    }
    providerPanel.setInitial(payload.provider, payload.provider_ready);
    commandSuggestions.setCommands(payload.commands || []);
    setMode(payload.mode, payload.mode_name);
    setWorkflow(payload.workflow);
    if (payload.error) {
      appendActivity("Provider not ready", true, payload.error);
    }
    syncControls();
    inventoryPanel.refresh();
    scriptsPanel.refresh();
    providerPanel.loadCatalog();
    engagementPanel.refresh();
    mcpPanel.refresh();
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
    scriptsPanel.refresh();
    engagementPanel.refresh();
    return;
  }

  if (data.type === "user.message.accepted") {
    if (pendingSubmission && pendingSubmission.id === payload.message_id) {
      pendingSubmission.canStop = payload.stoppable === true;
      // Only local commands are unstoppable, and a workflow is never one.
      pendingSubmission.typedCommand = payload.stoppable === false && pendingSubmission.showInConversation && !pendingSubmission.workflow;
      syncControls();
      if (pendingSubmission.showInConversation) {
        appendMessage("user", "You", pendingSubmission.message);
        if (!pendingSubmission.preserveDraft) {
          input.value = "";
          resizeInput();
        }
      }
    }
    return;
  }

  if (data.type === "user.message.rejected") {
    if (!pendingSubmission?.workflow) appendActivity("Message rejected", true, payload.detail || "Unknown message error");
    finishSubmission(payload.message_id);
    return;
  }

  if (data.type === "user.message.complete") {
    const workflow = pendingSubmission?.workflow;
    const stopped = pendingSubmission && pendingSubmission.id === payload.message_id && payload.stopped;
    if (finishSubmission(payload.message_id)) finishAssistantStream();
    if (stopped && !workflow) appendActivity("Run stopped");
    inventoryPanel.refresh();
    scriptsPanel.refresh();
    engagementPanel.refresh();
    return;
  }

  if (data.type === "user.message.stopping" || data.type === "user.message.stop_rejected") {
    if (!pendingSubmission || pendingSubmission.id !== payload.message_id || !pendingSubmission.canStop) {
      return;
    }
    pendingSubmission.stopping = data.type === "user.message.stopping";
    if (!pendingSubmission.stopping && pendingSubmission.workflow) {
      inventoryPanel.compliance.handleWorkflowEvent("error", { detail: payload.detail || "Unable to stop the workflow. Try again." }, pendingSubmission.conversationWorkflow);
    } else if (!pendingSubmission.stopping) {
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
    if (!pendingSubmission?.workflow) appendActivity(`${action}: ${payload.tool}`, !payload.approved, payload.reason || "");
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
    if (answersTypedCommand(payload)) {
      appendCommandReply(`Mode: ${payload.name}`, { body: payload.description });
    } else {
      appendActivity(`Mode: ${payload.name}`, false, payload.description);
    }
    return;
  }

  if (data.type === "mode.changed") {
    setWorkflow(null);
    finishAssistantStream();
    engagementPanel.contextChanged();
    agentsPanel.reset();
    scriptsPanel.reset();
    syncControls();
    inventoryPanel.refresh();
    setMode(payload.to, payload.name);
    tokenState.textContent = "0";
    resumedSavedHistory = false;
    stream.replaceChildren();
    appendCommandReply(`Mode switched to ${payload.name}`, {
      meta: "The previous conversation was cleared.",
      body: payload.description,
    });
    return;
  }

  if (data.type === "session.cleared") {
    setWorkflow(null);
    finishAssistantStream();
    engagementPanel.contextChanged();
    agentsPanel.reset();
    scriptsPanel.reset();
    syncControls();
    inventoryPanel.refresh();
    tokenState.textContent = "0";
    resumedSavedHistory = false;
    stream.replaceChildren();
    appendCommandReply("Conversation cleared", { body: "History and approvals granted this session were reset." });
    return;
  }

  if (data.type === "command.unsupported") {
    appendMessage("assistant", "Kerkov", payload.detail || "Unknown command.");
    return;
  }

  if (data.type === "settings.info" || data.type === "settings.changed") {
    if (answersTypedCommand(payload)) {
      appendCommandReply(payload.title, { body: payload.detail || "" });
    } else {
      appendActivity(payload.title, false, payload.detail);
    }
    // Approval (and autonomy, which moves approval too) owns a status chip.
    if (typeof payload.approval_mode === "string") {
      idleApprovalState = payload.approval_mode;
      if (requestedApprovalMode === idleApprovalState) requestedApprovalMode = null;
      if (approvalTray.hidden) approvalState.textContent = idleApprovalState;
      renderApprovalPicker();
    }
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
    // Errors carry no request id; a refused change must not swallow the next click.
    requestedApprovalMode = null;
    if (answersTypedCommand(payload)) {
      appendCommandReply("Command failed", { body: payload.detail || "Unknown error", failed: true });
    } else {
      appendActivity(payload.detail || "Unknown socket error", true);
    }
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

  socket.addEventListener("open", syncControls);

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
    inventoryPanel.compliance.disconnected();
    expireApprovals();
    pendingSubmission = null;
    requestedApprovalMode = null;
    setSocketState("Offline", "offline");
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
document.querySelector("#compliance-stop").addEventListener("click", stopSubmission);

input.addEventListener("input", resizeInput);
document.querySelector(".skip-link").addEventListener("click", () => {
  inventoryPanel.showView("conversation", false);
});
input.addEventListener("keydown", (event) => {
  if (commandSuggestions.handleKeydown(event)) {
    return;
  }
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

ctfStartButton.addEventListener("click", () => {
  submitUserMessage("/workflow ctf start", { showInConversation: false });
});
ctfStopButton.addEventListener("click", () => {
  submitUserMessage("/workflow ctf stop", { showInConversation: false });
});

for (const button of modeButtons) {
  button.addEventListener("click", () => {
    const mode = button.dataset.mode;
    if (mode && button.getAttribute("aria-pressed") !== "true") {
      submitUserMessage(`/mode ${mode}`, { showInConversation: false });
    }
  });
}

for (const button of approvalOptions) {
  button.addEventListener("click", () => requestApprovalMode(button.dataset.approvalMode));
}

connectSocket();
