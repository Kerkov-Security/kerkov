/* Saved conversations share the console's authenticated WebSocket. */
class EngagementPanel {
  constructor({ send, requestId, onPendingChange, canLeave, onResumed, onHistory, onStarted = () => {} }) {
    Object.assign(this, { send, requestId, onPendingChange, canLeave, onResumed, onHistory, onStarted });
    this.root = document.querySelector('#engagement-panel');
    this.list = document.querySelector('#engagement-list');
    this.search = document.querySelector('#engagement-search');
    this.status = document.querySelector('#engagement-status');
    this.detail = document.querySelector('#engagement-selection');
    this.resumeButton = document.querySelector('#engagement-resume');
    this.newButton = document.querySelector('#engagement-new');
    this.moreButton = document.querySelector('#engagement-more');
    this.refreshButton = document.querySelector('#engagement-refresh');
    this.rows = [];
    this.selected = null;
    this.activeId = null;
    this.contextId = null;
    this.nextOffset = null;
    this.connected = false;
    this.blocked = false;
    this.switching = false;
    this.pending = new Map();
    this.listRequest = null;
    this.search.addEventListener('input', () => {
      clearTimeout(this.searchTimer);
      // Invalidate the previous result immediately, before the debounce expires.
      this.listRequest = null;
      this.selected = null;
      this.rows = [];
      this.nextOffset = null;
      this.render();
      this.searchTimer = setTimeout(() => this.refresh(), 200);
    });
    this.refreshButton.addEventListener('click', () => this.refresh());
    this.moreButton.addEventListener('click', () => this.refresh(this.nextOffset));
    this.resumeButton.addEventListener('click', () => this.navigate('resume'));
    this.newButton.addEventListener('click', () => this.navigate('new'));
  }

  request(operation, payload = {}) {
    const id = this.requestId();
    const timer = setTimeout(() => {
      this.pending.delete(id);
      if (operation === 'list' && id !== this.listRequest) return;
      if (operation === 'history' && id !== this.historyRequest) return;
      this.status.textContent = operation === 'resume' || operation === 'new'
        ? 'Opening is taking longer than expected. Wait for completion or reload to reconnect.'
        : 'No response. Refresh to try again.';
      // Do not unlock a mutation whose outcome is unknown. A late completion
      // remains correlated by mutationId and can still complete the switch.
      if (operation === 'history') this.onHistory(null);
    }, 20000);
    this.pending.set(id, { operation, timer });
    if (!this.send({ type: `engagement.${operation}`, request_id: id, context_id: this.contextId, ...payload })) {
      clearTimeout(timer);
      this.pending.delete(id);
      this.status.textContent = 'Disconnected. Reload to reconnect.';
      return null;
    }
    return id;
  }

  refresh(offset = 0) {
    if (!this.connected || this.switching || offset === null) return;
    this.status.textContent = 'Loading saved engagements…';
    this.listRequest = this.request('list', { query: this.search.value, offset });
  }

  navigate(operation) {
    if (!this.connected || this.blocked || this.switching || !this.contextId) return;
    if (operation === 'resume' && (!this.selected || this.selected.id === this.activeId || this.selected.in_use)) return;
    const reason = this.canLeave();
    if (reason) {
      this.status.textContent = reason;
      return;
    }
    this.switching = true;
    this.onPendingChange();
    this.updateControls();
    this.status.textContent = operation === 'new' ? 'Starting a new engagement…' : 'Opening saved engagement…';
    this.mutationId = this.request(operation, operation === 'resume' ? { engagement_id: this.selected.id } : {});
    if (!this.mutationId) this.finishSwitch();
  }

  loadEarlier(before) {
    if (!this.connected || this.switching || this.blocked) return false;
    this.historyRequest = this.request('history', { before });
    return Boolean(this.historyRequest);
  }

  finishSwitch() {
    this.switching = false;
    this.mutationId = null;
    this.onPendingChange();
    this.updateControls();
  }

  setState({ connected, blocked }) {
    if (this.connected && !connected) {
      for (const { timer } of this.pending.values()) clearTimeout(timer);
      this.pending.clear();
      this.listRequest = null;
      this.mutationId = null;
      this.historyRequest = null;
      this.switching = false;
      this.status.textContent = 'Disconnected. Saved sessions remain on this computer.';
      this.onHistory(null);
    }
    this.connected = connected;
    this.blocked = blocked;
    this.updateControls();
  }

