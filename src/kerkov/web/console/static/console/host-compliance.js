/* Compliance actions use the shared conversation, execution and approval loop. */
class HostCompliance {
  constructor(inventory) {
    this.inventory = inventory;
    this.form = this.el("compliance-form");
    this.baselines = JSON.parse(this.el("compliance-baseline-catalog").textContent);
    this.selectedId = null;
    this.selectedIdentity = null;
    this.baselineId = this.baselines[0].id;
    this.run = null;
    this.workflowId = null;
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessTimer = null;
    this.accessCandidates = [];
    this.accessDirty = false;
    this.accessSource = null;
    this.records = new Map();
    this.reviewedPreview = null;
    this.savedRun = null;
    this.assessmentOf = null;
    this.historyContext = null;
    this.el("compliance-history-refresh").addEventListener("click", () => this.refreshHistory());
    this.el("compliance-reassess").addEventListener("click", () => this.restoreSettings(this.savedRun, true));
    this.el("compliance-review-apply").addEventListener("click", () => {
      this.restoreSettings(this.savedRun);
      this.reviewedPreview = this.savedRun;
      this.text("compliance-status", "Preview selected. Review its settings and outcomes, then choose Apply reviewed changes. Tool approvals still apply.");
    });
    this.form.addEventListener("submit", (event) => { event.preventDefault(); this.start("apply"); });
    this.form.addEventListener("input", (event) => {
      if (["user", "port", "private_key_file", "known_hosts_file", "sudo"].includes(event.target.name)) {
        this.accessDirty = true;
        this.accessSource = null;
        this.el("compliance-access-choice").hidden = !this.accessCandidates.length;
      }
      this.connectionSummary();
    });
    this.el("compliance-access-refresh").addEventListener("click", () => this.requestAccess(true));
    this.el("compliance-access-find").addEventListener("click", () => this.findAccess());
    this.el("compliance-access-select").addEventListener("change", (event) => {
      const candidate = this.accessCandidates.find(item => String(item.id) === event.target.value);
      if (candidate) { this.applyAccess(candidate); this.accessDirty = true; }
    });
    for (const button of this.form.querySelectorAll("[data-compliance-action]")) {
      button.addEventListener("click", () => this.start(button.dataset.complianceAction));
    }
    this.el("compliance-add-asset").addEventListener("click", () => {
      if (!this.inventory.canWrite()) return;
      this.inventory.showView("inventory");
      this.inventory.openForm();
      this.el("inventory-role").value = "assets";
      this.el("inventory-kind").value = "ip";
      this.inventory.renderValueHint();
    });
    this.el("compliance-welcome").addEventListener("click", () => this.inventory.showView("compliance"));
    this.el("compliance-open-conversation").addEventListener("click", () => this.inventory.showView("conversation"));
    this.renderBaselines();
    this.renderSelection();
  }

  el(id) { return document.getElementById(id); }
  field(name) { return this.form.elements.namedItem(name); }
  host() {
    const host = this.inventory.entries.get(this.selectedId);
    return host && ["ip", "domain"].includes(host.entry.kind) ? host : null;
  }
  baseline() { return this.baselines.find(item => item.id === this.baselineId); }
  text(id, value) { this.el(id).textContent = value; }
  node(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }

  reset() {
    this.records.clear();
    this.savedRun = null;
    this.reviewedPreview = null;
    this.historyContext = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.el("compliance-history").replaceChildren();
    this.resetAccess();
    this.form.reset();
    this.selectedId = null;
    this.selectedIdentity = null;
    this.run = null;
    this.workflowId = null;
    this.el("compliance-connection").open = false;
    this.el("compliance-result").hidden = true;
    this.el("compliance-workflow-details").hidden = true;
    this.el("compliance-workflow-output").textContent = "";
    this.el("compliance-assets").replaceChildren();
    this.el("compliance-empty").hidden = false;
    this.renderSelection();
    this.text("compliance-status", "");
  }

