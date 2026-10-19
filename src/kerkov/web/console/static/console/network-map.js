/* A local SVG projection of the engagement ledger. No network or DNS inference. */
class NetworkMap {
  constructor({ refresh, openResource, addResource }) {
    this.refresh = refresh;
    this.openResource = openResource;
    this.addResource = addResource;
    this.nodes = new Map();
    this.hosts = new Map();
    this.bundles = [];
    this.edges = [];
    this.selected = null;
    this.contextId = null;
    this.available = false;
    this.connected = false;
    this.canWrite = false;
    this.detail = "Connecting to inventory…";
    this.layoutKey = null;
    this.positions = new Map();
    this.manualPositions = new Map();
    this.camera = { x: 0, y: 0, scale: 1 };
    this.needsFit = true;
    this.drag = null;
    this.kinds = { repository: "Repository", url: "URL", domain: "Domain", ip: "IP address", container: "Container", vantage: "Origin" };
    this.relations = { deploys_to: "Deploys to", uses_host: "Uses host", resolves_to: "Resolves to", reaches: "Reaches", publishes_port: "Published port" };
    this.svg = this.el("network-canvas");
    this.world = this.el("network-world");
    const defs = this.svgElement("defs");
    const marker = this.svgElement("marker", { id: "network-arrow", viewBox: "0 0 10 10", refX: 9,
      refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
    marker.append(this.svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "context-stroke" }));
    defs.append(marker);
    const hitArea = this.svgElement("clipPath", { id: "network-edge-hit-area", clipPathUnits: "userSpaceOnUse" });
    this.edgeHitArea = this.svgElement("path", { "clip-rule": "evenodd" });
    hitArea.append(this.edgeHitArea);
    defs.append(hitArea);
    this.svg.prepend(defs);
    for (const id of ["network-search", "network-role", "network-kind", "network-origin"]) {
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
      if (event.button !== 0 || event.target.closest(".network-edge")) return;
      this.suppressClick = false;
      const node = event.target.closest(".network-node");
      const position = node ? this.positions.get(node.dataset.nodeId) : this.camera;
      this.drag = { id: event.pointerId, x: event.clientX, y: event.clientY,
        startX: position.x, startY: position.y, node, moved: false };
      (node || this.svg).setPointerCapture(event.pointerId);
      this.svg.classList.add(node ? "is-dragging-node" : "is-panning");
    });
    this.svg.addEventListener("pointermove", (event) => {
      if (this.drag?.id !== event.pointerId) return;
      if (Math.hypot(event.clientX - this.drag.x, event.clientY - this.drag.y) > 4) this.drag.moved = true;
      if (this.drag.node) {
        if (!this.drag.moved) return;
        const position = { x: this.drag.startX + (event.clientX - this.drag.x) / this.camera.scale,
          y: this.drag.startY + (event.clientY - this.drag.y) / this.camera.scale };
        const id = this.drag.node.dataset.nodeId;
        this.positions.set(id, position);
        this.manualPositions.set(id, position);
        this.drag.node.setAttribute("transform", `translate(${position.x} ${position.y})`);
        this.el("network-edges").replaceChildren();
        for (const edge of this.visibleEdges) this.drawEdge(edge);
        this.updateRouteHitAreas();
        this.highlight();
        return;
      }
      this.camera.x = this.drag.startX + event.clientX - this.drag.x;
      this.camera.y = this.drag.startY + event.clientY - this.drag.y;
      this.transform();
    });
    const endDrag = () => { this.drag = null; this.svg.classList.remove("is-panning", "is-dragging-node"); };
    this.svg.addEventListener("pointerup", (event) => {
      if (this.drag?.id === event.pointerId) {
        if (this.drag.node && this.drag.moved) {
          this.suppressClick = true;
          this.select({ type: "node", id: this.drag.node.dataset.nodeId });
        } else if (!this.drag.node && !this.drag.moved) this.select(null);
      }
      endDrag();
    });
    this.svg.addEventListener("click", (event) => {
      if (!this.suppressClick) return;
      this.suppressClick = false;
      event.stopPropagation();
    }, true);
    this.svg.addEventListener("pointercancel", endDrag);
    this.svg.addEventListener("lostpointercapture", endDrag);
    this.svg.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.selected) {
        event.preventDefault();
        this.select(null);
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
      if (vertical !== this.vertical || this.columns !== this.layoutColumns(this.positions.size)) this.render(true); else if (this.needsFit) this.fit();
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
    this.el("network-origin").value = "all";
  }

  hasFilters() {
    return !!this.el("network-search").value.trim() || this.el("network-role").value !== "all"
      || this.el("network-kind").value !== "all" || this.el("network-origin").value !== "all";
  }

  receiveSnapshot(payload) {
    const contextId = payload?.context_id ?? null;
    if (this.contextId !== contextId) {
      this.clearFilters();
      this.selected = null;
      this.layoutKey = null;
      this.manualPositions.clear();
    }
    this.contextId = contextId;
    this.available = payload?.available === true;
    const graph = this.available ? payload?.graph : null;
    this.nodes = new Map((Array.isArray(graph?.nodes) ? graph.nodes : []).filter((node) =>
      typeof node.id === "string" && Object.hasOwn(this.kinds, node.kind)
      && typeof node.value === "string" && Array.isArray(node.roles) && Array.isArray(node.records)
      && (node.kind !== "container" || (node.container && typeof node.container.state === "string"
        && typeof node.container.ready === "boolean" && Array.isArray(node.container.ports)))
    ).map((node) => [node.id, node]));
    this.edges = (Array.isArray(graph?.edges) ? graph.edges : []).filter((edge) =>
      typeof edge.id === "string" && this.nodes.has(edge.source) && this.nodes.has(edge.target)
      && Object.hasOwn(this.relations, edge.relation) && Array.isArray(edge.provenances)
      && this.validEdge(edge)
    );
    this.projectHosts();
    for (const id of this.manualPositions.keys()) if (!this.hosts.has(id)) this.manualPositions.delete(id);
    this.updateOrigins();
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

  validEdge(edge) {
    const source = this.nodes.get(edge.source), target = this.nodes.get(edge.target);
    const port = (value) => Number.isInteger(value) && value >= 1 && value <= 65535;
    if (edge.relation === "publishes_port") {
      return source.kind === "ip" && target.kind === "container" && target.container.state === "running"
        && ["tcp", "udp"].includes(edge.protocol) && port(edge.port) && port(edge.container_port)
        && typeof edge.bind_host === "string";
    }
    if (edge.relation === "reaches") {
      return ["ip", "domain", "vantage"].includes(source.kind) && ["ip", "domain"].includes(target.kind)
        && ["tcp", "udp"].includes(edge.protocol) && port(edge.port);
    }
    return true;
  }

  clipped(value, limit) { return value.length > limit ? `${value.slice(0, limit - 1)}…` : value; }

  hostname(node) {
    // Resource values are canonicalized by the backend. Preserve their exact
    // authority spelling: WHATWG URL would turn numeric DNS names such as
    // "127.1" into "127.0.0.1", creating a host identity the ledger never saved.
    let host = node.value;
    if (node.kind === "url") {
      const authority = host.match(/^https?:\/\/([^/?#]+)/i)?.[1] || host;
      const address = authority.slice(authority.lastIndexOf("@") + 1);
      host = address.startsWith("[") ? address.slice(1, address.indexOf("]")) : address.split(":")[0];
    }
    return host.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  }

  projectHosts() {
    this.hosts = new Map();
    this.resourceHosts = new Map();
    for (const node of this.nodes.values()) {
      if (node.kind === "repository") continue;
      const separate = ["vantage", "container"].includes(node.kind);
      const value = separate ? node.value : this.hostname(node);
      const id = separate ? node.id : `host:${value}`;
      if (!this.hosts.has(id)) this.hosts.set(id, { id, value, kind: separate ? node.kind : "host",
        container: node.container,
        members: [],
        endpoints: node.kind === "container" ? (node.endpoint_ids || []).map((id) => this.nodes.get(id)).filter(Boolean) : [],
        roles: [], dns: [], repositories: [], hidden: false });
      const host = this.hosts.get(id);
      host.members.push(node);
      host.roles = [...new Set([...host.roles, ...node.roles])];
      this.resourceHosts.set(node.id, id);
    }
    for (const edge of this.edges) {
      if (edge.relation === "resolves_to") {
        this.hosts.get(this.resourceHosts.get(edge.source))?.dns.push({ node: this.nodes.get(edge.target), edge });
      } else if (edge.relation === "deploys_to") {
        this.hosts.get(this.resourceHosts.get(edge.target))?.repositories.push({ node: this.nodes.get(edge.source), edge });
      }
    }
    for (const host of this.hosts.values()) {
      // A DNS answer remains accessible in details. Keep its own card if it has
      // any other relationship, including explicit reachability to that IP.
      host.hidden = host.members.every((node) => node.kind === "ip"
        && this.edges.some((edge) => edge.relation === "resolves_to" && edge.target === node.id)
        && !this.edges.some((edge) => edge.relation !== "resolves_to" && [edge.source, edge.target].includes(node.id)));
    }
    // Associate the destination service, never the whole IP: several containers
    // and ordinary host services can share one published address.
    const mappings = this.edges.filter((edge) => edge.relation === "publishes_port");
    const coveredMappings = new Set(), associatedHosts = new Set();
    const routes = this.edges.filter((edge) => edge.relation === "reaches").map((edge) => {
      if (!edge.provenances.includes("observed")) return edge;
      const candidates = mappings.filter((mapping) => this.resourceHosts.get(mapping.source) === this.resourceHosts.get(edge.target)
        && mapping.protocol === edge.protocol && mapping.port === edge.port);
      const destinations = new Set(candidates.map((mapping) => JSON.stringify([mapping.target, mapping.container_port])));
      if (destinations.size !== 1) return edge;
      const mapping = candidates[0];
      candidates.forEach((candidate) => coveredMappings.add(candidate.id));
      associatedHosts.add(this.resourceHosts.get(edge.target));
      return { ...edge, target: mapping.target, published_endpoint: this.nodes.get(edge.target).value,
        container_port: mapping.container_port };
    });
    const projectedEdges = [...routes, ...mappings.filter((edge) => !coveredMappings.has(edge.id))];
    for (const id of associatedHosts) {
      const host = this.hosts.get(id);
      const endpoints = new Set(mappings.filter((edge) => this.resourceHosts.get(edge.source) === id)
        .flatMap((edge) => this.hosts.get(edge.target).endpoints.filter((node) => {
          if (node.kind === "ip") return node.id === edge.source;
          if (node.kind !== "url" || edge.protocol !== "tcp" || this.hostname(node) !== this.hostname(this.nodes.get(edge.source))) return false;
          try {
            const url = new URL(node.value);
            return Number(url.port || (url.protocol === "https:" ? 443 : 80)) === edge.port;
          } catch { return false; }
        }).map((node) => node.id)));
      // Preserve independently described hosts, unmatched URLs/services, DNS,
      // deployments, and hosts used as the source of another access route.
      if (host.members.every((node) => endpoints.has(node.id) && node.roles.every((role) => role === "target")
          && node.records.every((record) => !record.environment && !record.notes && !(record.tags || []).length
            && (!record.name || node.kind === "url")))
          && !host.dns.length && !host.repositories.length
          && !this.edges.some((edge) => ["resolves_to", "deploys_to"].includes(edge.relation)
            && [edge.source, edge.target].some((nodeId) => this.resourceHosts.get(nodeId) === id))
          && !projectedEdges.some((edge) => [edge.source, edge.target].some((nodeId) => this.resourceHosts.get(nodeId) === id))) {
        host.hidden = true;
      }
    }
    const bundles = new Map();
    for (const edge of projectedEdges) {
      const source = this.resourceHosts.get(edge.source), target = this.resourceHosts.get(edge.target);
      if (!source || !target) continue;
      const id = JSON.stringify([source, target, edge.relation]);
      if (!bundles.has(id)) bundles.set(id, { id, source, target, relation: edge.relation, records: [] });
      bundles.get(id).records.push(edge);
    }
    this.bundles = [...bundles.values()];
  }

  updateOrigins() {
    const select = this.el("network-origin"), previous = select.value;
    select.replaceChildren(this.element("option", "", "All origins"));
    select.firstChild.value = "all";
    const origins = [...new Set(this.bundles.map((edge) => edge.source))]
      .map((id) => this.hosts.get(id)).sort((a, b) => a.value.localeCompare(b.value));
    for (const host of origins) {
      const option = this.element("option", "", `${host.value}${host.kind === "vantage" ? " (origin)" : ""}`);
      option.value = host.id;
      select.append(option);
    }
    select.value = origins.some((host) => host.id === previous) ? previous : "all";
  }

  matches(node) {
    const query = this.el("network-search").value.trim().toLocaleLowerCase();
    const role = this.el("network-role").value, kind = this.el("network-kind").value;
    return (role === "all" || node.roles.includes(role)) && (kind === "all" || node.kind === kind)
      && (!query || [node.value, node.container?.service || "", node.container?.project_name || "", ...(node.container?.ports || []).map((port) => `${port.host}:${port.host_port} ${port.protocol}`), ...(node.endpoint_ids || []).map((id) => this.nodes.get(id)?.value || ""), ...node.records.map((record) => record.name || "")]
        .some((value) => value.toLocaleLowerCase().includes(query)));
  }

  filteredNodes() {
    const matches = [...this.hosts.values()].filter((host) => !host.hidden &&
      [...host.members, ...host.dns.map((item) => item.node), ...host.repositories.map((item) => item.node)]
        .some((node) => this.matches(node)));
    const ids = new Set(matches.map((host) => host.id)), origin = this.el("network-origin").value;
    // Keep direct sources as context when a destination matches a role/type/
    // search filter. Role-less runner origins must not silently erase routes.
    for (const edge of this.bundles) {
      if (ids.has(edge.target) && !ids.has(edge.source) && (origin === "all" || edge.source === origin)) {
        matches.push(this.hosts.get(edge.source));
      }
    }
    return { nodes: [...new Map(matches.map((host) => [host.id, host])).values()], matchingIds: ids };
  }

  routesFor(host) {
    return this.visibleEdges.filter((edge) => edge.target === host.id);
  }

  routeStatus(host) {
    if (host.kind === "container") {
      const count = this.routesFor(host).filter((route) => route.relation === "reaches").length;
      return `${host.container.state} · ${host.container.ready ? "ready" : "not ready"}${count ? ` · ${count} ${count === 1 ? "route" : "routes"}` : ""}`;
    }
    if (host.kind === "vantage" || this.el("network-origin").value === host.id) return "Connection origin";
    const routes = this.routesFor(host);
    if (!routes.length && this.visibleEdges.some((edge) => edge.source === host.id && edge.relation === "publishes_port")) return "Published port origin";
    if (!routes.length) return this.hasFilters() ? "No matching route" : "No recorded route";
    const observed = routes.some((route) => route.records.some((edge) => edge.provenances.includes("observed")));
    return observed ? `${routes.length} ${this.hasFilters() ? "matching" : "recorded"} incoming ${routes.length === 1 ? "route" : "routes"}` : "Declared · unverified";
  }

  render(forceFit = false) {
    const { nodes, matchingIds } = this.filteredNodes(), ids = new Set(nodes.map((node) => node.id));
    const origin = this.el("network-origin").value;
    // Contextual sources do not become matching destinations of other routes.
    this.visibleEdges = this.bundles.filter((edge) => ids.has(edge.source) && matchingIds.has(edge.target)
      && (origin === "all" || edge.source === origin));
    const key = JSON.stringify([nodes.map((node) => node.id), this.visibleEdges.map((edge) => edge.id)]);
    const changed = this.layoutKey !== key;
    this.layoutKey = key;
    if (this.selected && !(this.selected.type === "node" ? ids.has(this.selected.id)
      : this.visibleEdges.some((edge) => edge.id === this.selected.id))) this.selected = null;
    const focused = document.activeElement, focusNode = focused?.dataset?.nodeId, focusEdge = focused?.dataset?.edgeId;
    this.el("network-nodes").replaceChildren();
    this.el("network-edges").replaceChildren();
    this.layout(nodes);
    for (const edge of this.visibleEdges) this.drawEdge(edge);
    this.updateRouteHitAreas();
    for (const node of nodes) this.drawNode(node);
    const total = [...this.hosts.values()].filter((host) => !host.hidden && host.kind !== "vantage").length;
    this.el("network-node-count").textContent = total;
    this.el("network-edge-count").textContent = this.bundles.length;
    const entityLabel = [...this.hosts.values()].some((host) => host.kind === "container") ? "hosts / containers" : "hosts";
    this.el("network-visible-count").textContent = `${nodes.filter((node) => node.kind !== "vantage").length} of ${total} ${entityLabel} · ${this.visibleEdges.length} visible connections`;
    const empty = nodes.length === 0;
    this.el("network-empty").hidden = !empty;
    this.svg.toggleAttribute("hidden", empty);
    this.el("network-empty-title").textContent = !this.available ? "Map unavailable"
      : this.hasFilters() ? "No matching hosts" : "Your hosts, at a glance";
    this.el("network-empty-body").textContent = !this.available
      ? this.detail || "The map will appear when the engagement inventory is available."
      : this.hasFilters() ? "Try another filter. Repository records are listed beside the map."
        : "Add a URL, domain, or IP address, or start a container. Published ports and recorded access routes appear automatically.";
    this.el("network-reset-filters").hidden = !this.hasFilters();
    for (const id of ["network-fit", "network-zoom-in", "network-zoom-out"]) this.el(id).disabled = empty;
    this.renderRepositories();
    this.highlight();
    this.renderDetails();
    if (changed || forceFit) { this.needsFit = true; this.fit(); }
    if (focusNode || focusEdge) {
      const replacement = Array.from(this.svg.querySelectorAll(".network-node, .network-edge"))
        .find((element) => focusNode ? element.dataset.nodeId === focusNode : element.dataset.edgeId === focusEdge);
      (replacement || this.el("network-tab")).focus({ preventScroll: true });
    }
  }

  layoutColumns(count) {
    return this.svg.clientWidth > 0 && this.svg.clientWidth < 600 ? 1
      : Math.min(this.svg.clientWidth > 900 ? 3 : 2, Math.max(1, Math.ceil(Math.sqrt(count))));
  }

  layout(nodes) {
    this.positions.clear();
    this.vertical = this.svg.clientWidth > 0 && this.svg.clientWidth < 600;
    const columns = this.layoutColumns(nodes.length);
    this.columns = columns;
    const sorted = [...nodes].sort((a, b) => Number(b.kind === "vantage") - Number(a.kind === "vantage")
      || a.value.localeCompare(b.value));
    // Reserve only the tracks and label rows needed by each host row. In a
    // single column there are no cross-column trunks to reserve vertical space for.
    const indices = new Map(sorted.map((node, index) => [node.id, index]));
    const columnWidth = 296 + this.visibleEdges.length * 14;
    this.routeGutters = new Map();
    let top = 0;
    for (let row = 0; row < Math.ceil(sorted.length / columns); row += 1) {
      const rowNodes = sorted.slice(row * columns, (row + 1) * columns);
      const incoming = Math.max(1, ...rowNodes.map((node) => this.visibleEdges.filter((edge) => edge.target === node.id).length));
      const labelBand = incoming * 26;
      const crossing = this.visibleEdges.filter((edge) => Math.floor(indices.get(edge.target) / columns) === row
        && indices.get(edge.source) % columns !== indices.get(edge.target) % columns);
      const y = top + labelBand + crossing.length * 14 + 24;
      crossing.forEach((edge, index) => this.routeGutters.set(edge.id, y - labelBand - 16 - index * 14));
      rowNodes.forEach((node, column) => this.positions.set(node.id, { x: column * columnWidth, y }));
      top = y + 132;
    }
    for (const node of nodes) {
      if (this.manualPositions.has(node.id)) this.positions.set(node.id, this.manualPositions.get(node.id));
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
    const position = this.positions.get(node.id), status = this.routeStatus(node);
    const urls = node.members.filter((member) => member.kind === "url");
    const addresses = [...new Set(node.dns.map((item) => item.node.value))];
    const group = this.svgElement("g", { class: `network-node kind-${node.kind}`,
      transform: `translate(${position.x} ${position.y})`, "data-node-id": node.id,
      role: "button", tabindex: 0, "aria-label": [node.value, node.kind === "vantage" ? "origin" : node.kind === "container" ? "container" : "host", node.roles.join(" and "), status].filter(Boolean).join(", "),
      "aria-pressed": "false" });
    group.append(this.svgElement("title", {}, `${node.value}\n${status}`));
    group.append(this.svgElement("rect", { width: 236, height: 112, rx: 6, class: "network-node-card" }));
    const icon = this.svgElement("g", { class: "network-node-icon", transform: "translate(15 11)", "aria-hidden": "true" });
    const icons = {
      vantage: "M1 2l4 4-4 4M7 12h6",
      container: "M7 1l6 3.5v7L7 15l-6-3.5v-7L7 1zM1 4.5L7 8l6-3.5M7 8v7",
      host: "M2 1h10v5H2zM2 10h10v5H2zM4 3.5h.01M4 12.5h.01M7 6v4",
    };
    icon.append(this.svgElement("path", { d: icons[node.kind] || icons.host }));
    group.append(icon);
    group.append(this.svgElement("text", { x: 37, y: 22, class: "network-node-kind" }, node.kind === "vantage" ? "ORIGIN" : node.kind === "container" ? "CONTAINER" : "HOST"));
    node.roles.filter((role) => ["asset", "target"].includes(role)).forEach((role, index) => {
      group.append(this.svgElement("circle", { cx: 121 + index * 55, cy: 18, r: 2.5, class: `network-role-${role}` }));
      group.append(this.svgElement("text", { x: 128 + index * 55, y: 22, class: `network-node-role network-role-${role}` }, role));
    });
    group.append(this.svgElement("text", { x: 15, y: 46, class: "network-node-name" }, this.clipped(node.value, 28)));
    const serviceCount = new Set(this.routesFor(node).flatMap((route) => route.records.map((edge) => `${edge.protocol}:${edge.port}`))).size;
    const summary = node.kind === "container" ? `${node.container.service || "Container"} · ${node.container.ports.length} published ${node.container.ports.length === 1 ? "port" : "ports"}`
      : node.kind === "vantage" ? "Recorded connection source"
      : `${serviceCount ? `${serviceCount} ${serviceCount === 1 ? "service" : "services"} · ` : ""}${urls.length} ${urls.length === 1 ? "URL" : "URLs"}${addresses.length ? ` · ${addresses.length === 1 ? addresses[0] : `${addresses.length} DNS addresses`}` : ""}`;
    group.append(this.svgElement("text", { x: 15, y: 66, class: "network-node-value" }, this.clipped(summary, 32)));
    group.append(this.svgElement("line", { x1: 15, y1: 78, x2: 221, y2: 78, class: "network-node-divider" }));
    group.append(this.svgElement("text", { x: 15, y: 97, class: `network-node-status${(node.kind === "container" && !node.container.ready) || status.startsWith("No ") || status === "Declared · unverified" || status === "Connection origin" || status === "Published port origin" ? " is-unknown" : ""}` }, status));
    this.activate(group, { type: "node", id: node.id });
    this.el("network-nodes").append(group);
  }

  serviceLabel(edge) {
    if (edge.relation === "publishes_port") return `${edge.protocol.toUpperCase()} :${edge.port} → :${edge.container_port}`;
    return `${edge.service || edge.protocol?.toUpperCase() || "Service"} :${edge.port}${edge.published_endpoint ? ` → :${edge.container_port}` : ""}`;
  }

  bundleLabel(bundle) {
    const services = [...new Map(bundle.records.map((edge) => [`${edge.protocol}:${edge.port}:${edge.container_port ?? ""}`, this.serviceLabel(edge)])).values()];
    return services.length === 1 ? this.clipped(services[0], 23) : `${this.clipped(services[0], 17)} +${services.length - 1}`;
  }

  curvedPath(points) {
    let path = `M${points[0][0]},${points[0][1]}`;
    for (let index = 1; index < points.length - 1; index += 1) {
      const [x, y] = points[index], previous = points[index - 1], next = points[index + 1];
      const before = Math.hypot(previous[0] - x, previous[1] - y);
      const after = Math.hypot(next[0] - x, next[1] - y);
      if (!before || !after) { path += ` L${x},${y}`; continue; }
      const radius = Math.min(100, before / 2, after / 2);
      path += ` L${x + (previous[0] - x) * radius / before},${y + (previous[1] - y) * radius / before}`;
      path += ` Q${x},${y} ${x + (next[0] - x) * radius / after},${y + (next[1] - y) * radius / after}`;
    }
    return `${path} L${points.at(-1)[0]},${points.at(-1)[1]}`;
  }

  updateRouteHitAreas() {
    const occupied = [];
    const cards = [...this.positions.values()].map(({ x, y }) => ({ x: x - 6, y: y - 6, width: 248, height: 124 }));
    const overlaps = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
    for (const edge of this.el("network-edges").children) {
      const label = edge.querySelector(".network-edge-label"), box = label.getBBox();
      const matrix = label.transform.baseVal.consolidate().matrix;
      const line = edge.querySelector(".network-edge-line"), length = line.getTotalLength();
      const candidates = [{ x: matrix.e, y: matrix.f }, ...[0.25, 0.75, 0.15, 0.85, 0.4, 0.6].map((fraction) => line.getPointAtLength(length * fraction))];
      const rectangle = (point) => ({ x: point.x + box.x - 3, y: point.y + box.y - 3, width: box.width + 6, height: box.height + 6 });
      const point = candidates.find((point) => [...cards, ...occupied].every((other) => !overlaps(rectangle(point), other))) || candidates[0];
      label.setAttribute("transform", `translate(${point.x} ${point.y})`);
      occupied.push(rectangle(point));
    }
    const bounds = this.el("network-edges").getBBox();
    let path = `M${bounds.x - 20},${bounds.y - 20} h${bounds.width + 40} v${bounds.height + 40} h${-bounds.width - 40} Z`;
    // Curves can cross another route's label. Keep wide hit targets out of
    // every label so SVG paint order cannot make an evidence link unclickable.
    for (const label of this.el("network-edges").querySelectorAll(".network-edge-label")) {
      const box = label.getBBox(), matrix = label.transform.baseVal.consolidate().matrix;
      path += ` M${matrix.e + box.x - 2},${matrix.f + box.y - 2} h${box.width + 4} v${box.height + 4} h${-box.width - 4} Z`;
    }
    this.edgeHitArea.setAttribute("d", path);
  }

  drawEdge(edge) {
    const source = this.positions.get(edge.source), target = this.positions.get(edge.target);
    const index = this.visibleEdges.indexOf(edge);
    const incoming = this.visibleEdges.filter((item) => item.target === edge.target);
    const targetIndex = incoming.indexOf(edge);
    const outgoing = this.visibleEdges.filter((item) => item.source === edge.source);
    const sourceIndex = outgoing.indexOf(edge);
    const x1 = source.x + 236;
    const y1 = source.y + 24 + (64 * (sourceIndex + 1)) / (outgoing.length + 1);
    const lane1 = x1 + 24 + index * 14;
    const lane2 = target.x + 260 + index * 14;
    const gutter = this.routeGutters.get(edge.id) ?? Math.min(source.y, target.y) - 60 - index * 26;
    let labelX = target.x + 140, labelY = target.y - 20 - targetIndex * 26;
    const targetX = target.x + 16 + (24 * (targetIndex + 1)) / (incoming.length + 1);
    // Keep a routed fallback for curves blocked by another host card.
    const points = source.x === target.x
      ? [[x1, y1], [lane1, y1], [lane1, labelY], [targetX, labelY], [targetX, target.y]]
      : [[x1, y1], [lane1, y1], [lane1, gutter], [lane2, gutter], [lane2, labelY], [targetX, labelY], [targetX, target.y]];
    let path = this.curvedPath(points);
    const direction = target.x > source.x ? 1 : -1;
    const sameColumn = Math.abs(target.x - source.x) < 236;
    const start = { x: source.x + (sameColumn || direction > 0 ? 236 : 0), y: y1 };
    const end = { x: target.x + (sameColumn || direction < 0 ? 236 : 0),
      y: target.y + 24 + (64 * (targetIndex + 1)) / (incoming.length + 1) };
    const reach = Math.max(60, Math.abs(end.x - start.x) * 0.55);
    const lane = Math.max(source.x, target.x) + 316 + index * 26;
    const bend = Math.abs(start.y - end.y) < 40 ? direction * 32 : 0;
    const first = { x: sameColumn ? lane : start.x + direction * reach, y: start.y + bend };
    const second = { x: sameColumn ? lane : end.x - direction * reach, y: end.y + bend };
    const pointAt = (t) => ({
      x: (1 - t) ** 3 * start.x + 3 * (1 - t) ** 2 * t * first.x + 3 * (1 - t) * t ** 2 * second.x + t ** 3 * end.x,
      y: (1 - t) ** 3 * start.y + 3 * (1 - t) ** 2 * t * first.y + 3 * (1 - t) * t ** 2 * second.y + t ** 3 * end.y,
    });
    const obstacles = [...this.positions.entries()].filter(([id]) => ![edge.source, edge.target].includes(id)).map(([, position]) => position);
    const blocked = Array.from({ length: 49 }, (_, i) => pointAt((i + 1) / 50)).some((point) =>
      obstacles.some((card) => point.x > card.x - 8 && point.x < card.x + 244 && point.y > card.y - 8 && point.y < card.y + 120));
    if (!blocked) {
      path = `M${start.x},${start.y} C${first.x},${first.y} ${second.x},${second.y} ${end.x},${end.y}`;
      const midpoint = pointAt(sameColumn ? 0.35 : 0.5);
      labelX = midpoint.x;
      labelY = midpoint.y;
    }
    const observed = edge.records.every((item) => item.provenances.includes("observed"));
    const mixed = !observed && edge.records.some((item) => item.provenances.includes("observed"));
    const published = edge.relation === "publishes_port";
    const provenance = published ? "published" : observed ? "observed" : "declared";
    const label = this.bundleLabel(edge);
    const group = this.svgElement("g", { class: `network-edge provenance-${provenance}`,
      "data-edge-id": edge.id, "data-source": edge.source, "data-target": edge.target,
      role: "button", tabindex: 0, "aria-pressed": "false",
      "aria-label": `${this.hosts.get(edge.source).value} to ${this.hosts.get(edge.target).value}, ${label}, ${published ? "published port, reachability unverified" : mixed ? "observed and unverified services" : observed ? "observed" : "declared, unverified"}` });
    group.append(this.svgElement("title", {}, group.getAttribute("aria-label")));
    group.append(this.svgElement("path", { d: path, class: "network-edge-hit", fill: "none", stroke: "transparent", "stroke-width": 18, "clip-path": "url(#network-edge-hit-area)" }));
    group.append(this.svgElement("path", { d: path, class: "network-edge-line", fill: "none", "marker-end": "url(#network-arrow)" }));
    const labelGroup = this.svgElement("g", { class: "network-edge-label", transform: `translate(${labelX} ${labelY})` });
    const width = label.length * 5.6 + 14;
    labelGroup.append(this.svgElement("rect", { x: -width / 2, y: -10, width, height: 20, rx: 5 }));
    labelGroup.append(this.svgElement("text", { y: 3, "text-anchor": "middle" }, label));
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
    const relatedEdges = new Set();
    for (const edge of this.visibleEdges) {
      if (this.selected?.type === "node" && [edge.source, edge.target].includes(this.selected.id)
          || this.selected?.type === "edge" && this.selected.id === edge.id) {
        relatedEdges.add(edge.id);
      }
    }
    for (const node of this.el("network-nodes").children) {
      const selected = this.selected?.type === "node" && this.selected.id === node.dataset.nodeId;
      node.classList.toggle("is-selected", selected);
      node.setAttribute("aria-pressed", String(selected));
    }
    for (const edge of this.el("network-edges").children) {
      edge.classList.toggle("is-active", relatedEdges.has(edge.dataset.edgeId));
      edge.setAttribute("aria-pressed", String(this.selected?.type === "edge" && this.selected.id === edge.dataset.edgeId));
    }
  }

  resourceItem(node) {
    const item = this.element("div", "network-connection");
    item.append(this.element("strong", "", this.name(node)));
    if (this.name(node) !== node.value) item.append(this.element("p", "network-resource-value", node.value));
    if (node.kind === "url") {
      try {
        const url = new URL(node.value);
        // Browsers interpret numeric DNS names such as 127.1 as IPv4 literals.
        // Keep the saved address inert if navigation would change its host.
        const sameHost = this.hostname({ kind: "url", value: url.href }) === this.hostname(node);
        if (["http:", "https:"].includes(url.protocol) && sameHost) {
          const link = this.element("a", "inventory-link network-endpoint-link", "Open URL ↗");
          link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
          item.append(link);
        }
      } catch { /* Invalid saved addresses remain inert, inspectable text. */ }
    }
    for (const record of node.records) {
      for (const [label, value] of [["Environment", record.environment], ["Tags", record.tags?.join(", ")], ["Notes", record.notes]]) {
        if (value) item.append(this.element("p", "network-resource-value resource-notes", `${record.role} · ${label}: ${value}`));
      }
      const button = this.element("button", "inventory-link network-resource-link", `Open ${record.role} profile ↗`);
      button.type = "button"; button.dataset.focusKey = `resource:${record.entry_id}`;
      button.addEventListener("click", () => this.openResource(record.entry_id));
      item.append(button);
    }
    return item;
  }

  section(title) {
    const section = this.element("section", "network-detail-section");
    section.append(this.element("h4", "eyebrow", title));
    return section;
  }

  appendEvidence(container, edge) {
    for (const record of edge.evidence || []) {
      const item = this.element("div", "network-evidence");
      item.append(this.element("span", "network-provenance", edge.relation === "publishes_port" ? "Docker port mapping" : record.provenance === "declared" ? "Declared · unverified" : record.provenance));
      if (record.service) item.append(this.element("p", "inventory-hint", `Service: ${record.service}`));
      if (record.observed_at) item.append(this.element("p", "inventory-hint", `Observed: ${record.observed_at}`));
      if (record.note) item.append(this.element("p", "inventory-hint", record.note));
      if (Number.isInteger(record.artifact_ref)) item.append(this.element("p", "inventory-hint", `Evidence: artifact #${record.artifact_ref}`));
      if (Number.isInteger(record.connection_id)) item.append(this.element("p", "inventory-hint", `Connection #${record.connection_id}`));
      if (Number.isInteger(record.container_id)) item.append(this.element("p", "inventory-hint", `Container observation #${record.container_id}`));
      container.append(item);
    }
  }

  renderRepositories() {
    const container = this.el("network-repositories"), open = container.open;
    const focusKey = container.contains(document.activeElement) ? document.activeElement?.dataset?.focusKey : null;
    const nodes = [...this.nodes.values()].filter((node) => node.kind === "repository" && this.matches(node));
    container.replaceChildren(this.element("summary", "", `Repositories · ${nodes.length}`));
    container.hidden = !nodes.length;
    for (const node of nodes) {
      const item = this.resourceItem(node);
      const hosts = [...this.hosts.values()].filter((host) => host.repositories.some((repo) => repo.node.id === node.id));
      item.append(this.element("p", "inventory-hint", hosts.length ? `Related hosts: ${hosts.map((host) => host.value).join(", ")}` : "No associated host"));
      container.append(item);
    }
    container.open = open;
    if (focusKey) Array.from(container.querySelectorAll("[data-focus-key]")).find((item) => item.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
  }

  hostButton(id) {
    const button = this.element("button", "inventory-link network-neighbor", this.hosts.get(id).value);
    button.type = "button"; button.dataset.focusKey = `neighbor:${id}`;
    button.addEventListener("click", () => {
      if (!this.positions.has(id)) { this.clearFilters(); this.render(true); }
      this.select({ type: "node", id });
    });
    return button;
  }

  appendContainerDetails(details, container) {
    const observation = this.section("LAST DOCKER OBSERVATION");
    for (const [label, value] of [["Service", container.service], ["Project", container.project_name || "Compose default"],
      ["Compose directory", container.compose_dir], ["Health", container.health || "No healthcheck"],
      ["Observed at", container.observed_at], ["Readiness", container.readiness_basis], ["Log artifact", container.log_artifact]]) {
      observation.append(this.element("p", "network-resource-value", `${label}: ${value}`));
    }
    observation.append(this.element("p", "inventory-hint", "Last-known state. Run container status to refresh changes made outside Kerkov."));
    details.append(observation);
    const ports = this.section(`PUBLISHED PORTS / ${container.ports.length}`);
    for (const port of container.ports) {
      const bind = port.host.includes(":") ? `[${port.host}]` : port.host;
      ports.append(this.element("p", "network-resource-value", `${bind}:${port.host_port} → ${port.container_port}/${port.protocol}`));
    }
    ports.append(this.element("p", "inventory-hint", "Port mappings do not establish reachability. Stopped and removed containers retain their last-known mappings."));
    details.append(ports);
  }

  renderDetails() {
    const details = this.el("network-details"), hadFocus = details.contains(document.activeElement);
    const focusKey = document.activeElement?.dataset?.focusKey;
    details.replaceChildren();
    details.hidden = !this.selected;
    this.el("network-selection-empty").hidden = !!this.selected;
    if (!this.selected) {
      if (hadFocus) this.el("network-tab").focus({ preventScroll: true });
      return;
    }
    if (this.selected.type === "node") {
      const host = this.hosts.get(this.selected.id), origin = this.el("network-origin").value;
      details.append(this.element("p", "eyebrow", host.kind === "vantage" ? "CONNECTION ORIGIN" : host.kind === "container" ? "CONTAINER" : "HOST"),
        this.element("h3", "network-detail-name", host.value));
      const roles = this.element("div", "network-detail-roles");
      for (const role of host.roles) roles.append(this.element("span", `network-badge network-role-${role}`, role));
      details.append(roles);
      const status = this.routeStatus(host);
      details.append(this.element("p", "network-route-status", status));
      if (status === "No recorded route" || status === "No matching route") {
        const scope = origin === "all" ? "" : ` from ${this.hosts.get(origin).value}`;
        const detail = status === "No matching route"
          ? `No recorded incoming route${scope} matches the current filters.`
          : "No incoming access route has been recorded.";
        details.append(this.element("p", "inventory-hint", `${detail} This does not establish that the host is unreachable.`));
      }
      if (host.kind === "container") this.appendContainerDetails(details, host.container);
      if (host.kind !== "vantage") {
        const members = host.kind === "container" ? host.endpoints : host.members;
        const resources = this.section(`${host.kind === "container" ? "SAVED ENDPOINTS" : "SAVED RESOURCES"} / ${members.length}`);
        for (const member of members) resources.append(this.resourceItem(member));
        details.append(resources);
      }
      if (host.dns.length) {
        const dns = this.section("DNS ADDRESSES");
        dns.append(this.element("p", "inventory-hint", "Saved DNS relationships describe name resolution; they do not establish access."));
        for (const item of host.dns) {
          dns.append(this.resourceItem(item.node)); this.appendEvidence(dns, item.edge);
        }
        details.append(dns);
      }
      if (host.repositories.length) {
        const repositories = this.section("RELATED REPOSITORIES");
        for (const item of host.repositories) {
          repositories.append(this.resourceItem(item.node)); this.appendEvidence(repositories, item.edge);
        }
        details.append(repositories);
      }
      const links = this.visibleEdges.filter((edge) => edge.source === host.id || edge.target === host.id);
      const routes = this.section(`VISIBLE CONNECTIONS / ${links.length}`);
      for (const edge of links) {
        const item = this.element("div", "network-connection");
        const relation = this.element("button", "inventory-link network-connection-link",
          `${edge.source === host.id ? "→" : "←"} ${this.bundleLabel(edge)}`);
        relation.type = "button"; relation.dataset.focusKey = `connection:${edge.id}`;
        relation.addEventListener("click", () => this.select({ type: "edge", id: edge.id }));
        item.append(relation, this.hostButton(edge.source === host.id ? edge.target : edge.source));
        routes.append(item);
      }
      details.append(routes);
    } else {
      const edge = this.bundles.find((item) => item.id === this.selected.id);
      const published = edge.relation === "publishes_port";
      details.append(this.element("p", "eyebrow", published ? "PUBLISHED PORT MAPPING" : "DIRECT ACCESS ROUTE"),
        this.element("h3", "network-detail-name", this.bundleLabel(edge)),
        this.element("p", "inventory-hint", published
          ? "Docker publishes these host ports to this container. Port mappings do not establish reachability. Run container status to refresh changes made outside Kerkov."
          : "Recorded connections describe this source and destination only. They do not imply an authenticated session or a transit path."));
      for (const [label, id] of [["FROM", edge.source], ["TO", edge.target]]) {
        const section = this.section(label); section.append(this.hostButton(id)); details.append(section);
      }
      const services = this.section(`${published ? "PORT MAPPINGS" : "SERVICES"} / ${edge.records.length}`);
      for (const record of edge.records) {
        const item = this.element("div", "network-connection");
        item.append(this.element("strong", "", this.serviceLabel(record)),
          this.element("span", "network-provenance", `${record.protocol.toUpperCase()} · ${published ? "Published port · unverified" : record.provenances.includes("observed") ? "Observed" : "Declared · unverified"}`));
        if (published) item.append(this.element("p", "inventory-hint", `Host bind: ${record.bind_host}`));
        if (record.published_endpoint) {
          const address = record.published_endpoint.includes(":") ? `[${record.published_endpoint}]` : record.published_endpoint;
          item.append(this.element("p", "inventory-hint", `Published endpoint: ${address}:${record.port} → container port ${record.container_port}/${record.protocol}. Access was observed at the host port; the container association follows its last-known published mapping.`));
        }
        if (record.observed_at) item.append(this.element("p", "inventory-hint", `Observed at: ${record.observed_at}`));
        this.appendEvidence(item, record); services.append(item);
      }
      details.append(services);
    }
    if (hadFocus) {
      const replacement = Array.from(details.querySelectorAll("[data-focus-key]"))
        .find((element) => element.dataset.focusKey === focusKey);
      (replacement || details).focus({ preventScroll: true });
    }
  }

  fit() {
    const width = this.svg.clientWidth, height = this.svg.clientHeight;
    if (!width || !height || !this.positions.size) { this.needsFit = true; return; }
    // Measure the drawn graph, including gutter paths and their labels. The
    // card grid alone misses wide labels, especially on a two-host mobile map.
    // getBBox uses local coordinates, independent of the current pan and zoom.
    const content = this.world.getBBox();
    this.bounds = { minX: content.x, minY: content.y,
      width: Math.max(1, content.width), height: Math.max(1, content.height) };
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
