class BrowserPanel {
  constructor({ send, requestId }) {
    this.send = send;
    this.requestId = requestId;
    this.connected = false;
    this.contextId = null;
    this.retiredContexts = new Set();
    this.pending = null;
    this.timer = null;
    this.refreshAgain = false;
    this.selection = null;
    this.controlled = false;
    this.frameId = null;
    this.inputQueue = [];
    this.view = document.getElementById("browser-view");
    this.status = document.getElementById("browser-status");
    this.sessions = document.getElementById("browser-sessions");
    this.refreshButton = document.getElementById("browser-refresh");
    this.image = document.getElementById("browser-image");
    this.title = document.getElementById("browser-title");
    this.url = document.getElementById("browser-url");
    this.snapshot = document.getElementById("browser-snapshot");
    this.el = (id) => document.getElementById(`browser-${id}`);
    this.el("session-mode").addEventListener("change", () => this.renderProfile());
    this.el("start-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.action("start", { session_mode: this.el("session-mode").value, profile: this.el("profile").value });
    });
    this.el("navigation").addEventListener("submit", (event) => {
      event.preventDefault();
      this.action("navigate", { url: this.el("location").value.trim() });
    });
    this.el("control").addEventListener("click", () => this.action(this.controlled ? "release" : "control"));
    for (const action of ["back", "forward", "reload", "close"]) {
      this.el(action).addEventListener("click", () => this.action(action));
    }
    this.el("input-form").addEventListener("submit", (event) => {
      event.preventDefault();
      if (this.action("type", { text: this.el("text").value })) this.el("text").value = "";
    });
    this.el("send-key").addEventListener("click", () => this.action("key", { keys: this.el("key").value }));
    this.image.addEventListener("click", (event) => {
      const point = this.point(event);
      if (point && this.action("click", point)) this.image.focus();
    });
    this.image.addEventListener("wheel", (event) => {
      if (!this.controlled) return;
      event.preventDefault();
      const point = this.point(event);
      if (point) this.action("scroll", { ...point, delta_y: Math.max(-2000, Math.min(2000, event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 720 : 1))) });
    }, { passive: false });
    this.image.addEventListener("keydown", (event) => {
      if (!this.controlled || event.isComposing) return;
      if (event.key === "Tab") return;
      event.preventDefault();
      const keys = [event.ctrlKey && "Control", event.metaKey && "Meta", event.altKey && "Alt", event.shiftKey && "Shift"].filter(Boolean);
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        this.action("type", { text: event.key });
      } else if (/^(Enter|Escape|Backspace|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|[A-Za-z0-9])$/.test(event.key)) {
        this.action("key", { keys: [...keys, event.key].join("+") });
      }
    });
    this.image.addEventListener("paste", (event) => {
      if (!this.controlled) return;
      event.preventDefault();
      this.action("type", { text: event.clipboardData.getData("text").slice(0, 8000) });
    });
    this.renderProfile();
    this.refreshButton.addEventListener("click", () => this.refresh());
    this.sessions.addEventListener("change", () => {
      this.selection = this.sessions.value || null;
      this.controlled = false;
      this.inputQueue = [];
      this.clearPreview();
      this.refresh();
    });
  }

  setState({ connected }) {
    this.connected = connected;
    if (!connected) {
      this.reset();
      this.status.textContent = "Disconnected. Browser sessions have ended.";
    }
    this.renderControls();
  }

  renderProfile() {
    const persistent = this.el("session-mode").value === "persistent";
    this.el("profile").hidden = !persistent;
    this.el("profile-label").hidden = !persistent;
    this.el("profile").required = persistent;
    this.el("profile-hint").textContent = persistent
      ? "Keeps cookies and site data on this Kerkov server across reconnects. Reuse the profile name to reopen; only one session can use it at a time."
      : "Fresh sessions discard browser data when closed.";
  }

  point(event) {
    const rect = this.image.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    return x >= 0 && x < 1 && y >= 0 && y < 1 ? { x, y } : null;
  }

  action(action, params = {}) {
    if (!this.connected || !this.contextId || this.view.hidden) return false;
    if (this.pending) {
      if (!["type", "key"].includes(action) || !this.controlled) return false;
      const last = this.inputQueue.at(-1);
      if (action === "type" && last?.action === "type" && last.params.text.length + params.text.length <= 8000) {
        last.params.text += params.text;
      } else if (this.inputQueue.length < 32) {
        this.inputQueue.push({ action, params });
      } else return false;
      return true;
    }
    if (["click", "scroll", "type", "key"].includes(action) && !this.frameId) return false;
    if (action !== "start" && !this.selection) return false;
    if (!["start", "control"].includes(action) && !this.controlled) return false;
    return this.request("browser.action", { params: { action, ...params, frame_id: this.frameId || "" } });
  }

  renderControls() {
    this.refreshButton.disabled = !this.connected || this.pending !== null;
    this.sessions.disabled = !this.connected || this.pending !== null || !this.sessions.options.length;
    const ready = this.connected && this.pending === null && this.contextId !== null;
    this.el("start").disabled = !ready;
    this.el("session-mode").disabled = !ready;
    this.el("profile").disabled = !ready;
    this.el("control").disabled = !ready || !this.selection;
    this.el("control").textContent = this.controlled ? "Return to Kerkov" : "Take control";
    for (const id of ["back", "forward", "reload", "location", "go", "close", "text", "type", "key", "send-key"]) {
      this.el(id).disabled = !ready || !this.selection || !this.controlled;
    }
    this.image.dataset.controlled = String(ready && this.controlled);
    this.image.tabIndex = this.controlled ? 0 : -1;
    this.view.setAttribute("aria-busy", String(this.pending !== null));
  }

  clearPreview() {
    this.frameId = null;
    this.image.hidden = true;
    this.image.removeAttribute("src");
    this.title.textContent = "No browser page selected";
    this.url.textContent = "Open a browser or ask Kerkov to open a page.";
    this.snapshot.textContent = "The latest snapshot requested by Kerkov appears here.";
  }

  reset(contextId = null) {
    if (this.contextId) this.retiredContexts.add(this.contextId);
    this.contextId = contextId;
    this.pending = null;
    this.selection = null;
    this.controlled = false;
    this.inputQueue = [];
    this.el("text").value = "";
    this.el("location").value = "";
    this.refreshAgain = false;
    window.clearTimeout(this.timer);
    this.sessions.replaceChildren();
    this.clearPreview();
    this.status.textContent = "No active browser sessions. Open a browser to get started.";
    this.renderControls();
  }

  show() { this.refresh(); }

  refresh() {
    if (!this.connected || this.view.hidden) return;
    if (this.pending !== null) {
      this.refreshAgain = true;
      return;
    }
    this.request("browser.preview");
  }

  request(type, extra = {}) {
    const requestId = this.requestId();
    if (!this.send({ type, request_id: requestId, context_id: this.contextId,
      session_id: this.selection, ...extra })) return false;
    this.pending = requestId;
    this.status.textContent = type === "browser.action" ? "Applying browser action…" : "Loading page preview…";
    this.renderControls();
    this.timer = window.setTimeout(() => {
      this.pending = null;
      this.inputQueue = [];
      this.refreshAgain = false;
      this.clearPreview();
      this.status.textContent = "Preview timed out. Select Refresh to retry.";
      this.renderControls();
    }, type === "browser.action" ? 45000 : 8000);
    return true;
  }

  handleEvent(type, payload) {
    if (!this.connected || this.retiredContexts.has(payload.context_id)) return;
    if (type === "browser.reset") {
      this.reset(payload.context_id);
      return;
    }
    if (type === "browser.changed") {
      if (this.contextId && payload.context_id !== this.contextId) return;
      this.refresh();
      return;
    }
    if (payload.request_id !== this.pending || this.pending === null) return;
    const restoreFocus = document.activeElement === this.image;
    window.clearTimeout(this.timer);
    this.pending = null;
    this.contextId = payload.context_id;
    if (type === "browser.error") {
      this.inputQueue = [];
      if (payload.session_closed) { this.selection = null; this.controlled = false; }
      this.clearPreview();
      this.status.textContent = payload.detail;
    } else if (type === "browser.preview") {
      const preview = payload.preview;
      this.selection = preview?.session_id || null;
      this.controlled = Boolean(preview?.controlled);
      if (!this.controlled) this.inputQueue = [];
      this.sessions.replaceChildren(...(payload.sessions || []).map((session) => {
        const option = document.createElement("option");
        option.value = session.session_id;
        option.textContent = `${session.session_id} · ${session.profile ? `Profile: ${session.profile}` : "Fresh"} · ${session.url}`;
        option.selected = session.session_id === this.selection;
        return option;
      }));
      this.clearPreview();
      if (preview) {
        this.frameId = preview.frame_id;
        this.el("location").value = preview.url;
        this.title.textContent = preview.title || "Untitled page";
        this.url.textContent = preview.url;
        this.snapshot.textContent = preview.snapshot || "Ask Kerkov for a browser snapshot to inspect the page elements.";
        if (typeof preview.image === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(preview.image)) {
          this.image.src = preview.image;
          this.image.hidden = false;
        }
      }
      this.status.textContent = preview
        ? `${this.controlled ? "You control this page." : "Kerkov can control this page."} Preview updated ${new Date().toLocaleTimeString()}.`
        : "No active browser sessions. Open a browser to get started.";
    }
    this.renderControls();
    if (restoreFocus && this.controlled && !this.image.hidden) this.image.focus();
    if (this.inputQueue.length) {
      const next = this.inputQueue.shift();
      this.action(next.action, next.params);
    } else if (this.refreshAgain) {
      this.refreshAgain = false;
      this.refresh();
    }
  }
}