  selectResource(id, navigate = true) {
    let entry = this.inventory.entries.get(id);
    if (!entry || !["ip", "domain"].includes(entry.entry.kind)) return;
    entry = this.hosts().find(row => row.entry.kind === entry.entry.kind && row.entry.value === entry.entry.value) || entry;
    const identity = JSON.stringify([this.inventory.contextId, entry.id, entry.entry.kind, entry.entry.value]);
    if (identity !== this.selectedIdentity) {
      this.assessmentOf = null;
      this.reviewedPreview = null;
      this.resetAccess();
      this.form.reset();
      this.el("compliance-connection").open = false;
      this.text("compliance-status", "");
    }
    this.selectedId = entry.id;
    this.selectedIdentity = identity;
    this.renderAssets();
    this.renderSelection();
    this.requestAccess();
    if (navigate) this.inventory.showView("compliance");
  }

  snapshot() {
    if (this.historyContext !== this.inventory.contextId) {
      this.historyContext = this.inventory.contextId;
      this.refreshHistory();
    }
    const hosts = this.hosts();
    if (hosts.length) this.selectResource(this.host() ? this.selectedId : hosts[0].id, false);
    else {
      this.resetAccess();
      this.selectedId = null;
      this.selectedIdentity = null;
      this.form.reset();
      this.renderAssets();
      this.renderSelection();
    }
  }

