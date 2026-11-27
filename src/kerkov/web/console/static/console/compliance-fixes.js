/* Import and review pinned community fixes without running them. */
class ComplianceFixLibrary {
  constructor(host) {
    this.host = host;
    this.inventory = host.inventory;
    this.catalog = JSON.parse(host.el("compliance-fix-catalog").textContent);
    this.dialog = this.el("library");
    this.available = new Set();
    this.pending = null;
    this.plan = null;
    this.reviewed = null;
    host.el("compliance-open-fixes").addEventListener("click", () => this.open());
    host.el("compliance-close-fixes").addEventListener("click", () => this.dialog.close());
    this.el("back").addEventListener("click", () => {
      this.reviewed = null;
      this.status("");
      this.render();
      this.el("search").focus();
    });
    for (const field of ["search", "family", "filter"]) {
      this.el(field).addEventListener("input", () => this.render());
    }
  }

  el(id) { return this.host.el("compliance-fix-" + id); }
  status(detail) { this.el("status").textContent = detail; }

  matches(item, record) {
    if (!record) return false;
    return record.request.scap_preset_id === item.profile_id ||
      (!!item.profile_url && item.profile_url === record.plan?.profile_bundle?.source?.url);
  }

  reset() {
    clearTimeout(this.pending?.timer);
    if (this.pending?.destination) this.pending.showStatus("Connection or engagement changed. Retry to load the playbook.", true);
    this.pending = null;
    if (this.plan) this.failPlan(this.plan, "The connection or engagement changed. Reopen the saved assessment to continue.");
    this.reviewed = null;
    this.available.clear();
    this.dialog.close();
  }

  open() {
    this.reviewed = null;
    this.el("family").value = "all";
    this.el("search").value = "";
    this.el("filter").value = "all";
    this.status("");
    this.dialog.showModal();
    this.render();
    if (!this.pending) this.request("compliance.fixes");
    this.el("search").focus();
  }

  request(type, fixId = null, destination = null) {
    const showStatus = (detail, failed = false) => {
      if (destination) {
        destination.status.textContent = detail;
        destination.button.hidden = !failed;
      } else this.status(detail);
    };
    if (!this.inventory.connected || this.pending || (type === "compliance.fix.import" && !this.inventory.canWrite())) {
      showStatus(this.inventory.connected ? "Wait for current work to finish, then try again." : "Reconnect to review the playbook.", true);
      return;
    }
    const id = this.inventory.requestId();
    const context = this.inventory.contextId;
    if (!this.inventory.send({ type, request_id: id, context_id: context, fix_id: fixId })) {
      showStatus("Could not reach the console. Reconnect and try again.", true);
      return;
    }
    const pending = { id, context, type, fixId, showStatus, destination, timer: null };
    pending.timer = setTimeout(() => {
      if (this.pending !== pending) return;
      this.pending = null;
      showStatus("The request timed out. Try again.", true);
      this.render();
    }, type === "compliance.fix.import" ? 190000 : 20000);
    this.pending = pending;
    showStatus(type === "compliance.fix.import" ? "Preparing the pinned source for review…"
      : type === "compliance.fix.review" ? "Verifying and opening the playbook…" : "Checking available imports…");
    this.render();
  }

  handle(type, payload) {
    if (type === "compliance.fix.plan") {
      this.receivePlan(payload);
      return;
    }
    const pending = this.pending;
    if (!pending || pending.type !== type || pending.id !== payload.request_id ||
        pending.context !== payload.context_id || pending.context !== this.inventory.contextId) return;
    clearTimeout(pending.timer);
    this.pending = null;
    if (Array.isArray(payload.available)) this.available = new Set(payload.available);
    if (payload.detail && type === "compliance.fix.review") {
      this.available.delete(pending.fixId);
      if (this.el("filter").value === "imported") this.el("filter").value = "all";
    }
    if (payload.fix?.id === pending.fixId) {
      if (pending.destination) {
        pending.destination.button.remove();
        this.showSource(pending.destination.preview, payload.fix);
      } else this.reviewed = payload.fix;
    }
    const detail = payload.detail ||
      (pending.destination && payload.fix?.id !== pending.fixId ? "No verified playbook was returned. Try again." : "");
    pending.showStatus(detail, !!detail);
    this.render();
    if (this.dialog.open && this.reviewed) this.el("back").focus();
  }

