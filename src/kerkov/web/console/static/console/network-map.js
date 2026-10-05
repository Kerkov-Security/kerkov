/* A local SVG projection of the engagement ledger. No network or DNS inference. */
class NetworkMap {
  constructor({ refresh, openResource, addResource, icon }) {
    this.refresh = refresh;
    this.openResource = openResource;
    this.addResource = addResource;
    this.icon = icon;
    this.nodes = new Map();
    this.edges = [];
    this.selected = null;
    this.contextId = null;
    this.available = false;
    this.connected = false;
    this.canWrite = false;
    this.detail = "Connecting to inventory…";
    this.layoutKey = null;
    this.positions = new Map();
    this.camera = { x: 0, y: 0, scale: 1 };
    this.needsFit = true;
    this.drag = null;
    this.kinds = { repository: "Repository", url: "URL", domain: "Domain", ip: "IP address" };
    this.relations = { deploys_to: "Deploys to", uses_host: "Uses host", resolves_to: "Resolves to" };
    this.svg = this.el("network-canvas");
    this.world = this.el("network-world");
    this.lanes = this.svgElement("g", { class: "network-lanes", "aria-hidden": "true" });
    this.world.prepend(this.lanes);
    const defs = this.svgElement("defs");
    const marker = this.svgElement("marker", { id: "network-arrow", viewBox: "0 0 10 10", refX: 9,
      refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
    marker.append(this.svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "context-stroke" }));
    defs.append(marker);
    this.svg.prepend(defs);
    for (const id of ["network-search", "network-role", "network-kind"]) {
      this.el(id).addEventListener(id === "network-search" ? "input" : "change", () => this.render(true));
    }
    this.el("network-reset-filters").addEventListener("click", () => {
      this.clearFilters();
      this.render(true);
      this.el("network-search").focus();
    });
    this.el("network-refresh").addEventListener("click", () => {
      this.setStatus("Refreshing map…");
      this.refresh();
    });
    this.el("network-add-resource").addEventListener("click", () => this.addResource());
    this.el("network-fit").addEventListener("click", () => this.fit());
    this.el("network-zoom-in").addEventListener("click", () => this.zoom(1.25));
    this.el("network-zoom-out").addEventListener("click", () => this.zoom(0.8));
    this.svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = this.svg.getBoundingClientRect();
      this.zoom(Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * 0.004),
        event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });
    this.svg.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".network-node, .network-edge")) return;
      this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
        startX: this.camera.x, startY: this.camera.y };
      this.svg.setPointerCapture(event.pointerId);
      this.svg.classList.add("is-panning");
    });
    this.svg.addEventListener("pointermove", (event) => {
      if (this.drag?.id !== event.pointerId) return;
      this.camera.x = this.drag.startX + event.clientX - this.drag.x;
      this.camera.y = this.drag.startY + event.clientY - this.drag.y;
      this.transform();
    });
    const endDrag = () => { this.drag = null; this.svg.classList.remove("is-panning"); };
    this.svg.addEventListener("pointerup", endDrag);
    this.svg.addEventListener("pointercancel", endDrag);
    this.svg.addEventListener("lostpointercapture", endDrag);
    this.svg.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        this.selected = null;
        this.highlight();
        this.renderDetails();
      }
      if (event.target !== this.svg) return;
      const shifts = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] };
      if (shifts[event.key]) {
        event.preventDefault();
        this.camera.x += shifts[event.key][0];
        this.camera.y += shifts[event.key][1];
        this.transform();
      } else if (["+", "=", "-", "0"].includes(event.key)) {
        event.preventDefault();
        if (event.key === "0") this.fit(); else this.zoom(event.key === "-" ? 0.8 : 1.25);
      }
    });
    this.resizeObserver = new ResizeObserver(() => {
      if (!this.svg.clientWidth || !this.svg.clientHeight) return;
      const vertical = this.svg.clientWidth < 600;
      if (vertical !== this.vertical) this.render(true); else if (this.needsFit) this.fit();
    });
    this.resizeObserver.observe(this.svg);
    this.render();
  }

  el(id) { return document.getElementById(id); }

  svgElement(tag, attributes = {}, text = null) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
    if (text !== null) element.textContent = text;
    return element;
  }

  element(tag, className = "", text = "") {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    return element;
  }

  clearFilters() {
    this.el("network-search").value = "";
    this.el("network-role").value = "all";
    this.el("network-kind").value = "all";
  }

  receiveSnapshot(payload) {
    const contextId = payload?.context_id ?? null;
    if (this.contextId !== contextId) {
      this.clearFilters();
      this.selected = null;
      this.layoutKey = null;
    }
    this.contextId = contextId;
    this.available = payload?.available === true;
    const graph = this.available ? payload?.graph : null;
    this.nodes = new Map((Array.isArray(graph?.nodes) ? graph.nodes : []).filter((node) =>
      typeof node.id === "string" && Object.hasOwn(this.kinds, node.kind)
      && typeof node.value === "string" && Array.isArray(node.roles) && Array.isArray(node.records)
    ).map((node) => [node.id, node]));
    this.edges = (Array.isArray(graph?.edges) ? graph.edges : []).filter((edge) =>
      typeof edge.id === "string" && this.nodes.has(edge.source) && this.nodes.has(edge.target)
      && Object.hasOwn(this.relations, edge.relation) && Array.isArray(edge.provenances)
    );
    this.detail = payload?.detail || (payload && !graph ? "Refresh to load the network map." : "");
    this.render();
    this.renderStatus();
  }

  setState({ connected, canWrite }) {
    this.connected = connected;
    this.canWrite = canWrite;
    this.el("network-refresh").disabled = !connected;
    this.el("network-add-resource").disabled = !canWrite;
    this.renderStatus();
  }

  setStatus(detail) { this.detail = detail; this.renderStatus(); }

  renderStatus() {
    this.el("network-status").textContent = !this.connected
      ? (this.contextId ? "Disconnected. Showing the last received map." : "Connecting to inventory…")
      : this.detail;
  }

  show() { if (this.needsFit) window.requestAnimationFrame(() => this.fit()); }

  name(node) { return node.records.find((record) => record.name)?.name || node.value; }

  clipped(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }

  filteredNodes() {
    const query = this.el("network-search").value.trim().toLocaleLowerCase();
    const role = this.el("network-role").value;
    const kind = this.el("network-kind").value;
    return Array.from(this.nodes.values()).filter((node) =>
      (role === "all" || node.roles.includes(role)) && (kind === "all" || node.kind === kind)
      && (!query || [node.value, ...node.records.map((record) => record.name || "")]
        .some((value) => value.toLocaleLowerCase().includes(query))));
  }

  render(forceFit = false) {
    const nodes = this.filteredNodes();
    const ids = new Set(nodes.map((node) => node.id));
    this.visibleEdges = this.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
    const key = JSON.stringify([nodes.map((node) => node.id), this.visibleEdges.map((edge) => edge.id)]);
    const changed = this.layoutKey !== key;
    this.layoutKey = key;
    if (this.selected && !(this.selected.type === "node" ? ids.has(this.selected.id)
      : this.visibleEdges.some((edge) => edge.id === this.selected.id))) this.selected = null;
    const focused = document.activeElement;
    const focusNode = focused?.dataset?.nodeId;
    const focusEdge = focused?.dataset?.edgeId;
    this.el("network-nodes").replaceChildren();
    this.el("network-edges").replaceChildren();
    this.lanes.replaceChildren();
    this.layout(nodes);
    for (const edge of this.visibleEdges) this.drawEdge(edge);
    for (const node of nodes) this.drawNode(node);
    this.el("network-node-count").textContent = this.nodes.size;
    this.el("network-edge-count").textContent = this.edges.length;
    this.el("network-visible-count").textContent = `${nodes.length} of ${this.nodes.size} resources · ${this.visibleEdges.length} connections`;
    const empty = nodes.length === 0;
    this.el("network-empty").hidden = !empty;
    this.svg.toggleAttribute("hidden", empty);
    const hasFilters = !!this.el("network-search").value || this.el("network-role").value !== "all"
      || this.el("network-kind").value !== "all";
    this.el("network-empty-title").textContent = !this.available ? "Map unavailable"
      : this.nodes.size ? "No matching resources" : "Your engagement, connected";
    this.el("network-empty-body").textContent = !this.available
      ? this.detail || "The map will appear when the engagement inventory is available."
      : this.nodes.size ? "Try a different name, address, role, or resource type."
        : "Add assets and targets to start your map. Saved deployment and DNS connections will appear here.";
    this.el("network-add-resource").hidden = !this.available || this.nodes.size > 0;
    this.el("network-reset-filters").hidden = !hasFilters;
    for (const id of ["network-fit", "network-zoom-in", "network-zoom-out"]) this.el(id).disabled = empty;
    this.highlight();
    this.renderDetails();
    if (changed || forceFit) { this.needsFit = true; this.fit(); }
    if (focusNode || focusEdge) {
      const replacement = Array.from(this.svg.querySelectorAll(".network-node, .network-edge"))
        .find((element) => focusNode ? element.dataset.nodeId === focusNode : element.dataset.edgeId === focusEdge);
      (replacement || this.el("network-tab")).focus({ preventScroll: true });
    }
  }

  layout(nodes) {
    this.positions.clear();
    const kinds = Object.keys(this.kinds).filter((kind) => nodes.some((node) => node.kind === kind));
    const columns = kinds.map((kind) => nodes.filter((node) => node.kind === kind)
      .sort((a, b) => a.value.localeCompare(b.value)));
    const adjacency = new Map(nodes.map((node) => [node.id, []]));
    for (const edge of this.visibleEdges) {
      adjacency.get(edge.source).push(edge.target);
      adjacency.get(edge.target).push(edge.source);
    }
    // Stable barycentric passes reduce crossing without a moving force layout.
    for (let pass = 0; pass < 4; pass++) {
      const ranks = new Map(columns.flatMap((column) => column.map((node, index) => [node.id, index])));
      const rank = (node) => {
        const neighbors = adjacency.get(node.id);
        return neighbors.length ? neighbors.reduce((sum, id) => sum + ranks.get(id), 0) / neighbors.length : ranks.get(node.id);
      };
      for (const column of columns) column.sort((a, b) => rank(a) - rank(b) || a.value.localeCompare(b.value));
    }
    const rows = Math.max(1, ...columns.map((column) => column.length));
    this.vertical = this.svg.clientWidth > 0 && this.svg.clientWidth < 600;
    this.bounds = this.vertical
      ? { width: rows * 240 - 50, height: Math.max(116, kinds.length * 156 - 40), minX: 0, minY: 0 }
      : { width: Math.max(190, kinds.length * 300 - 110), height: rows * 130 + 10, minX: 0, minY: 0 };
    columns.forEach((column, index) => {
      const x = this.vertical ? 0 : index * 300;
      const y = this.vertical ? index * 156 : 0;
      this.lanes.append(this.svgElement("text", { x, y, class: "network-lane-label" },
        `${this.kinds[kinds[index]].toUpperCase()}  /  ${column.length}`));
      column.forEach((node, row) => this.positions.set(node.id, this.vertical
        ? { x: row * 240 + (rows - column.length) * 120, y: y + 16 }
        : { x, y: 30 + row * 130 + (rows - column.length) * 65 }));
    });
    this.longEdges = this.visibleEdges.filter((edge) =>
      kinds.indexOf(this.nodes.get(edge.target).kind) - kinds.indexOf(this.nodes.get(edge.source).kind) > 1);
    // Route connections that skip a column around the outside of other cards.
    if (this.longEdges.length) {
      const margin = (this.vertical ? 82 : 51) + (this.longEdges.length - 1) * 24;
      if (this.vertical) { this.bounds.minX = -margin; this.bounds.width += margin; }
      else { this.bounds.minY = -margin; this.bounds.height += margin; }
    }
  }

  activate(element, selection) {
    element.addEventListener("click", () => this.select(selection));
    element.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      this.select(selection);
    });
    element.addEventListener("focus", () => this.reveal(element));
  }

  drawNode(node) {
    const position = this.positions.get(node.id);
    const group = this.svgElement("g", { class: `network-node kind-${node.kind}`,
      transform: `translate(${position.x} ${position.y})`, "data-node-id": node.id,
      role: "button", tabindex: 0, "aria-label": `${this.name(node)}, ${this.kinds[node.kind]}, ${node.roles.join(" and ")}`,
      "aria-pressed": "false" });
    group.append(this.svgElement("title", {}, `${this.name(node)}\n${node.value}\n${node.roles.join(" + ")}`));
    group.append(this.svgElement("rect", { width: 190, height: 86, rx: 12, class: "network-node-card" }));
    const icon = this.svgElement("g", { class: "network-node-icon", transform: "translate(12 12)" });
    icon.append(this.svgElement("rect", { width: 30, height: 30, rx: 8 }));
    // icon() returns only our fixed resource icon paths, never resource values.
    const iconSvg = new DOMParser().parseFromString(this.icon(node.kind).replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" '), "image/svg+xml").documentElement;
    iconSvg.setAttribute("x", "5");
    iconSvg.setAttribute("y", "5");
    icon.append(document.importNode(iconSvg, true));
    group.append(icon);
    group.append(this.svgElement("text", { x: 52, y: 23, class: "network-node-kind" }, this.kinds[node.kind]));
    group.append(this.svgElement("text", { x: 52, y: 39, class: "network-node-name" }, this.clipped(this.name(node), 17)));
    group.append(this.svgElement("text", { x: 12, y: 60, class: "network-node-value" }, this.clipped(node.value, 26)));
    node.roles.forEach((role, index) => {
      if (!["asset", "target"].includes(role)) return;
      group.append(this.svgElement("circle", { cx: 16 + index * 70, cy: 74, r: 2.5, class: `network-role-${role}` }));
      group.append(this.svgElement("text", { x: 23 + index * 70, y: 77, class: `network-node-role network-role-${role}` }, role));
    });
    this.activate(group, { type: "node", id: node.id });
    this.el("network-nodes").append(group);
  }

  drawEdge(edge) {
    const source = this.positions.get(edge.source);
    const target = this.positions.get(edge.target);
    const x1 = source.x + (this.vertical ? 95 : 190), y1 = source.y + (this.vertical ? 86 : 43);
    const x2 = target.x + (this.vertical ? 95 : 0), y2 = target.y + (this.vertical ? 0 : 43);
    const bend = this.vertical ? 25 : Math.max(45, (x2 - x1) * 0.45);
    let path = this.vertical
      ? `M${x1},${y1} C${x1},${y1 + bend} ${x2},${y2 - bend} ${x2},${y2}`
      : `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
    let labelX = (x1 + x2) / 2, labelY = (y1 + y2) / 2;
    const longIndex = this.longEdges.indexOf(edge);
    if (longIndex >= 0) {
      const offset = -35 - longIndex * 24;
      if (this.vertical) {
        labelX = offset;
        const gutter = target.y - 36;
        path = `M${source.x},${source.y + 43} C${offset},${source.y + 43} ${offset},${source.y + 43} ${offset},${source.y + 80} L${offset},${gutter - 16} Q${offset},${gutter} ${offset + 16},${gutter} L${target.x + 79},${gutter} Q${target.x + 95},${gutter} ${target.x + 95},${target.y}`;
      } else {
        labelY = offset;
        const gutter = target.x - 26 - (longIndex % 2) * 16;
        path = `M${source.x + 95},${source.y} C${source.x + 95},${offset} ${source.x + 95},${offset} ${source.x + 140},${offset} L${gutter - 16},${offset} Q${gutter},${offset} ${gutter},${offset + 16} L${gutter},${target.y + 27} Q${gutter},${target.y + 43} ${target.x},${target.y + 43}`;
      }
    }
    const provenance = edge.provenances.includes("observed") ? "observed"
      : edge.provenances.includes("declared") ? "declared" : "derived";
    const label = this.relations[edge.relation];
    const group = this.svgElement("g", { class: `network-edge provenance-${provenance}`,
      "data-edge-id": edge.id, role: "button", tabindex: 0, "aria-pressed": "false",
      "aria-label": `${this.name(this.nodes.get(edge.source))} ${label.toLowerCase()} ${this.name(this.nodes.get(edge.target))}, ${edge.provenances.join(" and ")}` });
    group.append(this.svgElement("title", {}, group.getAttribute("aria-label")));
    group.append(this.svgElement("path", { d: path, class: "network-edge-hit", fill: "none", stroke: "transparent", "stroke-width": 18 }));
    group.append(this.svgElement("path", { d: path, class: "network-edge-line", fill: "none", "marker-end": "url(#network-arrow)" }));
    const labelGroup = this.svgElement("g", { class: "network-edge-label", transform: `translate(${labelX} ${labelY})` });
    const width = label.length * 6 + 16;
    labelGroup.append(this.svgElement("rect", { x: -width / 2, y: -10, width, height: 20, rx: 5 }));
    labelGroup.append(this.svgElement("text", { y: 3, "text-anchor": "middle" }, label.toLowerCase()));
    group.append(labelGroup);
    this.activate(group, { type: "edge", id: edge.id });
    this.el("network-edges").append(group);
  }

  select(selection) {
    this.selected = selection;
    this.highlight();
    this.renderDetails();
  }

  highlight() {
    const relatedNodes = new Set();
    const relatedEdges = new Set();
    if (this.selected?.type === "node") relatedNodes.add(this.selected.id);
    for (const edge of this.visibleEdges) {
      if (this.selected?.type === "node" && [edge.source, edge.target].includes(this.selected.id)
          || this.selected?.type === "edge" && this.selected.id === edge.id) {
        relatedNodes.add(edge.source); relatedNodes.add(edge.target); relatedEdges.add(edge.id);
      }
    }
    for (const node of this.el("network-nodes").children) {
      const selected = this.selected?.type === "node" && this.selected.id === node.dataset.nodeId;
      node.classList.toggle("is-selected", selected);
      node.classList.toggle("is-dimmed", !!this.selected && !relatedNodes.has(node.dataset.nodeId));
      node.setAttribute("aria-pressed", String(selected));
    }
    for (const edge of this.el("network-edges").children) {
      edge.classList.toggle("is-active", relatedEdges.has(edge.dataset.edgeId));
      edge.classList.toggle("is-dimmed", !!this.selected && !relatedEdges.has(edge.dataset.edgeId));
      edge.setAttribute("aria-pressed", String(this.selected?.type === "edge" && this.selected.id === edge.dataset.edgeId));
    }
  }

  renderDetails() {
    const details = this.el("network-details");
    const hadFocus = details.contains(document.activeElement);
    const focusKey = document.activeElement?.dataset?.focusKey;
    details.replaceChildren();
    details.hidden = !this.selected;
    this.el("network-selection-empty").hidden = !!this.selected;
    if (!this.selected) {
      if (hadFocus) this.el("network-tab").focus({ preventScroll: true });
      return;
    }
    if (this.selected.type === "node") {
      const node = this.nodes.get(this.selected.id);
      details.append(this.element("p", "eyebrow", this.kinds[node.kind].toUpperCase()),
        this.element("h3", "network-detail-name", this.name(node)),
        this.element("p", "network-detail-value", node.value));
      const roles = this.element("div", "network-detail-roles");
      for (const role of node.roles) roles.append(this.element("span", `network-badge network-role-${role}`, role));
      details.append(roles);
      for (const record of node.records) {
        const button = this.element("button", "inventory-link network-resource-link", `Open ${record.role} in inventory ↗`);
        button.type = "button";
        button.dataset.focusKey = `resource:${record.entry_id}`;
        button.addEventListener("click", () => this.openResource(record.entry_id));
        details.append(button);
      }
      const links = this.edges.filter((edge) => edge.source === node.id || edge.target === node.id);
      const section = this.element("section", "network-detail-section");
      section.append(this.element("h4", "eyebrow", `CONNECTIONS / ${links.length}`));
      if (!links.length) section.append(this.element("p", "inventory-hint", "No recorded connections yet. Ask Kerkov to record a deployment or DNS relationship with the ledger tool."));
      for (const edge of links) {
        const neighbor = this.nodes.get(edge.source === node.id ? edge.target : edge.source);
        const item = this.element("div", "network-connection");
        const relation = this.element("button", "inventory-link network-connection-link",
          `${edge.source === node.id ? "→" : "←"} ${this.relations[edge.relation]}`);
        relation.type = "button";
        relation.dataset.focusKey = `connection:${edge.id}`;
        relation.addEventListener("click", () => {
          this.clearFilters(); this.render(true); this.select({ type: "edge", id: edge.id });
        });
        const button = this.element("button", "inventory-link network-neighbor", this.name(neighbor));
        button.type = "button";
        button.dataset.focusKey = `neighbor:${neighbor.id}`;
        button.addEventListener("click", () => {
          this.clearFilters(); this.render(true); this.select({ type: "node", id: neighbor.id });
        });
        item.append(relation, button, this.element("span", "network-provenance", edge.provenances.join(" · ")));
        section.append(item);
      }
      details.append(section);
    } else {
      const edge = this.edges.find((item) => item.id === this.selected.id);
      details.append(this.element("p", "eyebrow", "CONNECTION"),
        this.element("h3", "network-detail-name", this.relations[edge.relation]),
        this.element("p", "network-provenance", edge.provenances.join(" · ")));
      for (const [label, id] of [["FROM", edge.source], ["TO", edge.target]]) {
        const section = this.element("section", "network-detail-section");
        section.append(this.element("h4", "eyebrow", label));
        const button = this.element("button", "inventory-link network-neighbor", this.name(this.nodes.get(id)));
        button.type = "button";
        button.dataset.focusKey = `neighbor:${id}`;
        button.addEventListener("click", () => this.select({ type: "node", id }));
        section.append(button, this.element("p", "network-detail-value", this.nodes.get(id).value));
        details.append(section);
      }
      const evidence = this.element("section", "network-detail-section");
      evidence.append(this.element("h4", "eyebrow", "BASIS FOR CONNECTION"));
      if (edge.provenances.includes("derived")) evidence.append(this.element("p", "inventory-hint",
        "The URL's hostname exactly matches this saved domain or IP address. This does not imply DNS resolution."));
      for (const record of edge.evidence || []) {
        const item = this.element("div", "network-connection");
        item.append(this.element("span", "network-provenance", record.provenance));
        if (record.note) item.append(this.element("p", "inventory-hint", record.note));
        if (Number.isInteger(record.artifact_ref)) item.append(this.element("p", "inventory-hint", `Evidence: artifact #${record.artifact_ref}`));
        evidence.append(item);
      }
      details.append(evidence);
    }
    if (hadFocus) {
      const replacement = Array.from(details.querySelectorAll("[data-focus-key]"))
        .find((element) => element.dataset.focusKey === focusKey);
      details.tabIndex = -1;
      (replacement || details).focus({ preventScroll: true });
    }
  }

  fit() {
    const width = this.svg.clientWidth, height = this.svg.clientHeight;
    if (!width || !height || !this.positions.size) { this.needsFit = true; return; }
    const scale = Math.min(1.2, (width - 64) / this.bounds.width, (height - 112) / this.bounds.height);
    this.fitScale = Math.max(0.00001, scale);
    this.camera = { scale: this.fitScale, x: 0, y: 0 };
    this.camera.x = (width - this.bounds.width * this.camera.scale) / 2 - this.bounds.minX * this.camera.scale;
    this.camera.y = (height - this.bounds.height * this.camera.scale) / 2 - 8 - this.bounds.minY * this.camera.scale;
    this.needsFit = false;
    this.transform();
  }

  zoom(factor, x = this.svg.clientWidth / 2, y = this.svg.clientHeight / 2) {
    const minimum = Math.min(0.08, (this.fitScale || 1) / 2);
    const scale = Math.max(minimum, Math.min(2.5, this.camera.scale * factor));
    const ratio = scale / this.camera.scale;
    this.camera.x = x - (x - this.camera.x) * ratio;
    this.camera.y = y - (y - this.camera.y) * ratio;
    this.camera.scale = scale;
    this.transform();
  }

  reveal(element) {
    const rect = element.getBoundingClientRect(), viewport = this.svg.getBoundingClientRect();
    if (rect.left >= viewport.left && rect.right <= viewport.right
        && rect.top >= viewport.top && rect.bottom <= viewport.bottom) return;
    this.camera.x += viewport.left + viewport.width / 2 - rect.left - rect.width / 2;
    this.camera.y += viewport.top + viewport.height / 2 - rect.top - rect.height / 2;
    this.transform();
  }

  transform() {
    const { x, y, scale } = this.camera;
    this.world.setAttribute("transform", `translate(${x} ${y}) scale(${scale})`);
    this.el("network-zoom").textContent = `${scale < 0.01 ? (scale * 100).toFixed(1) : Math.round(scale * 100)}%`;
  }
}