  hosts() {
    const rows = Array.from(this.inventory.entries.values())
      .filter(row => ["ip", "domain"].includes(row.entry.kind))
      .sort((a, b) => Number(b.section === "assets") - Number(a.section === "assets") || a.id - b.id);
    const seen = new Set();
    return rows.filter(row => {
      const identity = JSON.stringify([row.entry.kind, row.entry.value]);
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
  }

  renderAssets() {
    const hosts = this.hosts();
    this.el("compliance-empty").hidden = !!hosts.length;
    this.el("compliance-assets").replaceChildren(...hosts.map(row => {
      const button = this.node("button", "compliance-asset", "");
      button.type = "button";
      button.dataset.complianceAsset = row.id;
      button.setAttribute("aria-pressed", String(row.id === this.selectedId));
      button.append(this.node("span", "compliance-asset-icon", "▣"),
        this.node("strong", "", row.entry.name || row.entry.value),
        this.node("span", "compliance-asset-address", row.entry.value),
        this.node("span", "compliance-asset-role", row.section === "assets" ? "Asset" : "Target"));
      button.addEventListener("click", () => this.selectResource(row.id));
      return button;
    }));
    this.setState();
  }

  renderBaselines() {
    const items = [...this.baselines, { id: "custom", name: "Your own baseline", description: "Connect an existing Cinc profile and Ansible playbook.", platform: "Custom" }];
    this.el("compliance-baselines").replaceChildren(...items.map(item => {
      const button = this.node("button", "compliance-baseline", "");
      button.type = "button";
      button.dataset.complianceBaseline = item.id;
      button.setAttribute("aria-pressed", String(item.id === this.baselineId));
      button.append(this.node("span", "compliance-baseline-meta", item.recommended ? "RECOMMENDED START" : item.platform),
        this.node("strong", "", item.name), this.node("span", "", item.description),
        this.node("span", "compliance-baseline-count", item.controls ? `${item.controls.length} checks · fixes included` : "Bring your own profile"));
      button.addEventListener("click", () => {
        this.baselineId = item.id;
        this.renderBaselines();
        this.renderSelection();
      });
      return button;
    }));
  }

  renderSelection() {
    const host = this.host();
    const baseline = this.baseline();
    this.el("compliance-custom").hidden = !!baseline;
    this.text("compliance-selection", host ? `${host.entry.name || host.entry.value} → ${baseline?.name || "Your own baseline"}` : "Choose an asset to get started.");
    this.text("compliance-changes", baseline?.changes || "Use the reviewed assessment and remediation profiles configured below.");
    this.text("compliance-coverage", baseline ? `${baseline.platform} · Built-in baseline v${baseline.version} · Covers these specific checks, not full CIS or PCI certification.` : "Your profile defines its coverage and supported operating systems.");
    this.connectionSummary();
    this.setState();
  }

  connectionSummary() {
    const ready = ["user", "private_key_file", "known_hosts_file"].every(name => this.field(name).value.trim());
    this.text("compliance-connection-summary", ready ? (this.accessSource ? "Access from engagement" : "Connection details entered") : "Set up access");
  }

  setState() {
    const writable = this.inventory.canWrite();
    for (const control of this.form.elements) control.disabled = !writable || !this.host();
    for (const button of this.el("compliance-assets").querySelectorAll("button")) button.disabled = !writable;
    for (const button of this.el("compliance-baselines").querySelectorAll("button")) button.disabled = !writable;
    this.el("compliance-add-asset").disabled = !writable;
    this.el("compliance-access-select").disabled = !writable || !this.host();
    this.el("compliance-access-find").disabled = !writable || !this.host();
    this.el("compliance-access-refresh").disabled = !writable || !this.host() || !!this.accessRequest;
    this.el("compliance-review-apply").disabled = !writable;
    this.el("compliance-reassess").disabled = !writable || !this.savedRun || this.savedRun.status === "running";
    this.el("compliance-history-refresh").disabled = !this.inventory.connected;
    for (const button of this.el("compliance-evidence-actions").querySelectorAll("button")) button.disabled = !this.inventory.connected;
  }

  resetAccess() {
    clearTimeout(this.accessTimer);
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessCandidates = [];
    this.accessDirty = false;
    this.accessSource = null;
    this.el("compliance-access-choice").hidden = true;
    this.el("compliance-access-select").replaceChildren();
    this.text("compliance-access-status", "Recorded access is retrieved from this engagement.");
  }

  requestAccess(force = false) {
    const host = this.host();
    if (!host || !this.inventory.connected || !this.inventory.available || !this.inventory.contextId) return;
    if (this.accessRequest && !force) { this.accessRefreshAgain = true; return; }
    this.accessRefreshAgain = false;
    clearTimeout(this.accessTimer);
    const request = { id: this.inventory.requestId(), identity: this.selectedIdentity };
    this.accessRequest = request;
    if (!this.inventory.send({ type: "inventory.ssh_access", request_id: request.id,
      context_id: this.inventory.contextId, entry_id: host.id })) {
      this.accessRequest = null;
      this.text("compliance-access-status", "Recorded access could not be retrieved. Enter details or try Refresh.");
    } else this.accessTimer = window.setTimeout(() => {
      if (this.accessRequest !== request) return;
      this.accessRequest = null;
      this.text("compliance-access-status", "Access lookup did not respond. Enter details or try Refresh.");
      this.setState();
    }, 15000);
    this.setState();
  }

  applyAccess(candidate) {
    const fields = candidate.fields;
    for (const name of ["user", "port", "private_key_file", "known_hosts_file"]) this.field(name).value = fields[name] ?? "";
    this.field("sudo").checked = fields.sudo ?? true;
    this.accessSource = candidate.id;
    this.text("compliance-access-status", candidate.complete
      ? `Using recorded SSH access from ledger #${candidate.id}. Authentication is checked when the workflow runs.`
      : `Loaded partial access from ledger #${candidate.id}. Complete the missing connection details.`);
    this.connectionSummary();
  }

  receiveAccess(type, payload) {
    const request = this.accessRequest;
    if (!request || payload.request_id !== request.id || payload.context_id !== this.inventory.contextId
        || request.identity !== this.selectedIdentity) return;
    if (type !== "inventory.ssh_access" && type !== "inventory.error") return;
    this.accessRequest = null;
    clearTimeout(this.accessTimer);
    if (this.accessRefreshAgain) {
      this.accessRefreshAgain = false;
      queueMicrotask(() => { if (this.selectedIdentity === request.identity) this.requestAccess(); });
    }
    if (type === "inventory.error") {
      this.text("compliance-access-status", payload.detail || "Recorded access could not be retrieved.");
      this.setState();
      return;
    }
    const host = this.host();
    if (!host || payload.entry_id !== host.id || payload.resource?.kind !== host.entry.kind || payload.resource?.value !== host.entry.value) { this.setState(); return; }
    this.accessCandidates = payload.candidates || [];
    const select = this.el("compliance-access-select");
    select.replaceChildren(this.node("option", "", "Choose recorded access…"));
    select.firstChild.value = "";
    for (const candidate of this.accessCandidates) {
      const option = this.node("option", "", `${candidate.fields.user} · port ${candidate.fields.port ?? "not recorded"} · ledger #${candidate.id}`);
      option.value = String(candidate.id);
      select.append(option);
    }
    if (!this.accessDirty && ((!this.accessCandidates.length && this.accessSource) || this.accessCandidates.length > 1 || payload.truncated)) {
      for (const name of ["user", "port", "private_key_file", "known_hosts_file"]) this.field(name).value = "";
      this.accessSource = null;
      this.connectionSummary();
    }
    this.el("compliance-access-choice").hidden = !this.accessCandidates.length || (this.accessCandidates.length === 1 && !this.accessDirty && !payload.truncated);
    if (!this.accessDirty && !payload.truncated && this.accessCandidates.length === 1) {
      this.applyAccess(this.accessCandidates[0]);
      select.value = String(this.accessCandidates[0].id);
    } else this.text("compliance-access-status", this.accessCandidates.length
      ? (this.accessDirty ? "Recorded access is available. Your connection edits were kept." : "Multiple SSH connections are recorded. Choose the intended endpoint.")
      : "No recorded SSH file references found. Use Find known access to recover details from this engagement, or enter them here.");
    this.setState();
  }

  findAccess() {
    const host = this.host();
    if (!host || !this.inventory.canWrite()) return;
    const message = [
      `Find existing SSH access for the selected asset: kind=${JSON.stringify(host.entry.kind)}, host=${JSON.stringify(host.entry.value)}, ledger entry_id=${host.id}.`,
      "Read its ledger profile and credentials, the current conversation, and use recall for earlier container setup/tool output if needed. Recover only previously recorded connection facts: exact reachable SSH host, explicit SSH port, username, existing controller-local private_key_file and independently trusted known_hosts_file references, and sudo when known.",
      "Record confirmed access with ledger op='add', section='credentials', entry containing kind='ssh', host, username, port, private_key_file, known_hosts_file and sudo when known. Different published ports are different SSH endpoints; never guess from a Docker mapping or propagate keys across hosts. If multiple candidates exist, retain their distinct ports and explain the choice.",
      "This is access discovery only: do not start/stop containers, scan, authenticate, remediate, create keys, write credential files, or add host-key trust. Do not print private-key contents, passwords or tokens in your reply. If only a raw secret or password exists and the required file references are unavailable, explain what is missing. The GUI will retrieve the recorded file references; normal tool approvals apply.",
    ].join("\n\n");
    const messageId = this.inventory.startWorkflow(message);
    if (messageId) {
      this.workflowId = messageId;
      this.text("compliance-status", "Finding recorded access…");
      this.el("compliance-workflow-output").textContent = "";
    }
  }

  start(action) {
    const host = this.host();
    if (!host || !this.inventory.canWrite() || !["assess", "preview", "apply"].includes(action)) return;
    const baseline = this.baseline();
    const required = ["user", "port", "private_key_file", "known_hosts_file"];
    const customFields = ["profile", "inputs_file", "automation_profile", "playbook", "host_alias", "vars_file"];
    if (!baseline) required.push("profile", ...(action === "assess" ? [] : ["automation_profile", "playbook", "host_alias"]));
    const values = {};
    for (const input of this.form.querySelectorAll("input")) {
      if (input.type === "checkbox") continue;
      input.required = required.includes(input.name);
      if (customFields.includes(input.name) && (baseline || (action === "assess" && !["profile", "inputs_file"].includes(input.name)))) continue;
      values[input.name] = input.value.trim();
      if (!input.checkValidity() || (input.required && !values[input.name]) || /[\x00-\x1f\x7f]/.test(values[input.name])) {
        this.el(input.closest("#compliance-custom") ? "compliance-custom" : "compliance-connection").open = true;
        this.text("compliance-status", "Complete the connection and selected profile settings to continue.");
        input.focus();
        input.reportValidity();
        return;
      }
    }
    if (!baseline && action !== "assess" && ["all", "ungrouped"].includes(values.host_alias)) {
      this.text("compliance-status", "Choose a single host alias, not an inventory group.");
      return;
    }
    const scan = {
      host: host.entry.value, user: values.user, port: Number(values.port),
      profile: baseline?.profile || values.profile,
      private_key_file: values.private_key_file, known_hosts_file: values.known_hosts_file, sudo: this.field("sudo").checked,
    };
    if (!baseline && values.inputs_file) scan.inputs_file = values.inputs_file;
    const remediation = baseline || action === "assess" ? null : {
      action: "run", profile: values.automation_profile, playbook: values.playbook,
      limit: values.host_alias, check: action === "preview", bootstrap_python: true,
      vars_files: values.vars_file ? [values.vars_file] : [],
    };
    const message = `${action} compliance for ${host.entry.value} against ${baseline?.name || "the selected custom baseline"}.`;
    const workflowRequest = {
      action, resource_id: host.id, baseline_id: baseline?.id || null, scan,
      remediation: baseline || action === "assess" ? null : remediation,
      preview_id: action === "apply" ? this.reviewedPreview?.id || null : null,
      assessment_of: action === "assess" ? this.assessmentOf : null,
    };
    if (action === "apply" && !workflowRequest.preview_id) {
      this.text("compliance-status", "Preview changes first, then review and select the saved preview before applying.");
      return;
    }
    const messageId = this.inventory.startWorkflow(message, workflowRequest);
    if (!messageId) { this.text("compliance-status", "Unable to start. Check the connection and wait for current work to finish."); return; }
    this.savedRun = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.workflowId = messageId;
    this.run = { messageId, host: host.entry.value, action, label: baseline?.name || "Custom baseline" };
    this.result("Workflow started", `${this.run.host} · ${this.run.label}. Progress, approvals and evidence appear here.`);
    this.el("compliance-workflow-output").textContent = "";
    this.text("compliance-status", "Starting workflow…");
  }

  result(title, detail) {
    this.el("compliance-result").hidden = false;
    this.text("compliance-result-title", title);
    this.text("compliance-result-detail", detail);
  }

  handleWorkflowEvent(type, payload) {
    this.handleEvent(type, payload);
    this.el("compliance-workflow-details").hidden = false;
    if (type === "tool.start") {
      const stages = { compliance_scan: this.savedRun?.stage === "verification" ? "Verifying baseline…" : "Assessing baseline…",
        ansible: this.run?.action === "preview" ? "Previewing changes…" : "Applying baseline…",
        write: "Preparing configuration…", ledger: "Updating engagement context…", recall: "Retrieving known context…" };
      this.text("compliance-status", stages[payload.name] || "Working…");
    }
    if (type === "tool.result") {
      const output = this.el("compliance-workflow-output");
      output.textContent += `\n${payload.success ? "Finished" : "Failed"}: ${payload.name}\n${payload.error || payload.output || ""}\n`;
      output.textContent = output.textContent.slice(-32000);
    }
    if (["assistant.message", "error", "provider.error", "log.warning"].includes(type)) {
      const output = this.el("compliance-workflow-output");
      output.textContent = (output.textContent + "\n" + (payload.content || payload.detail || payload.message || "")).slice(-32000);
    }
  }

  handleEvent(type, payload) {
    if (type === "compliance.snapshot" || type === "compliance.updated") {
      if (payload.context_id !== this.inventory.contextId) return;
      const records = type === "compliance.snapshot" ? payload.runs : [payload.run];
      if (type === "compliance.snapshot") this.records.clear();
      for (const record of records || []) this.records.set(record.id, record);
      this.renderHistory();
      if (type === "compliance.updated") this.showSavedRun(payload.run);
      else if (this.savedRun && this.records.has(this.savedRun.id)) this.showSavedRun(this.records.get(this.savedRun.id));
      else if (!this.savedRun && records?.length) this.showSavedRun(records[0]);
      return;
    }
    if (type === "compliance.evidence") {
      if (payload.context_id !== this.inventory.contextId || payload.run_id !== this.savedRun?.id) return;
      this.el("compliance-evidence-preview").hidden = false;
      this.text("compliance-evidence-preview", payload.body || payload.detail || "No evidence available.");
      return;
    }
    this.receiveAccess(type, payload);
    if (type === "approval.request" && this.workflowId) this.text("compliance-status", "Approval required to continue.");
    if (payload.message_id === this.workflowId && ["user.message.complete", "user.message.rejected"].includes(type)) {
      this.text("compliance-status", payload.stopped ? "Workflow stopped." : type === "user.message.rejected" ? payload.detail : "Workflow finished. Results are available to the conversation.");
      this.workflowId = null;
    }
    if (!this.run) return;
    const run = this.run;
    if (["user.message.complete", "user.message.rejected"].includes(type) && payload.message_id === run.messageId) {
      if (!this.savedRun || this.savedRun.status === "running") this.result(
        payload.stopped ? "Workflow stopped" : type === "user.message.rejected" ? "Workflow could not start" : "Refreshing saved assessment",
        payload.detail || "No verified assessment result received yet. Refresh assessment history to review the server-recorded outcome.",
      );
      this.run = null;
      this.workflowId = null;
      this.refreshHistory();
    }
  }

  refreshHistory() {
    if (!this.inventory.connected || !this.inventory.contextId) return;
    this.inventory.send({ type: "compliance.list", context_id: this.inventory.contextId });
  }

  renderHistory() {
    const records = Array.from(this.records.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
    this.el("compliance-history").replaceChildren(...records.map(record => {
      const button = this.node("button", "compliance-history-item", "");
      button.type = "button";
      button.append(this.node("strong", "", `${record.request.scan.host} · ${record.label}`),
        this.node("span", "", `${record.status} · ${new Date(record.updated_at).toLocaleString()}`));
      button.addEventListener("click", () => this.showSavedRun(record));
      return button;
    }));
    if (!records.length) this.el("compliance-history").append(this.node("p", "compliance-caption", "No saved assessments in this engagement."));
  }

  showSavedRun(record) {
    this.savedRun = record;
    this.el("compliance-run-review").hidden = false;
    this.el("compliance-evidence-preview").hidden = true;
    const titles = { running: "Workflow in progress", previewed: "Preview ready for review", verified: "Baseline verified", passed: "Baseline checks passed", attention: "Baseline needs attention", interrupted: "Workflow interrupted", failed: "Workflow needs review", blocked: "Workflow blocked", incomplete: "Workflow incomplete" };
    this.result(titles[record.status] || record.status, `${record.request.scan.host} · ${record.label}. ${record.detail}`);
    this.text("compliance-run-time", new Date(record.updated_at).toLocaleString());
    const stages = record.request.action === "assess" ? ["readiness", "assessment"] : record.request.action === "preview" ? ["readiness", "assessment", "preview"] : ["readiness", "assessment", "remediation", "verification"];
    this.el("compliance-stages").replaceChildren(...stages.map(stage => {
      const node = this.node("li", "", stage);
      if (stage === record.stage) node.setAttribute("aria-current", "step");
      return node;
    }));
    this.el("compliance-readiness").replaceChildren(...(record.readiness || []).map(check => this.node("li", `compliance-check-${check.status}`, `${check.name}: ${check.detail}`)));
    this.text("compliance-plan-summary", record.plan?.changes || "Preparing plan…");
    const evidence = {};
    for (const phase of ["before", "after", "remediation"]) {
      if (record[phase]) evidence[phase] = Object.fromEntries(Object.entries(record[phase]).filter(([key]) => key.endsWith("artifact") || key === "artifact_directory" || key === "profile_sha256"));
    }
    this.text("compliance-plan-evidence", JSON.stringify({ plan: record.plan, evidence }, null, 2));
    const controls = record.controls || [];
    this.el("compliance-controls").replaceChildren(...controls.map(control => {
      const row = this.node("tr", "", "");
      const title = this.node("td", "", "");
      title.append(this.node("strong", "", control.control_id), this.node("span", "", control.title || control.profile));
      row.append(title, this.node("td", "", control.impact == null ? "Unknown" : String(control.impact)));
      for (const phase of ["before", "after"]) row.append(this.node("td", `compliance-outcome-${control[phase]}`, (control[phase] || "unassessed").replaceAll("_", " ")));
      return row;
    }));
    const report = record.after || record.before;
    const counts = report?.counts;
    this.text("compliance-controls-notice", counts ? `${Object.entries(counts).map(([status, count]) => `${count} ${status.replaceAll("_", " ")}`).join(" · ")}. ${report.controls_truncated ? "Showing a bounded control list; open normalized evidence for complete results. " : ""}An unassessed after column means verification has not been recorded.` : "No verified control evidence received yet.");
    const actions = [];
    for (const phase of ["before", "after", "remediation"]) {
      if (!record[phase]) continue;
      const button = this.node("button", "inventory-button", `${phase === "remediation" ? "Remediation log" : `${phase} evidence`}`);
      button.type = "button";
      button.addEventListener("click", () => this.inventory.send({ type: "compliance.evidence", context_id: this.inventory.contextId, run_id: record.id, phase }));
      actions.push(button);
    }
    this.el("compliance-evidence-actions").replaceChildren(...actions);
    this.el("compliance-review-apply").hidden = record.status !== "previewed";
    this.el("compliance-review-apply").disabled = !this.inventory.canWrite();
    this.el("compliance-reassess").disabled = !this.inventory.canWrite() || record.status === "running";
  }

  restoreSettings(record, assess = false) {
    if (!record || !this.inventory.canWrite()) return;
    const request = record.request;
    if (!this.inventory.entries.has(request.resource_id)) {
      this.text("compliance-status", "The saved asset is unavailable. Refresh inventory before reassessing.");
      return;
    }
    this.selectResource(request.resource_id);
    this.baselineId = request.baseline_id || "custom";
    this.renderBaselines();
    this.renderSelection();
    for (const [key, value] of Object.entries(request.scan)) {
      const field = this.field(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = value;
      else field.value = value || "";
    }
    if (request.remediation) {
      for (const [field, key] of [["automation_profile", "profile"], ["playbook", "playbook"], ["host_alias", "limit"]]) this.field(field).value = request.remediation[key] || "";
      this.field("vars_file").value = request.remediation.vars_files?.[0] || "";
    }
    this.accessDirty = true;
    this.connectionSummary();
    if (assess) {
      this.assessmentOf = record.id;
      this.start("assess");
    }
  }

  disconnected() {
    if (!this.workflowId) return;
    this.text("compliance-status", "Connection lost. Resume the engagement to review the saved evidence.");
    if (this.run) this.result("Workflow connection lost", "The final outcome is unknown. Review saved evidence before retrying changes.");
    this.run = null;
    this.workflowId = null;
  }

}