  reviewAssessment(record) {
    if (!this.inventory.connected) return;
    const card = this.host.newWorkflowCard("review");
    this.host.setCardTarget(card, record.request.scan, this.host.profileLabel(record));
    card.controls.append(this.host.cardButton(card, "View failed checks", () => {
      this.host.openSavedRun(this.host.records.get(record.id) || record);
      this.host.el("compliance-control-filter").value = "failed";
      this.host.renderControlRows();
      this.inventory.showView("compliance");
    }));
    this.readPlan(card, record, Date.now() + 15000);
  }

  readPlan(card, record, deadline) {
    if (this.plan) {
      this.failPlan(this.plan, "Another assessment was opened.");
    }
    card.status.textContent = "Reading the saved scan…";
    const id = this.inventory.requestId();
    const plan = { id, card, record, deadline, timer: null };
    this.plan = plan;
    if (!this.inventory.send({ type: "compliance.fix.plan", context_id: card.context, request_id: id, run_id: record.id })) {
      this.failPlan(plan, "Could not load the saved scan. Reconnect and try again.");
      return;
    }
    plan.timer = setTimeout(() => this.failPlan(plan, "Loading the saved scan timed out."), 20000);
  }

  failPlan(plan, detail) {
    clearTimeout(plan.timer);
    this.plan = null;
    const { card, record } = plan;
    card.status.textContent = detail;
    const retry = this.host.cardButton(card, "Retry review", () => {
      if (!this.inventory.connected) return;
      retry.remove();
      this.readPlan(card, record, Date.now() + 15000);
    });
    card.controls.prepend(retry);
  }

  receivePlan(payload) {
    const plan = this.plan;
    if (!plan || payload.request_id !== plan.id || payload.run_id !== plan.record.id ||
        payload.context_id !== plan.card.context || payload.context_id !== this.inventory.contextId) return;
    clearTimeout(plan.timer);
    if (payload.retry_after_ms && Date.now() < plan.deadline) {
      this.plan = { ...plan, id: null, timer: setTimeout(() => {
        this.plan = null;
        this.readPlan(plan.card, plan.record, plan.deadline);
      }, payload.retry_after_ms) };
      return;
    }
    if (payload.detail) {
      this.failPlan(plan, payload.detail);
      return;
    }
    this.plan = null;
    this.renderPlan(plan.card, plan.record, payload);
  }

  renderPlan(card, record, { failed, errors, checks }) {
    const node = (...args) => this.host.node(...args);
    card.status.textContent = failed + " failed " + (failed === 1 ? "check" : "checks") +
      (errors ? " · " + errors + " assessment " + (errors === 1 ? "error" : "errors") + " to investigate" : "") + ".";
    const related = this.catalog.find(fix => this.matches(fix, record));
    card.summary.append(node("p", "", related
      ? "Related community playbook · " + related.name + " (" + related.scope.toLowerCase() + "). Review its scope before using it."
      : "This profile has no paired playbook. Use the failed checks below to plan changes for this host."));
    if (checks.length) {
      const list = node("ul", "", "");
      for (const check of checks) {
        const item = node("li", "", "");
        item.append(node("strong", "", check.title),
          node("p", "compliance-caption", check.control_id + " · " + this.host.impactBand(check.impact) + " impact"));
        if (check.guidance) {
          const more = node("details", "", "");
          more.append(node("summary", "", check.publisher_fix ? "Publisher remediation guidance" : "What the check requires"),
            node("p", "compliance-fix-guidance", check.guidance));
          item.append(more);
        }
        list.append(item);
      }
      card.summary.append(node("p", "", failed > checks.length
        ? "Showing " + checks.length + " of " + failed + " failed checks by impact." : "Failed checks by impact"), list);
    }
    if (related) this.loadCommunitySource(card, related);
    card.moveToLatest();
  }

  loadCommunitySource(card, related) {
    const status = this.host.node("p", "compliance-caption", "");
    status.setAttribute("role", "status");
    const preview = this.host.node("div", "", "");
    const load = () => {
      const type = this.available.has(related.id) ? "compliance.fix.review" : "compliance.fix.import";
      this.request(type, related.id, { status, preview, button });
    };
    const button = this.host.cardButton(card, "Retry playbook", load);
    card.summary.append(status, preview);
    card.controls.prepend(button);
    load();
  }