  updateControls() {
    const disabled = !this.connected || this.blocked || this.switching || !this.contextId;
    this.newButton.disabled = disabled;
    this.resumeButton.disabled = disabled || !this.selected || this.selected.id === this.activeId || this.selected.in_use;
    this.refreshButton.disabled = !this.connected || this.switching;
    this.search.disabled = !this.connected || this.switching;
    this.moreButton.disabled = !this.connected || this.switching;
    this.root.setAttribute('aria-busy', String(this.switching));
  }

  contextChanged() {
    this.contextId = null;
    this.activeId = null;
    this.selected = null;
    this.listRequest = null;
    this.historyRequest = null;
    this.render();
    if (!this.switching) this.refresh();
  }

  handleEvent(type, payload) {
    const pending = this.pending.get(payload.request_id);
    if (pending) {
      clearTimeout(pending.timer);
      this.pending.delete(payload.request_id);
    }
    if (type === 'engagement.list') {
      if (payload.request_id !== this.listRequest || this.switching || payload.query !== this.search.value) return;
      this.contextId = payload.context_id;
      this.activeId = payload.active_id;
      this.nextOffset = payload.next_offset;
      this.rows = payload.offset ? [...this.rows, ...payload.engagements] : payload.engagements;
      this.selected = this.rows.find((row) => row.id === this.selected?.id) || null;
      this.status.textContent = this.rows.length ? `${this.rows.length} saved engagement${this.rows.length === 1 ? '' : 's'}`
        : this.search.value.trim() ? 'No matching engagements.' : 'No saved engagements yet. Start a conversation to create one.';
      this.render();
    } else if (type === 'engagement.resumed' || type === 'engagement.started') {
      if (payload.request_id !== this.mutationId) return;
      this.contextId = payload.context_id;
      this.activeId = payload.engagement_id;
      this.historyRequest = null;
      if (type === 'engagement.resumed') this.onResumed(payload);
      this.finishSwitch();
      if (type === 'engagement.started') this.onStarted(payload);
      this.refresh();
    } else if (type === 'engagement.history') {
      if (payload.request_id !== this.historyRequest || payload.context_id !== this.contextId) return;
      this.historyRequest = null;
      this.onHistory(payload);
    } else if (type === 'engagement.error') {
      if (!pending && payload.request_id !== this.mutationId) return;
      if (payload.operation === 'list' && payload.request_id !== this.listRequest) return;
      if (payload.operation === 'history' && payload.request_id !== this.historyRequest) return;
      this.status.textContent = payload.detail;
      if (payload.request_id === this.mutationId) this.finishSwitch();
      if (payload.request_id === this.historyRequest) {
        this.historyRequest = null;
        this.onHistory(null);
      }
    }
  }

  renderSelection() {
    this.detail.hidden = !this.selected;
    if (this.selected) {
      document.querySelector('#engagement-selected-title').textContent = this.selected.summary || this.selected.id;
      document.querySelector('#engagement-selected-meta').textContent =
        `${this.selected.mode} · ${this.selected.approval_mode} approvals · saved model: ${this.selected.model}`;
      this.resumeButton.textContent = this.selected.id === this.activeId ? 'Current engagement'
        : this.selected.in_use ? 'Open in another tab' : 'Resume engagement';
    }
    this.updateControls();
  }

  render() {
    const focusedId = document.activeElement?.dataset.engagementId;
    this.list.replaceChildren();
    for (const row of this.rows) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'engagement-item';
      button.dataset.engagementId = row.id;
      button.setAttribute('aria-pressed', String(row.id === this.selected?.id));
      if (row.id === this.activeId) button.setAttribute('aria-current', 'true');
      const title = document.createElement('span');
      title.className = 'engagement-title';
      title.textContent = row.summary || row.id;
      const meta = document.createElement('span');
      meta.className = 'engagement-meta';
      const date = new Date(row.updated_at);
      const dateText = Number.isNaN(date.getTime()) ? 'Unknown date' : date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      meta.textContent = `${row.id === this.activeId ? 'Current · ' : ''}${row.mode} · ${dateText}`;
      button.title = `${row.id} · ${row.message_count} messages`;
      button.append(title, meta);
      button.addEventListener('click', () => { this.selected = row; this.render(); });
      item.append(button);
      this.list.append(item);
      if (row.id === focusedId) button.focus({ preventScroll: true });
    }
    this.moreButton.hidden = this.nextOffset === null;
    this.renderSelection();
  }
}
