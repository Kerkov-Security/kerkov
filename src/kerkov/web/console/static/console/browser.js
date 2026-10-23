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
    this.view = document.getElementById("browser-view");
    this.status = document.getElementById("browser-status");
    this.sessions = document.getElementById("browser-sessions");
    this.refreshButton = document.getElementById("browser-refresh");
    this.image = document.getElementById("browser-image");
    this.title = document.getElementById("browser-title");
    this.url = document.getElementById("browser-url");
    this.snapshot = document.getElementById("browser-snapshot");
    this.refreshButton.addEventListener("click", () => this.refresh());
    this.sessions.addEventListener("change", () => {
      this.selection = this.sessions.value || null;
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

  renderControls() {
    this.refreshButton.disabled = !this.connected || this.pending !== null;
    this.sessions.disabled = !this.connected || this.pending !== null || !this.sessions.options.length;
    this.view.setAttribute("aria-busy", String(this.pending !== null));
  }

  clearPreview() {
    this.image.hidden = true;
    this.image.removeAttribute("src");
    this.title.textContent = "No browser page selected";
    this.url.textContent = "Ask Kerkov to open a page in the browser.";
    this.snapshot.textContent = "The latest snapshot requested by Kerkov appears here.";
  }

  reset(contextId = null) {
    if (this.contextId) this.retiredContexts.add(this.contextId);
    this.contextId = contextId;
    this.pending = null;
    this.selection = null;
    this.refreshAgain = false;
    window.clearTimeout(this.timer);
    this.sessions.replaceChildren();
    this.clearPreview();
    this.status.textContent = "No active browser sessions. Ask Kerkov to open a page.";
    this.renderControls();
  }

  show() { this.refresh(); }

  refresh() {
    if (!this.connected || this.view.hidden) return;
    if (this.pending !== null) {
      this.refreshAgain = true;
      return;
    }
    const requestId = this.requestId();
    if (!this.send({ type: "browser.preview", request_id: requestId, context_id: this.contextId,
      session_id: this.selection })) return;
    this.pending = requestId;
    this.status.textContent = "Loading page preview…";
    this.renderControls();
    this.timer = window.setTimeout(() => {
      this.pending = null;
      this.refreshAgain = false;
      this.clearPreview();
      this.status.textContent = "Preview timed out. Select Refresh to retry.";
      this.renderControls();
    }, 8000);
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
    window.clearTimeout(this.timer);
    this.pending = null;
    this.contextId = payload.context_id;
    if (type === "browser.error") {
      if (payload.session_closed) this.selection = null;
      this.clearPreview();
      this.status.textContent = payload.detail;
    } else if (type === "browser.preview") {
      const preview = payload.preview;
      this.selection = preview?.session_id || null;
      this.sessions.replaceChildren(...(payload.sessions || []).map((session) => {
        const option = document.createElement("option");
        option.value = session.session_id;
        option.textContent = `${session.session_id} · ${session.url}`;
        option.selected = session.session_id === this.selection;
        return option;
      }));
      this.clearPreview();
      if (preview) {
        this.title.textContent = preview.title || "Untitled page";
        this.url.textContent = preview.url;
        this.snapshot.textContent = preview.snapshot || "Ask Kerkov for a browser snapshot to inspect the page elements.";
        if (typeof preview.image === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(preview.image)) {
          this.image.src = preview.image;
          this.image.hidden = false;
        }
      }
      this.status.textContent = preview
        ? `Preview updated ${new Date().toLocaleTimeString()}. Updates after browser actions; use Refresh for page changes.`
        : "No active browser sessions. Ask Kerkov to open a page.";
    }
    this.renderControls();
    if (this.refreshAgain) {
      this.refreshAgain = false;
      this.refresh();
    }
  }
}