  showSource(container, fix) {
    const node = (...args) => this.host.node(...args);
    container.append(node("strong", "", fix.name),
      node("p", "compliance-caption", [fix.publisher, fix.version, fix.platform, fix.scope].join(" · ")),
      node("p", "", fix.note));
    const details = node("details", "", "");
    const metadata = node("dl", "", "");
    for (const [label, value] of [["Source revision", fix.revision], ["Playbook path", fix.playbook],
      ["Playbook SHA-256", fix.playbook_sha256], ["Bundle SHA-256", fix.bundle_sha256]]) {
      if (value) metadata.append(node("dt", "", label), node("dd", "compliance-fix-guidance", value));
    }
    details.append(node("summary", "", "Source details"), metadata);
    container.append(details);
    for (const [label, preview] of [["Playbook source", fix.preview], ["Role entry tasks", fix.role_preview]]) {
      if (!preview) continue;
      const details = node("details", "", "");
      details.append(node("summary", "", label), node("pre", "compliance-fix-source", preview));
      container.append(details);
    }
  }

  renderReview(fix) {
    const node = (...args) => this.host.node(...args);
    this.el("review-title").textContent = fix.name;
    const entries = [
      ["Publisher", fix.publisher], ["Platform", fix.platform], ["Coverage", fix.scope],
      ["Release", fix.version], ["Source revision", fix.revision],
      ["Playbook path", fix.playbook], ["Playbook SHA-256", fix.playbook_sha256],
    ];
    if (fix.bundle_sha256) entries.push(["Bundle SHA-256", fix.bundle_sha256]);
    if (fix.role_tasks) entries.push(["Role entry tasks", fix.role_tasks]);
    this.el("facts").replaceChildren(...entries.flatMap(([label, value]) =>
      [node("dt", "", label), node("dd", "", value)]));
    this.el("text").textContent = fix.note + (fix.preview_truncated ? " Showing the first 32 KB of the saved playbook." : "");
    this.el("playbook").textContent = fix.preview;
    this.el("role").hidden = !fix.role_preview;
    this.el("role-source").textContent = fix.role_preview || "";
  }

  render() {
    this.el("browser").hidden = !!this.reviewed;
    this.el("review").hidden = !this.reviewed;
    if (this.reviewed) {
      this.renderReview(this.reviewed);
      return;
    }
    const terms = this.el("search").value.toLowerCase().match(/\S+/g) || [];
    const family = this.el("family").value;
    const filter = this.el("filter").value;
    const node = (...args) => this.host.node(...args);
    const groups = new Map();
    for (const item of this.catalog) {
      const imported = this.available.has(item.id);
      if ((family !== "all" && item.family !== family) ||
          (filter === "imported" && !imported) ||
          !terms.every(term => [item.name, item.publisher, item.platform, item.family, item.scope].join(" ").toLowerCase().includes(term))) continue;
      if (!groups.has(item.family)) {
        const section = node("section", "compliance-fix-group", "");
        section.append(node("h4", "", item.family));
        groups.set(item.family, section);
      }
      const card = node("article", "compliance-fix-item", "");
      card.setAttribute("aria-label", item.name);
      const detail = node("div", "", "");
      detail.append(node("strong", "", item.name),
        node("p", "compliance-caption", [item.publisher, item.platform, item.version, item.scope].join(" · ")),
        node("p", "compliance-caption", item.note));
      const source = node("a", "inventory-link", "Upstream source");
      source.href = item.revision.length === 40 ? item.url + "/tree/" + item.revision : item.url;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      detail.append(source);
      const button = node("button", "inventory-button", imported ? "Review playbook" : "Import");
      button.type = "button";
      button.disabled = !!this.pending || !this.inventory.connected || (!imported && !this.inventory.canWrite());
      button.addEventListener("click", () => this.request(imported ? "compliance.fix.review" : "compliance.fix.import", item.id));
      card.append(detail, button);
      groups.get(item.family).append(card);
    }
    this.el("list").replaceChildren(...(groups.size ? groups.values() :
      [node("p", "compliance-caption", "No fixes match these filters.")]));
  }
}
