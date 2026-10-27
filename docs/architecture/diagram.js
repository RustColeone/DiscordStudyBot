"use strict";
// Diagrams for the architecture page. A chart is described as nodes, groups and edges (Chart), laid out by ELK (the
// Eclipse Layout Kernel: layered, with right-angled edges and groups inside groups) and drawn here as SVG, with
// zooming and panning (wheel, drag, pinch, keys and buttons). Hovering a box lights up what it is connected to.
(() => {
  const ELK_URLS = ["https://cdn.jsdelivr.net/npm/elkjs@0.9.3/lib/elk.bundled.js",
                    "https://unpkg.com/elkjs@0.9.3/lib/elk.bundled.js"];
  const SVG = "http://www.w3.org/2000/svg";
  const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  // The letter on each kind of box.
  const BADGE = { module: "M", folder: "D", file: "F", class: "C", function: "ƒ", method: "ƒ", section: "§",
                  external: "↗", gateway: "G", manager: "M", providers: "P", store: "DB", startup: "S",
                  supervisor: "S", contracts: "K" };

  // ---- describing a chart ---------------------------------------------------------------------------------
  class Chart {
    constructor(direction = "RIGHT") {
      this.direction = direction;
      this.nodes = [];
      this.groups = [];
      this.edges = [];
      this.stack = [];
      this.count = 0;
    }
    node(title, { kind = "file", sub = "", target = "", current = false, tip = "" } = {}) {
      const id = `n${this.count++}`;
      this.nodes.push({ id, title: String(title), kind, sub: String(sub || ""), target, current, tip: String(tip || ""),
                        parent: this.stack.at(-1) || null });
      return id;
    }
    group(title, build, { kind = "plain", pack = false } = {}) {  // pack: its boxes in a grid (they have no edges inside)
      const id = `g${this.count++}`;
      this.groups.push({ id, title: String(title), kind, pack, parent: this.stack.at(-1) || null });
      this.stack.push(id);
      build();
      this.stack.pop();
      return id;
    }
    edge(from, to, { dashed = false, label = "", arrow = "end" } = {}) {  // arrow: end, both or none
      if (from && to && from !== to) this.edges.push({ id: `e${this.edges.length}`, from, to, dashed, label, arrow });
    }
    key() { return JSON.stringify([this.direction, this.nodes, this.groups, this.edges]); }
  }

  // ---- layout (ELK) ---------------------------------------------------------------------------------------
  let elkReady = null;
  function loadElk() {  // pinned; a second CDN stands in when the first cannot be reached
    if (elkReady) return elkReady;
    elkReady = ELK_URLS.reduce((tried, url) => tried.catch(() => new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.onload = () => (window.ELK ? resolve(new window.ELK()) : reject(new Error("no ELK")));
      script.onerror = () => { script.remove(); reject(new Error(`could not load ${url}`)); };
      document.head.append(script);
    })), Promise.reject(new Error("start")));
    elkReady.catch(() => { elkReady = null; });  // try again with the next chart
    return elkReady;
  }
  const measure = document.createElement("canvas").getContext("2d");
  const textWidth = (text, size, weight = 400) => { measure.font = `${weight} ${size}px ${FONT}`; return measure.measureText(text).width; };
  const boxSize = (node) => ({
    width: Math.ceil(Math.max(textWidth(node.title, 13, 650), node.sub ? textWidth(node.sub, 11) : 0) + 50),
    height: node.sub ? 46 : 32,
  });

  function elkGraph(chart) {
    const root = {
      id: "root", children: [], edges: [],
      layoutOptions: {
        "elk.algorithm": "layered", "elk.direction": chart.direction, "elk.hierarchyHandling": "INCLUDE_CHILDREN",
        "elk.edgeRouting": "ORTHOGONAL", "elk.layered.spacing.nodeNodeBetweenLayers": "54", "elk.spacing.nodeNode": "18",
        "elk.layered.spacing.edgeNodeBetweenLayers": "16", "elk.spacing.edgeEdge": "9", "elk.spacing.edgeNode": "14",
        "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF", "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        "elk.separateConnectedComponents": "true", "elk.spacing.componentComponent": "34", "elk.aspectRatio": "1.7",
        "elk.edgeLabels.placement": "CENTER", "elk.padding": "[top=24,left=24,bottom=24,right=24]",
      },
    };
    const holders = new Map([[null, root]]);
    for (const group of chart.groups) {
      const minimum = Math.ceil(textWidth(group.title, 12, 700) + 40);
      holders.set(group.id, { id: group.id, children: [], layoutOptions: {
        "elk.padding": "[top=38,left=16,bottom=16,right=16]", "elk.nodeSize.constraints": "MINIMUM_SIZE",
        "elk.nodeSize.minimum": `(${minimum}, 60)`,
        ...(group.pack ? { "elk.hierarchyHandling": "SEPARATE_CHILDREN", "elk.algorithm": "rectpacking",
                           "elk.aspectRatio": "2.2", "elk.spacing.nodeNode": "12" } : {}) } });
    }
    for (const group of chart.groups) holders.get(group.parent).children.push(holders.get(group.id));
    for (const node of chart.nodes) holders.get(node.parent).children.push({ id: node.id, ...boxSize(node) });
    root.edges = chart.edges.map((edge) => ({
      id: edge.id, sources: [edge.from], targets: [edge.to],
      labels: edge.label ? [{ text: edge.label, width: Math.ceil(textWidth(edge.label, 11) + 12), height: 18 }] : [],
    }));
    return root;
  }

  // ---- drawing ------------------------------------------------------------------------------------------
  const el = (tag, attributes = {}, text) => {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text != null) node.textContent = text;
    return node;
  };
  function rounded(points, radius = 9) {  // a polyline with its corners rounded
    let path = `M${points[0].x},${points[0].y}`;
    for (let index = 1; index < points.length - 1; index++) {
      const [a, b, c] = [points[index - 1], points[index], points[index + 1]];
      const first = Math.hypot(b.x - a.x, b.y - a.y), second = Math.hypot(c.x - b.x, c.y - b.y);
      const r = Math.min(radius, first / 2, second / 2);
      const start = { x: b.x + ((a.x - b.x) / (first || 1)) * r, y: b.y + ((a.y - b.y) / (first || 1)) * r };
      const end = { x: b.x + ((c.x - b.x) / (second || 1)) * r, y: b.y + ((c.y - b.y) / (second || 1)) * r };
      path += ` L${start.x},${start.y} Q${b.x},${b.y} ${end.x},${end.y}`;
    }
    const last = points[points.length - 1];
    return `${path} L${last.x},${last.y}`;
  }

  function render(chart, laid) {
    const where = new Map();  // every box, group and the root: its place on the page
    (function place(node, x, y) {
      const box = { x: x + (node.x || 0), y: y + (node.y || 0), width: node.width || 0, height: node.height || 0 };
      where.set(node.id, box);
      for (const child of node.children || []) place(child, box.x, box.y);
    })(laid, 0, 0);
    const dense = (laid.edges || []).length > 30;  // many edges: drawn lighter until a box is hovered
    const svg = el("svg", { class: `graph${dense ? " dense" : ""}`, role: "group", tabindex: "0",
                            "aria-label": "Diagram; drag to pan, scroll or pinch to zoom" });
    const defs = el("defs");
    for (const [id, refX] of [["arrow", 9], ["arrow-start", 1]]) {
      const marker = el("marker", { id, viewBox: "0 0 10 10", refX, refY: 5, markerWidth: 7, markerHeight: 7,
                                    orient: id === "arrow" ? "auto" : "auto-start-reverse", markerUnits: "strokeWidth" });
      marker.append(el("path", { d: "M0,0 L10,5 L0,10 z", class: "arrowhead" }));
      defs.append(marker);
    }
    const world = el("g", { class: "world" });
    const groupsLayer = el("g", { class: "groups" }), edgesLayer = el("g", { class: "edges" }), nodesLayer = el("g", { class: "nodes" });
    world.append(groupsLayer, edgesLayer, nodesLayer);
    svg.append(defs, world);

    for (const group of chart.groups) {
      const box = where.get(group.id);
      if (!box) continue;
      const g = el("g", { class: `grp g-${group.kind}` });
      g.append(el("rect", { x: box.x, y: box.y, width: box.width, height: box.height, rx: 14 }),
               el("text", { x: box.x + 14, y: box.y + 23, class: "grp-title" }, group.title));
      groupsLayer.append(g);
    }

    const links = new Map();  // box id → the edges and boxes it touches, for lighting them up
    const touch = (id, item) => { if (!links.has(id)) links.set(id, []); links.get(id).push(item); };
    const edgeOf = new Map(chart.edges.map((edge) => [edge.id, edge]));
    for (const laidEdge of laid.edges || []) {
      const edge = edgeOf.get(laidEdge.id);
      const section = laidEdge.sections?.[0];
      if (!edge || !section) continue;
      const offset = where.get(laidEdge.container) || { x: 0, y: 0 };
      const points = [section.startPoint, ...(section.bendPoints || []), section.endPoint].map((point) => ({ x: point.x + offset.x, y: point.y + offset.y }));
      const g = el("g", { class: `edge-g${edge.dashed ? " dashed" : ""}` });
      const path = el("path", { d: rounded(points), class: "edge" });
      if (edge.arrow !== "none") path.setAttribute("marker-end", "url(#arrow)");
      if (edge.arrow === "both") path.setAttribute("marker-start", "url(#arrow-start)");
      g.append(path);
      for (const label of laidEdge.labels || []) {
        const x = (label.x || 0) + offset.x, y = (label.y || 0) + offset.y;
        g.append(el("rect", { x, y, width: label.width, height: label.height, rx: 5, class: "edge-label-bg" }),
                 el("text", { x: x + label.width / 2, y: y + label.height / 2 + 4, class: "edge-label" }, label.text));
      }
      edgesLayer.append(g);
      touch(edge.from, { line: g, other: edge.to });
      touch(edge.to, { line: g, other: edge.from });
    }

    const nodes = new Map();
    for (const node of chart.nodes) {
      const box = where.get(node.id);
      if (!box) continue;
      const g = el("g", { class: `node k-${node.kind}${node.current ? " current" : ""}${node.target ? " link" : ""}`,
                          transform: `translate(${box.x},${box.y})`, "data-id": node.id });
      if (node.target) {
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "link");
        g.setAttribute("aria-label", [node.title, node.sub].filter(Boolean).join(", "));
      }
      g.append(el("rect", { class: "card", width: box.width, height: box.height, rx: 10 }));
      const badge = BADGE[node.kind] || "•";
      g.append(el("rect", { class: "badge", x: 10, y: box.height / 2 - 10, width: 22, height: 20, rx: 6 }),
               el("text", { class: "badge-text", x: 21, y: box.height / 2 + 4 }, badge));
      g.append(el("text", { class: "title", x: 40, y: node.sub ? 20 : box.height / 2 + 4.5 }, node.title));
      if (node.sub) g.append(el("text", { class: "sub", x: 40, y: 35 }, node.sub));
      if (node.tip) g.append(el("title", {}, node.tip));
      nodesLayer.append(g);
      nodes.set(node.id, g);
    }
    // Hovering (or focusing) a box: it, its edges and the boxes at their other ends stay bright.
    const light = (id, on) => {
      svg.classList.toggle("focus", on);
      nodes.get(id)?.classList.toggle("on", on);
      for (const item of links.get(id) || []) {
        item.line.classList.toggle("on", on);
        nodes.get(item.other)?.classList.toggle("on", on);
      }
    };
    for (const [id, g] of nodes) {
      g.addEventListener("pointerenter", () => light(id, true));
      g.addEventListener("pointerleave", () => light(id, false));
      g.addEventListener("focus", () => light(id, true));
      g.addEventListener("blur", () => light(id, false));
    }
    const bounds = where.get("root");
    return { svg, world, bounds: { x: 0, y: 0, width: bounds.width, height: bounds.height },
             current: chart.nodes.find((node) => node.current && where.has(node.id)) ? where.get(chart.nodes.find((node) => node.current).id) : null,
             targets: new Map(chart.nodes.filter((node) => node.target).map((node) => [node.id, node.target])) };
  }

  // ---- zooming and panning ---------------------------------------------------------------------------------
  class Viewport {
    constructor(picture, onGo) {
      Object.assign(this, picture, { scale: 1, x: 0, y: 0, moved: false, pointers: new Map(), onGo });
      const svg = this.svg;
      svg.addEventListener("wheel", (event) => {
        event.preventDefault();
        const step = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
        this.zoomAt(Math.exp(-step * 0.0016), ...this.point(event));
      }, { passive: false });
      svg.addEventListener("pointerdown", (event) => {
        this.pointers.set(event.pointerId, this.point(event));
        if (this.pointers.size === 1) { this.origin = this.point(event); this.moved = false; this.pressed = event.target.closest(".node.link"); }
        svg.setPointerCapture(event.pointerId);
      });
      svg.addEventListener("pointermove", (event) => {
        if (!this.pointers.has(event.pointerId)) return;
        const before = this.pointers.get(event.pointerId);
        const now = this.point(event);
        if (this.pointers.size === 1) {
          if (Math.hypot(now[0] - this.origin[0], now[1] - this.origin[1]) > 4) this.moved = true;
          if (this.moved) { this.x += now[0] - before[0]; this.y += now[1] - before[1]; this.apply(); svg.classList.add("panning"); }
        } else if (this.pointers.size === 2) {  // pinch: zoom by the change in distance, about the middle
          const [other] = [...this.pointers.entries()].filter(([id]) => id !== event.pointerId).map(([, point]) => point);
          const was = Math.hypot(before[0] - other[0], before[1] - other[1]);
          const is = Math.hypot(now[0] - other[0], now[1] - other[1]);
          if (was > 0) this.zoomAt(is / was, (now[0] + other[0]) / 2, (now[1] + other[1]) / 2);
          this.moved = true;
        }
        this.pointers.set(event.pointerId, now);
      });
      const release = (event) => {
        this.pointers.delete(event.pointerId);
        svg.classList.remove("panning");
        if (event.type === "pointerup" && !this.moved && this.pressed && this.pointers.size === 0) this.go(this.pressed);
        if (!this.pointers.size) this.pressed = null;
      };
      svg.addEventListener("pointerup", release);
      svg.addEventListener("pointercancel", release);
      svg.addEventListener("dblclick", (event) => { if (!event.target.closest(".node")) this.zoomAt(1.7, ...this.point(event)); });
      svg.addEventListener("keydown", (event) => {
        const node = event.target.closest?.(".node.link");
        if (node && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); this.go(node); return; }
        const pan = 60;
        const actions = { "+": () => this.zoomBy(1.25), "=": () => this.zoomBy(1.25), "-": () => this.zoomBy(0.8), "0": () => this.fit(),
                          ArrowLeft: () => this.panBy(pan, 0), ArrowRight: () => this.panBy(-pan, 0),
                          ArrowUp: () => this.panBy(0, pan), ArrowDown: () => this.panBy(0, -pan) };
        if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
      });
    }
    point(event) { const box = this.svg.getBoundingClientRect(); return [event.clientX - box.left, event.clientY - box.top]; }
    go(node) { const target = this.targets.get(node.dataset.id); if (target) this.onGo(target); }
    apply() {
      this.world.setAttribute("transform", `translate(${this.x},${this.y}) scale(${this.scale})`);
      this.onZoom?.(this.scale);
    }
    zoomAt(factor, cx, cy) {
      const scale = Math.min(3, Math.max(0.08, this.scale * factor));
      this.x = cx - (cx - this.x) * (scale / this.scale);
      this.y = cy - (cy - this.y) * (scale / this.scale);
      this.scale = scale;
      this.apply();
    }
    zoomBy(factor) { const box = this.svg.getBoundingClientRect(); this.zoomAt(factor, box.width / 2, box.height / 2); }
    panBy(dx, dy) { this.x += dx; this.y += dy; this.apply(); }
    fitScale() {
      const box = this.svg.getBoundingClientRect();
      return Math.min((box.width - 32) / this.bounds.width, (box.height - 32) / this.bounds.height);
    }
    fit(maxScale = 1) {
      const box = this.svg.getBoundingClientRect();
      this.scale = Math.max(0.08, Math.min(this.fitScale(), maxScale));
      this.x = (box.width - this.bounds.width * this.scale) / 2;
      this.y = (box.height - this.bounds.height * this.scale) / 2;
      this.apply();
    }
    focusOn(target, scale) {  // the current box, centred, at a readable size
      const box = this.svg.getBoundingClientRect();
      this.scale = scale;
      this.x = box.width / 2 - (target.x + target.width / 2) * scale;
      this.y = box.height / 2 - (target.y + target.height / 2) * scale;
      this.apply();
    }
    start() {  // the current box centred; else the whole diagram if it stays readable, else its beginning at 60%
      if (this.current) { this.focusOn(this.current, Math.max(0.85, Math.min(1, this.fitScale()))); return; }
      if (this.fitScale() >= 0.6) { this.fit(1); return; }
      const box = this.svg.getBoundingClientRect();
      this.scale = 0.6;
      this.x = 16;
      this.y = Math.max(16, (box.height - this.bounds.height * this.scale) / 2);
      this.apply();
    }
  }

  // ---- the page's one call ------------------------------------------------------------------------------------
  const laidOut = new Map();  // chart → its layout, so going back is instant
  let turn = 0;
  async function draw(holder, chart, { onGo, onZoom }) {
    const mine = ++turn;
    const key = chart.key();
    let laid = laidOut.get(key);
    if (!laid) {
      const elk = await loadElk();
      laid = await elk.layout(elkGraph(chart));
      laidOut.set(key, laid);
    }
    if (mine !== turn) return null;
    const picture = render(chart, laid);
    holder.replaceChildren(picture.svg);
    const view = new Viewport(picture, onGo);
    view.onZoom = onZoom;
    view.start();
    return view;
  }

  window.Diagram = { Chart, draw, loadElk };
})();
