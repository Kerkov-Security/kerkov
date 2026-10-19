/* The engagement ledger is authoritative; resource values are always plain text. */
class InventoryPanel {
  constructor({ send, requestId, useInConversation, onPendingChange, onViewChange, onSnapshot = () => {}, onStatus = () => {} }) {
    this.send = send;
    this.requestId = requestId;
    this.useInConversation = useInConversation;
    this.onPendingChange = onPendingChange;
    this.onViewChange = onViewChange;
    this.onSnapshot = onSnapshot;
    this.onStatus = onStatus;
    this.views = ["conversation", "inventory", "network", "scripts", "engagement"];
    this.entries = new Map();
    this.rows = new Map();
    this.contextId = null;
    this.retiredContexts = new Set();
    this.selectedId = null;
    this.connected = false;
    this.busy = false;
    this.available = false;
    this.availabilityDetail = "Connecting to inventory…";
    this.listRequest = null;
    this.pendingSave = null;
    this.evidenceRequest = null;
    this.evidence = null;
    this.editing = null;
    this.view = "conversation";
    this.refreshTimer = null;
    this.requestTimer = null;
    this.saveTimer = null;
    this.refreshAgain = false;
    this.labels = { url: "URL", domain: "Domain", ip: "IP address", repository: "Repository" };
    this.elements = {};
    for (const id of [
      "conversation-tab", "inventory-tab", "conversation-view", "inventory-view",
      "network-tab", "network-view", "scripts-tab", "scripts-view",
      "engagement-tab", "engagement-view",
      "inventory-count", "inventory-assets-count", "inventory-targets-count",
      "inventory-manage", "inventory-assets-link", "inventory-targets-link",
      "inventory-summary-status", "inventory-status", "inventory-add",
      "inventory-assets", "inventory-targets", "inventory-assets-empty", "inventory-targets-empty",
      "inventory-selection-empty", "inventory-detail", "inventory-detail-icon",
      "inventory-detail-role", "inventory-detail-name", "inventory-detail-kind",
      "inventory-address-label", "inventory-detail-value", "inventory-detail-created",
      "inventory-other-role", "inventory-other-role-label", "inventory-use", "inventory-edit",
      "inventory-evidence", "inventory-evidence-open", "inventory-evidence-content",
      "inventory-evidence-title", "inventory-evidence-body", "inventory-evidence-truncated",
      "inventory-evidence-error", "inventory-form", "inventory-form-heading",
      "inventory-environment-row", "inventory-tags-row", "inventory-notes-row",
      "inventory-detail-environment", "inventory-detail-tags", "inventory-detail-notes",
      "inventory-environment", "inventory-tags", "inventory-notes", "inventory-more-details",
      "inventory-role", "inventory-kind", "inventory-value", "inventory-value-label",
      "inventory-value-hint", "inventory-name", "inventory-form-error", "inventory-save", "inventory-cancel",
    ]) {
      this.elements[id] = document.getElementById(id);
    }
    this.el("conversation-tab").addEventListener("click", () => this.showView("conversation"));
    this.el("inventory-tab").addEventListener("click", () => this.showView("inventory"));
    this.el("network-tab").addEventListener("click", () => this.showView("network"));
    this.el("scripts-tab").addEventListener("click", () => this.showView("scripts"));
    this.el("engagement-tab").addEventListener("click", () => this.showView("engagement"));
    this.el("inventory-manage").addEventListener("click", () => this.showView("inventory"));
    for (const section of ["assets", "targets"]) {
      this.el(`inventory-${section}-link`).addEventListener("click", () => {
        const first = Array.from(this.entries.values()).find((entry) => entry.section === section);
        if (first && !this.editing) this.select(first.id);
        this.showView("inventory");
      });
    }
    for (const tab of this.views.map((name) => this.el(`${name}-tab`))) {
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const index = this.views.indexOf(this.view);
        const view = event.key === "Home" ? this.views[0] : event.key === "End" ? this.views.at(-1)
          : this.views[(index + (event.key === "ArrowRight" ? 1 : this.views.length - 1)) % this.views.length];
        this.showView(view);
      });
    }
    this.el("inventory-add").addEventListener("click", () => this.openForm());
    this.el("inventory-edit").addEventListener("click", () => this.openForm(this.entries.get(this.selectedId)));
    this.el("inventory-cancel").addEventListener("click", () => {
      if (this.pendingSave) return;
      this.editing = null;
      this.renderDetails();
      this.renderControls();
      (this.rows.get(this.selectedId) || this.el("inventory-add")).focus();
    });
    this.el("inventory-kind").addEventListener("change", () => this.renderValueHint());
    this.el("inventory-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.saveResource();
    });
    this.el("inventory-use").addEventListener("click", () => {
      const entry = this.entries.get(this.selectedId);
      if (!entry || !this.canWrite()) return;
      if (this.useInConversation(entry)) this.showView("conversation", false);
    });
    this.el("inventory-evidence-open").addEventListener("click", () => this.openEvidence());
    this.profile = new ResourceProfile(this);
    document.getElementById("inventory-profile-open").addEventListener("click", () => this.profile.openResource(this.selectedId));
  }

  el(id) { return this.elements[id]; }

  setText(id, value) {
    const element = this.el(id);
    const text = String(value ?? "");
    if (element.textContent !== text) element.textContent = text;
    if (id === "inventory-status") this.onStatus(text);
  }

  get saving() { return this.pendingSave !== null; }

  canWrite() { return this.connected && this.available && !this.busy && !this.saving && this.contextId !== null; }

  showView(view, focusTab = true) {
    this.view = view;
    for (const name of this.views) {
      const active = name === view;
      this.el(`${name}-view`).hidden = !active;
      this.el(`${name}-tab`).setAttribute("aria-selected", String(active));
      this.el(`${name}-tab`).tabIndex = active ? 0 : -1;
    }
    if (focusTab) this.el(`${view}-tab`).focus();
    if (view === "inventory" || view === "network") this.refresh();
    this.onViewChange(view);
  }

  setState({ connected, busy }) {
    const wasConnected = this.connected;
    this.connected = connected;
    this.busy = busy;
    if (!connected && wasConnected) {
      clearTimeout(this.refreshTimer);
      clearTimeout(this.requestTimer);
      clearTimeout(this.saveTimer);
      this.refreshTimer = null;
      this.listRequest = null;
      this.refreshAgain = false;
      this.evidenceRequest = null;
      if (this.pendingSave) {
        this.pendingSave = null;
        this.formError("Disconnected before the save was confirmed. Check the saved resource when you reopen the engagement.");
        this.onPendingChange();
      }
      this.setText("inventory-status", "Disconnected. Showing the last received inventory.");
    }
    this.renderControls();
  }

  refresh() {
    if (!this.connected) return;
    if (this.listRequest) {
      this.refreshAgain = true;
      return;
    }
    if (this.refreshTimer !== null) return;
    this.refreshTimer = window.setTimeout(() => {
      this.refreshTimer = null;
      if (!this.connected) return;
      const id = this.requestId();
      this.listRequest = id;
      if (!this.send({ type: "inventory.list", request_id: id })) {
        this.listRequest = null;
        this.setText("inventory-status", "Unable to load inventory. Open this tab to try again.");
        return;
      }
      this.requestTimer = window.setTimeout(() => {
        if (this.listRequest !== id) return;
        this.listRequest = null;
        this.refreshAgain = false;
        this.setText("inventory-status", "Inventory did not respond. Open this tab to try again.");
      }, 15000);
    }, 100);
  }

  handleEvent(type, payload) {
    this.profile.handleEvent(type, payload);
    if (type === "inventory.snapshot") {
      this.receiveSnapshot(payload);
    } else if (type === "inventory.saved") {
      if (payload.context_id === this.contextId && payload.request_id === this.pendingSave?.id) {
        this.pendingSave.entryId = payload.entry_id;
      }
    } else if (type === "inventory.error") {
      if (payload.context_id && payload.context_id !== this.contextId && this.contextId !== null) return;
      if (payload.request_id === this.pendingSave?.id) {
        this.finishSave();
        this.formError(payload.detail || "The resource could not be saved.");
        this.refresh();
      } else if (payload.request_id === this.evidenceRequest?.id) {
        this.evidenceRequest = null;
        this.setText("inventory-evidence-error", payload.detail || "Evidence is unavailable.");
        this.el("inventory-evidence-error").hidden = false;
        this.renderControls();
      } else if (payload.request_id === this.listRequest) {
        this.finishList();
        this.setText("inventory-status", payload.detail || "Inventory is unavailable.");
      }
    } else if (type === "inventory.evidence") {
      if (payload.context_id !== this.contextId || payload.request_id !== this.evidenceRequest?.id
          || payload.entry_id !== this.selectedId) return;
      this.evidenceRequest = null;
      if (!payload.artifact || typeof payload.artifact.body !== "string") return;
      this.evidence = { entryId: payload.entry_id, artifact: payload.artifact };
      this.renderEvidence();
      this.renderControls();
    }
  }

  finishList() {
    clearTimeout(this.requestTimer);
    this.listRequest = null;
    if (this.refreshAgain) {
      this.refreshAgain = false;
      this.refresh();
    }
  }

  receiveSnapshot(payload) {
    if (typeof payload.context_id !== "string" || !Array.isArray(payload.entries)
        || this.retiredContexts.has(payload.context_id)) return;
    const isList = payload.request_id === this.listRequest && this.listRequest !== null;
    const isSave = payload.request_id === this.pendingSave?.id;
    if (!isList && !isSave && payload.request_id !== "") return;
    if (this.contextId && this.contextId !== payload.context_id) this.reset();
    this.contextId = payload.context_id;
    this.available = payload.available === true;
    this.availabilityDetail = payload.detail || "";
    this.entries = new Map(payload.entries.filter((item) => Number.isInteger(item.id)
      && ["assets", "targets"].includes(item.section) && item.entry
      && Object.hasOwn(this.labels, item.entry.kind) && typeof item.entry.value === "string"
    ).map((item) => [item.id, item]));
    if (isSave) {
      this.selectedId = this.pendingSave.entryId || this.selectedId;
      this.editing = null;
      this.finishSave();
      this.setText("inventory-status", "Resource saved to this engagement.");
    } else {
      this.setText("inventory-status", this.availabilityDetail);
    }
    if (!this.entries.has(this.selectedId)) this.selectedId = this.entries.keys().next().value ?? null;
    this.renderRows();
    this.renderDetails();
    this.renderControls();
    if (isSave) (this.rows.get(this.selectedId) || this.el("inventory-add")).focus();
    if (isList) this.finishList();
    this.onSnapshot(payload);
    this.profile.snapshot();
  }

  reset() {
    this.profile.close(false);
    if (this.contextId) this.retiredContexts.add(this.contextId);
    if (this.retiredContexts.size > 32) this.retiredContexts.delete(this.retiredContexts.values().next().value);
    clearTimeout(this.refreshTimer);
    clearTimeout(this.requestTimer);
    clearTimeout(this.saveTimer);
    this.refreshTimer = null;
    this.listRequest = null;
    this.refreshAgain = false;
    this.pendingSave = null;
    this.contextId = null;
    this.onSnapshot(null);
    this.available = false;
    this.availabilityDetail = "Loading inventory…";
    this.entries.clear();
    this.selectedId = null;
    this.editing = null;
    this.evidence = null;
    this.evidenceRequest = null;
    const hadFocus = this.el("inventory-view").contains(document.activeElement);
    this.renderRows();
    this.renderDetails();
    this.renderControls();
    this.setText("inventory-status", "Loading inventory…");
    this.onPendingChange();
    if (hadFocus) this.el("inventory-tab").focus();
  }

  icon(kind) {
    const paths = {
      repository: '<path d="M3 7V5h6l2 2h10v13H3Z"/><path d="m9 11-3 3 3 3m6-6 3 3-3 3"/>',
      url: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c-5 5-5 13 0 18M12 3c5 5 5 13 0 18"/>',
      domain: '<path d="M12 8v5M5 17v-4h14v4"/><rect x="8" y="2" width="8" height="6" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/>',
      ip: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h.01M7 17h.01M11 6h6M11 17h6"/>',
    };
    return `<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind] || ""}</svg>`;
  }

  renderRows() {
    let assets = 0;
    let targets = 0;
    let removedFocus = false;
    for (const [id, row] of this.rows) {
      if (!this.entries.has(id)) {
        removedFocus ||= row.contains(document.activeElement);
        row.remove();
        this.rows.delete(id);
      }
    }
    for (const entry of this.entries.values()) {
      if (entry.section === "assets") assets += 1; else targets += 1;
      let row = this.rows.get(entry.id);
      if (!row) {
        row = document.createElement("button");
        row.type = "button";
        row.className = "inventory-row";
        row.dataset.entryId = entry.id;
        // Static structure and known icon paths only; names/addresses never use HTML.
        row.innerHTML = '<span class="inventory-icon" aria-hidden="true"></span><span class="inventory-row-main"><span class="inventory-row-name"></span><span class="inventory-row-value"></span></span><span class="inventory-kind"></span><span class="inventory-chevron" aria-hidden="true">›</span>';
        row.addEventListener("click", () => this.select(entry.id));
        this.rows.set(entry.id, row);
      }
      const container = this.el(`inventory-${entry.section}`);
      if (row.parentElement !== container) container.append(row);
      const values = { ".inventory-row-name": entry.entry.name || entry.entry.value,
        ".inventory-row-value": entry.entry.value, ".inventory-kind": this.labels[entry.entry.kind] };
      for (const [selector, value] of Object.entries(values)) {
        if (row.querySelector(selector).textContent !== value) row.querySelector(selector).textContent = value;
      }
      if (row.dataset.kind !== entry.entry.kind) {
        row.querySelector(".inventory-icon").innerHTML = this.icon(entry.entry.kind);
        row.dataset.kind = entry.entry.kind;
      }
      row.setAttribute("aria-pressed", String(entry.id === this.selectedId));
    }
    this.setText("inventory-count", assets + targets);
    this.setText("inventory-assets-count", assets);
    this.setText("inventory-targets-count", targets);
    this.el("inventory-assets-empty").hidden = assets > 0 || !this.available;
    this.el("inventory-targets-empty").hidden = targets > 0 || !this.available;
    if (removedFocus) (this.rows.get(this.selectedId) || this.el("inventory-tab")).focus();
  }

  select(id) {
    if (this.saving) return;
    if (this.profile.open && this.profile.entryId !== id) this.profile.close(false);
    this.selectedId = id;
    this.editing = null;
    this.evidence = null;
    this.evidenceRequest = null;
    this.renderRows();
    this.renderDetails();
    this.renderControls();
    if (window.matchMedia("(max-width: 760px)").matches) {
      this.el("inventory-detail").focus({ preventScroll: true });
      this.el("inventory-detail").scrollIntoView({ block: "nearest" });
    }
  }

  renderDetails() {
    const entry = this.entries.get(this.selectedId);
    this.el("inventory-form").hidden = !this.editing;
    this.el("inventory-detail").hidden = !entry || !!this.editing;
    this.el("inventory-selection-empty").hidden = !!entry || !!this.editing;
    if (!entry || this.editing) return;
    const resource = entry.entry;
    this.el("inventory-detail-icon").innerHTML = this.icon(resource.kind);
    this.setText("inventory-detail-role", entry.section === "assets" ? "Asset" : "Target");
    this.setText("inventory-detail-name", resource.name || resource.value);
    this.setText("inventory-detail-kind", this.labels[resource.kind]);
    this.setText("inventory-address-label", resource.kind === "repository" ? "Local path" : "Address");
    this.setText("inventory-detail-value", resource.value);
    for (const field of ["environment", "tags", "notes"]) {
      const value = field === "tags" ? (resource.tags || []).join(", ") : resource[field];
      this.el(`inventory-${field}-row`).hidden = !value;
      this.setText(`inventory-detail-${field}`, value);
    }
    const created = new Date(entry.created_at);
    this.setText("inventory-detail-created", Number.isNaN(created.getTime()) ? "—" : created.toLocaleString());
    const other = Array.from(this.entries.values()).find((item) => item.section !== entry.section
      && item.entry.kind === resource.kind && item.entry.value === resource.value);
    this.el("inventory-other-role").hidden = !other;
    this.setText("inventory-other-role-label", other ? `${other.section === "assets" ? "Asset" : "Target"} · ${other.entry.name || other.entry.value}` : "");
    this.el("inventory-evidence").hidden = !Number.isInteger(entry.artifact_ref);
    if (this.evidence && (this.evidence.entryId !== entry.id || this.evidence.artifact.artifact_id !== entry.artifact_ref)) this.evidence = null;
    this.setText("inventory-evidence-open", `View artifact #${entry.artifact_ref}`);
    this.renderEvidence();
  }

  renderEvidence() {
    const cached = this.evidence?.entryId === this.selectedId ? this.evidence.artifact : null;
    this.el("inventory-evidence-content").hidden = !cached;
    this.el("inventory-evidence-open").setAttribute("aria-expanded", String(!!cached));
    this.el("inventory-evidence-error").hidden = true;
    this.setText("inventory-evidence-title", cached?.title || cached?.source || "");
    this.setText("inventory-evidence-body", cached?.body || "");
    this.el("inventory-evidence-truncated").hidden = !cached?.truncated;
  }

  openEvidence() {
    const entry = this.entries.get(this.selectedId);
    if (this.evidence?.entryId === this.selectedId) {
      this.evidence = null;
      this.renderEvidence();
      this.renderControls();
      return;
    }
    if (!entry || !this.connected || !this.available || this.evidenceRequest) return;
    const id = this.requestId();
    this.evidenceRequest = { id, entryId: entry.id };
    if (!this.send({ type: "inventory.evidence", request_id: id, context_id: this.contextId, entry_id: entry.id })) {
      this.evidenceRequest = null;
    }
    this.renderControls();
  }

  openForm(entry = null) {
    if (!this.canWrite()) return;
    this.profile.close(false);
    this.editing = { entryId: entry?.id ?? null, updatedAt: entry?.updated_at ?? null };
    this.el("inventory-role").value = entry?.section || "targets";
    this.el("inventory-kind").value = entry?.entry.kind || "url";
    this.el("inventory-value").value = entry?.entry.value || "";
    this.el("inventory-name").value = entry?.entry.name || "";
    for (const field of ["environment", "tags", "notes"]) {
      this.el(`inventory-${field}`).value = field === "tags" ? (entry?.entry.tags || []).join(", ") : entry?.entry[field] || "";
    }
    this.el("inventory-more-details").open = !!(entry?.entry.environment || entry?.entry.tags?.length || entry?.entry.notes);
    this.el("inventory-form-error").hidden = true;
    this.setText("inventory-form-heading", entry ? "Edit resource" : "Add resource");
    this.renderValueHint();
    this.renderDetails();
    this.renderControls();
    this.el(entry ? "inventory-name" : "inventory-value").focus();
  }

  renderValueHint() {
    const kind = this.el("inventory-kind").value;
    const hints = {
      url: ["URL", "https://api.example.test", "An HTTP or HTTPS address, without embedded credentials."],
      domain: ["Domain", "example.test", "A DNS name, without a scheme or path."],
      ip: ["IP address", "192.0.2.10", "One IPv4 or IPv6 address, not a network range."],
      repository: ["Local repository path", "/work/project", "An absolute folder path on the Kerkov host, or ~/path. Not a Git URL."],
    };
    const [label, placeholder, hint] = hints[kind];
    this.setText("inventory-value-label", label);
    this.el("inventory-value").placeholder = placeholder;
    this.setText("inventory-value-hint", hint);
  }

  formError(message) {
    this.setText("inventory-form-error", message);
    this.el("inventory-form-error").hidden = false;
  }

  saveResource() {
    if (!this.editing || !this.canWrite() || !this.el("inventory-form").reportValidity()) return;
    const resource = { kind: this.el("inventory-kind").value, value: this.el("inventory-value").value.trim() };
    const name = this.el("inventory-name").value.trim();
    if (name) resource.name = name;
    for (const field of ["environment", "notes"]) {
      const value = this.el(`inventory-${field}`).value.trim();
      if (value) resource[field] = value;
    }
    const tags = [...new Set(this.el("inventory-tags").value.split(",").map((tag) => tag.trim()).filter(Boolean))];
    if (tags.length > 20 || tags.some((tag) => tag.length > 64)) {
      this.formError("Use at most 20 tags, with at most 64 characters each.");
      return;
    }
    if (tags.length) resource.tags = tags;
    if (!resource.value) { this.formError("Enter a resource value."); return; }
    const id = this.requestId();
    const request = { type: "inventory.save", request_id: id, context_id: this.contextId,
      entry_id: this.editing.entryId, section: this.el("inventory-role").value, resource };
    if (this.editing.entryId !== null) request.expected_updated_at = this.editing.updatedAt;
    this.el("inventory-form-error").hidden = true;
    this.pendingSave = { id, entryId: this.editing.entryId };
    this.renderControls();
    this.onPendingChange();
    if (!this.send(request)) {
      this.finishSave();
      this.formError("Unable to send the save request. Your draft is still here.");
      return;
    }
    this.saveTimer = window.setTimeout(() => {
      if (this.pendingSave?.id !== id) return;
      this.finishSave();
      this.formError("Save confirmation did not arrive. Check the inventory before trying again.");
      this.refresh();
    }, 15000);
  }

  finishSave() {
    clearTimeout(this.saveTimer);
    this.pendingSave = null;
    this.renderControls();
    this.onPendingChange();
  }

  renderControls() {
    this.profile?.setState();
    const writable = this.canWrite();
    this.el("inventory-add").disabled = !writable;
    this.el("inventory-edit").disabled = !writable;
    this.el("inventory-use").disabled = !writable;
    this.el("inventory-save").disabled = !writable;
    this.el("inventory-cancel").disabled = this.saving;
    for (const id of ["inventory-role", "inventory-kind", "inventory-value", "inventory-name", "inventory-environment", "inventory-tags", "inventory-notes"]) {
      this.el(id).disabled = !writable;
    }
    for (const row of this.rows.values()) row.disabled = this.saving;
    this.setText("inventory-save", this.saving ? "Saving…" : this.editing?.entryId ? "Save changes" : "Add resource");
    this.el("inventory-evidence-open").disabled = !!this.evidenceRequest || (!this.evidence && (!this.connected || !this.available));
    this.setText("inventory-summary-status", !this.connected ? "Offline · last received inventory"
      : !this.available ? this.availabilityDetail : this.busy ? "Updates appear as Kerkov works." : "Shared with this engagement’s agents.");
  }
}
