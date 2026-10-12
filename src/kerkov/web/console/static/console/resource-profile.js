/* Profiles use the same engagement context and resource identities as inventory. */
class ResourceProfile {
  constructor(inventory) {
    this.inventory = inventory;
    this.entryId = null;
    this.data = null;
    this.pending = null;
    this.evidencePending = null;
    this.requestedArtifactId = null;
    this.previewArtifactId = null;
    this.timer = null;
    this.evidenceTimer = null;
    this.tab = "overview";
    this.tabs = ["overview", "services", "findings", "activity"];
    this.el("profile-back").addEventListener("click", () => this.close());
    this.el("profile-refresh").addEventListener("click", () => this.refresh());
    this.el("profile-investigate").addEventListener("click", () => this.investigate());
    for (const name of this.tabs) {
      const tab = this.el(`profile-${name}-tab`);
      tab.addEventListener("click", () => this.showTab(name));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const index = this.tabs.indexOf(this.tab);
        this.showTab(event.key === "Home" ? this.tabs[0] : event.key === "End" ? this.tabs.at(-1)
          : this.tabs[(index + (event.key === "ArrowRight" ? 1 : 3)) % 4], true);
      });
    }
  }

  el(id) { return document.getElementById(id); }
  text(id, value) { this.el(id).textContent = value ?? ""; }
  element(tag, className, text = "") {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    return element;
  }
  date(value) {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
  }
  get open() { return this.entryId !== null; }

  openResource(id) {
    if (!this.inventory.entries.has(id) || this.inventory.editing || this.inventory.saving) return;
    this.close(false);
    this.inventory.select(id);
    this.entryId = id;
    this.data = null;
    this.inventory.showView("inventory", false);
    this.el("inventory-layout").hidden = true;
    this.el("inventory-header").hidden = true;
    this.el("resource-profile").hidden = false;
    // Reuse the existing editable overview and evidence controls in the full profile.
    this.el("profile-overview-details").append(this.el("inventory-detail"));
    this.showTab("overview");
    this.render();
    this.refresh();
    this.el("profile-heading").focus({ preventScroll: true });
  }

  close(focus = true) {
    const previous = this.entryId;
    this.entryId = null;
    this.data = null;
    this.pending = null;
    this.evidencePending = null;
    this.requestedArtifactId = null;
    this.previewArtifactId = null;
    clearTimeout(this.timer);
    clearTimeout(this.evidenceTimer);
    this.el("inventory-inspector").prepend(this.el("inventory-detail"));
    this.el("resource-profile").hidden = true;
    this.el("inventory-layout").hidden = false;
    this.el("inventory-header").hidden = false;
    this.el("profile-evidence").hidden = true;
    this.text("profile-evidence-status", "");
    this.text("profile-evidence-body", "");
    if (focus && previous !== null) (this.inventory.rows.get(previous) || this.el("inventory-tab")).focus();
  }

  snapshot() {
    if (!this.open) return;
    if (!this.inventory.entries.has(this.entryId)) { this.close(); return; }
    if (this.data && (this.data.resource.kind !== this.inventory.entries.get(this.entryId).entry.kind
        || this.data.resource.value !== this.inventory.entries.get(this.entryId).entry.value)) {
      this.data = null;
      this.previewArtifactId = null;
      this.requestedArtifactId = null;
      this.evidencePending = null;
      clearTimeout(this.evidenceTimer);
      this.el("profile-evidence").hidden = true;
      this.text("profile-evidence-status", "");
    }
    this.render();
    this.refresh();
  }

  setState() {
    this.el("profile-investigate").disabled = !this.inventory.canWrite() || !this.data;
    this.el("profile-refresh").disabled = !this.inventory.connected || !!this.pending;
    for (const button of this.el("resource-profile").querySelectorAll("[data-profile-artifact]")) {
      button.disabled = !this.inventory.connected || !!this.evidencePending;
    }
    for (const button of this.el("resource-profile").querySelectorAll("[data-profile-edit]")) {
      button.disabled = !this.inventory.canWrite();
    }
    if (!this.inventory.connected && this.open) {
      this.pending = null;
      this.evidencePending = null;
      clearTimeout(this.timer);
      clearTimeout(this.evidenceTimer);
      this.text("profile-status", "Disconnected. Showing the last received profile.");
      this.el("profile-refresh").disabled = true;
    }
  }

  refresh() {
    if (!this.open || !this.inventory.connected || !this.inventory.available) return;
    const id = this.inventory.requestId();
    this.pending = id;
    clearTimeout(this.timer);
    this.text("profile-status", "Loading profile…");
    if (!this.inventory.send({ type: "inventory.profile", request_id: id,
      context_id: this.inventory.contextId, entry_id: this.entryId })) {
      this.pending = null;
      this.text("profile-status", "Unable to load profile. Try Refresh.");
    } else {
      this.timer = window.setTimeout(() => {
        if (this.pending !== id) return;
        this.pending = null;
        this.text("profile-status", "Profile did not respond. Try Refresh.");
        this.setState();
      }, 15000);
    }
    this.setState();
  }

  handleEvent(type, payload) {
    if (!this.open || payload.context_id !== this.inventory.contextId) return;
    if (type === "inventory.profile" && payload.request_id === this.pending && payload.entry_id === this.entryId) {
      this.pending = null;
      clearTimeout(this.timer);
      this.data = payload.profile;
      if (this.previewArtifactId && !this.artifactIsLinked(this.previewArtifactId)) {
        this.previewArtifactId = null;
        this.el("profile-evidence").hidden = true;
        this.text("profile-evidence-status", "This evidence is no longer linked to the profile.");
      }
      if (this.evidencePending && !this.artifactIsLinked(this.requestedArtifactId)) {
        this.evidencePending = null;
        this.requestedArtifactId = null;
        clearTimeout(this.evidenceTimer);
        this.text("profile-evidence-status", "This evidence is no longer linked to the profile.");
      }
      this.render();
      this.text("profile-status", this.data.truncated
        ? "Showing up to 200 records per section. Use the ledger to read all records."
        : "Saved resource details and exact-address records from this engagement.");
    } else if (type === "inventory.profile_evidence" && payload.request_id === this.evidencePending
        && payload.entry_id === this.entryId) {
      this.evidencePending = null;
      this.requestedArtifactId = null;
      this.previewArtifactId = payload.artifact.artifact_id;
      clearTimeout(this.evidenceTimer);
      this.el("profile-evidence").hidden = false;
      this.text("profile-evidence-title", payload.artifact.title || payload.artifact.source || `Artifact #${payload.artifact.artifact_id}`);
      this.text("profile-evidence-body", payload.artifact.body);
      this.text("profile-evidence-status", payload.artifact.truncated ? "Showing the first 32,000 characters. Use recall for the full artifact." : "");
      this.el("profile-evidence").focus({ preventScroll: true });
      this.el("profile-evidence").scrollIntoView({ block: "nearest" });
    } else if (type === "inventory.error") {
      if (payload.request_id === this.pending) {
        this.pending = null;
        clearTimeout(this.timer);
        this.data = null;
        this.render();
        this.text("profile-status", payload.detail || "Profile is unavailable. Try Refresh.");
      } else if (payload.request_id === this.evidencePending) {
        this.evidencePending = null;
        clearTimeout(this.evidenceTimer);
        this.text("profile-evidence-status", payload.detail || "Evidence is unavailable.");
      }
    }
    this.setState();
  }

  artifactIsLinked(ref) {
    if (!this.data || !Number.isInteger(ref)) return false;
    return ["records", "hosts", "services", "findings", "activity"].some(section =>
      this.data[section].some(row => row.artifact_ref === ref))
      || this.data.connections.some(edge => edge.evidence.some(item => item.artifact_ref === ref));
  }

  showTab(name, focus = false) {
    this.tab = name;
    for (const tab of this.tabs) {
      const active = tab === name;
      this.el(`profile-${tab}-tab`).setAttribute("aria-selected", String(active));
      this.el(`profile-${tab}-tab`).tabIndex = active ? 0 : -1;
      this.el(`profile-${tab}-view`).hidden = !active;
    }
    if (focus) this.el(`profile-${name}-tab`).focus();
  }

  evidenceLink(parent, ref) {
    if (!Number.isInteger(ref)) return;
    const button = this.element("button", "inventory-link", `View evidence #${ref}`);
    button.type = "button";
    button.dataset.profileArtifact = ref;
    button.addEventListener("click", () => {
      if (!this.inventory.connected || this.evidencePending) return;
      const id = this.inventory.requestId();
      this.evidencePending = id;
      this.requestedArtifactId = ref;
      this.previewArtifactId = null;
      this.el("profile-evidence").hidden = true;
      this.text("profile-evidence-status", "Loading evidence…");
      if (!this.inventory.send({ type: "inventory.profile_evidence", request_id: id,
        context_id: this.inventory.contextId, entry_id: this.entryId, artifact_id: ref })) {
        this.evidencePending = null;
        this.text("profile-evidence-status", "Unable to load evidence. Try again.");
      } else {
        clearTimeout(this.evidenceTimer);
        this.evidenceTimer = window.setTimeout(() => {
          if (this.evidencePending !== id) return;
          this.evidencePending = null;
          this.text("profile-evidence-status", "Evidence did not respond. Try again.");
          this.setState();
        }, 15000);
      }
      this.setState();
    });
    parent.append(button);
  }

  factCard(row) {
    const card = this.element("article", "profile-card");
    const fields = row.entry;
    const title = fields.title || (row.section === "services"
      ? `${fields.port || "?"}/${fields.protocol || "transport unknown"} · ${fields.service || "Unknown service"}`
      : fields.hostname || fields.ip || "Host observation");
    card.append(this.element("h4", "profile-card-title", title));
    card.append(this.element("p", "profile-meta", `${row.artifact_ref ? "Evidence linked" : "Recorded · no evidence linked"} · Updated ${this.date(row.updated_at)}`));
    if (row.section === "findings") {
      const badges = this.element("div", "profile-badges");
      const severity = (fields.severity || "unknown").toLowerCase();
      const badge = this.element("span", "profile-badge", severity);
      if (["critical", "high", "medium", "low", "info"].includes(severity)) badge.dataset.severity = severity;
      badges.append(badge, this.element("span", "profile-badge", fields.status || "Status unknown"));
      card.append(badges);
    }
    const details = this.element("dl", "profile-fact-fields");
    for (const [key, value] of Object.entries(fields)) {
      if (["title", "severity", "status", "port", "protocol", "service"].includes(key) || !value) continue;
      const pair = this.element("div", "");
      pair.append(this.element("dt", "", key.replaceAll("_", " ")), this.element("dd", "resource-notes", value));
      details.append(pair);
    }
    card.append(details);
    if (row.truncated_fields?.length) card.append(this.element("p", "profile-meta", `Clipped fields: ${row.truncated_fields.join(", ")}. Read the full ledger record for more.`));
    this.evidenceLink(card, row.artifact_ref);
    return card;
  }

  render() {
    const entry = this.inventory.entries.get(this.entryId);
    if (!entry) return;
    this.text("profile-heading", entry.entry.name || entry.entry.value);
    this.text("profile-address", entry.entry.value);
    const data = this.data;
    this.text("profile-roles", data?.resource.roles.map(role => role === "asset" ? "Asset" : "Target").join(" · ") || (entry.section === "assets" ? "Asset" : "Target"));
    const context = data?.records.map(record => {
      const parts = [record.entry.environment, ...(record.entry.tags || [])].filter(Boolean);
      return parts.length ? `${record.section === "assets" ? "Asset" : "Target"}: ${parts.join(" · ")}` : "";
    }).filter(Boolean).join(" / ") || "";
    this.text("profile-context", context);
    this.text("profile-recorded", `Last recorded: ${this.date(data?.last_recorded_at)}`);
    this.text("profile-observed", `Last observed connection: ${this.date(data?.last_observed_at)}`);
    this.text("profile-summary", data ? `${data.counts.services} services · ${data.counts.findings} findings · ${data.counts.connections} connections` : "Profile has not loaded.");
    const roles = this.el("profile-role-records");
    roles.replaceChildren();
    for (const record of data?.records || []) {
      const card = this.element("section", "profile-card");
      card.append(this.element("h4", "profile-card-title", `${record.section === "assets" ? "Asset" : "Target"} context`));
      for (const key of ["name", "environment", "tags", "notes"]) {
        const value = record.entry[key];
        if (value) card.append(this.element("p", "resource-notes", `${key}: ${Array.isArray(value) ? value.join(", ") : value}`));
      }
      const edit = this.element("button", "inventory-link", "Edit this role’s details");
      edit.type = "button";
      edit.dataset.profileEdit = record.id;
      edit.disabled = !this.inventory.canWrite();
      edit.addEventListener("click", () => this.inventory.openForm(this.inventory.entries.get(record.id)));
      card.append(edit);
      roles.append(card);
    }
    for (const [id, rows, empty] of [
      ["profile-hosts", data?.hosts || [], "No host details recorded for this exact address."],
      ["profile-services", data?.services || [], "No services recorded for this exact address."],
      ["profile-findings", data?.findings || [], "No findings linked to this exact address. This does not establish that it is secure."],
    ]) {
      const container = this.el(id);
      container.replaceChildren(...rows.map(row => this.factCard(row)));
      if (!rows.length) container.append(this.element("p", "inventory-empty", data ? empty : "Load the profile to view records."));
    }
    const connections = this.el("profile-connections");
    connections.replaceChildren();
    for (const edge of data?.connections || []) {
      const otherId = edge.source === data.resource.id ? edge.target : edge.source;
      const other = data.related_resources.find(node => node.id === otherId);
      const card = this.element("article", "profile-card");
      const direction = edge.source === data.resource.id ? "→" : "←";
      const label = `${direction} ${edge.relation.replaceAll("_", " ")} · ${other?.value || "Related resource"}`;
      if (other?.entry_ids.length && ["domain", "ip", "url", "repository"].includes(other.kind)) {
        const button = this.element("button", "inventory-link profile-related", label);
        button.type = "button";
        button.addEventListener("click", () => this.openResource(other.entry_ids[0]));
        card.append(button);
      } else card.append(this.element("h4", "profile-card-title", label));
      if (edge.port) card.append(this.element("p", "profile-meta", `${edge.protocol}/${edge.port} · ${edge.service || "Service unknown"}`));
      card.append(this.element("p", "profile-meta", edge.provenances.map(p => p === "declared" ? "Declared · unverified" : p).join(", ")));
      for (const evidence of edge.evidence) {
        if (evidence.note) card.append(this.element("p", "resource-notes", evidence.note));
        this.evidenceLink(card, evidence.artifact_ref);
      }
      connections.append(card);
    }
    if (!data?.connections.length) connections.append(this.element("p", "inventory-empty", "No connections recorded."));
    const activity = this.el("profile-activity");
    activity.replaceChildren();
    for (const row of data?.activity || []) {
      const card = this.element("article", "profile-card");
      card.append(this.element("h4", "profile-card-title", `${row.section} · ${row.label}`));
      card.append(this.element("p", "profile-meta", `Recorded ${this.date(row.created_at)} · Updated ${this.date(row.updated_at)}`));
      this.evidenceLink(card, row.artifact_ref);
      activity.append(card);
    }
    if (!data?.activity.length) activity.append(this.element("p", "inventory-empty", "No activity loaded."));
    this.setState();
  }

  investigate() {
    const entry = this.inventory.entries.get(this.entryId);
    if (!entry || !this.inventory.canWrite() || !this.data) return;
    // A compact ledger handle lets the agent read the current profile without
    // copying stale observations or evidence bodies into the user's draft.
    const resource = { ...entry, entry: { ...entry.entry,
      notes: `${entry.entry.notes ? `${entry.entry.notes}\n\n` : ""}Read the resource profile with ledger op='profile', entry_id=${entry.id}. Roles: ${this.data.resource.roles.join(", ")}. Review linked evidence and prioritize the next investigation.` } };
    if (this.inventory.useInConversation(resource)) this.inventory.showView("conversation", false);
    else this.text("profile-status", "The conversation draft is too long. Shorten it before adding this profile.");
  }
}
