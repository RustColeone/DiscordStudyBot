"use strict";
// Diagrams for the architecture page. A chart is described as nodes, groups and edges (Chart), laid out by ELK (the
// Eclipse Layout Kernel: layered, with right-angled edges and groups inside groups) and drawn here as SVG, with
// zooming and panning (wheel, drag, pinch, keys and buttons) for a mouse and a finger alike. Hovering a box lights up
// what it is connected to.
(() => {
  const ELK_URLS = ["https://cdn.jsdelivr.net/npm/elkjs@0.9.3/lib/elk.bundled.js",
                    "https://unpkg.com/elkjs@0.9.3/lib/elk.bundled.js"];
  const SVG = "http://www.w3.org/2000/svg";
  const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  // The letter on each kind of box.
  const BADGE = { module: "M", folder: "D", file: "F", class: "C", function: "ƒ", method: "ƒ", section: "§",
                  external: "↗", gateway: "G", manager: "M", providers: "P", store: "DB", startup: "S",
                  supervisor: "S", contracts: "K", guide: "?", data: "≡" };

  // ---- describing a chart ---------------------------------------------------------------------------------
  class Chart {
    // compact: each box laid out on its own and the boxes packed tight; edges crossing between boxes are drawn as
    // curves (a file's classes and sections). Otherwise ELK routes every edge through the groups (the top map).
    // overview: it opens fitted whole, however small (the system, the guide)
    constructor(direction = "RIGHT", { compact = false, overview = false } = {}) {
      this.direction = direction;
      this.compact = compact;
      this.overview = overview;
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
    // pack: its boxes in a grid (they have no edges inside); target: where a click on its title goes
    group(title, build, { kind = "plain", pack = false, target = "", current = false, tip = "" } = {}) {
      const id = `g${this.count++}`;
      this.groups.push({ id, title: String(title), kind, pack, target, current, tip: String(tip || ""),
                         parent: this.stack.at(-1) || null });
      this.stack.push(id);
      build();
      this.stack.pop();
      return id;
    }
    edge(from, to, { dashed = false, label = "", arrow = "end" } = {}) {  // arrow: end, both or none
      if (from && to && from !== to) this.edges.push({ id: `e${this.edges.length}`, from, to, dashed, label, arrow });
    }
    // The diagram without its highlight: the same structure lays out the same, so a click inside it keeps the view.
    structure() {
      const plain = (items) => items.map((item) => ({ ...item, current: false }));
      return JSON.stringify([this.direction, this.compact, plain(this.nodes), plain(this.groups), this.edges]);
    }
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
    const compact = chart.compact;
    const root = {
      id: "root", children: [], edges: [],
      layoutOptions: {
        "elk.algorithm": "layered", "elk.direction": chart.direction, "elk.hierarchyHandling": "INCLUDE_CHILDREN",
        "elk.edgeRouting": "ORTHOGONAL", "elk.layered.spacing.nodeNodeBetweenLayers": "54", "elk.spacing.nodeNode": "18",
        "elk.layered.spacing.edgeNodeBetweenLayers": "16", "elk.spacing.edgeEdge": "9", "elk.spacing.edgeNode": "14",
        "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF", "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
        "elk.separateConnectedComponents": "true", "elk.spacing.componentComponent": "34", "elk.aspectRatio": "1.7",
        "elk.edgeLabels.placement": "CENTER", "elk.padding": "[top=24,left=24,bottom=24,right=24]",
        ...(compact ? { "elk.hierarchyHandling": "SEPARATE_CHILDREN" } : {}),
      },
    };
    const holders = new Map([[null, root]]);
    const holdsGroups = new Set(chart.groups.map((group) => group.parent));
    const holdsNodes = new Set(chart.nodes.map((node) => node.parent));
    for (const group of chart.groups) {
      const minimum = Math.ceil(textWidth(group.title, 12, 700) + 40);
      const packed = group.pack || (compact && holdsGroups.has(group.id) && !holdsNodes.has(group.id));  // boxes of boxes
      holders.set(group.id, { id: group.id, children: [], edges: [], layoutOptions: {
        "elk.padding": "[top=38,left=16,bottom=16,right=16]", "elk.nodeSize.constraints": "MINIMUM_SIZE",
        "elk.nodeSize.minimum": `(${minimum}, 60)`,
        ...(compact ? { "elk.hierarchyHandling": "SEPARATE_CHILDREN", "elk.algorithm": "layered",
                        "elk.direction": chart.direction, "elk.edgeRouting": "ORTHOGONAL",
                        "elk.layered.spacing.nodeNodeBetweenLayers": "40", "elk.spacing.nodeNode": "12",
                        "elk.separateConnectedComponents": "true", "elk.spacing.componentComponent": "18",
                        "elk.aspectRatio": "1.3" } : {}),
        ...(packed ? { "elk.hierarchyHandling": "SEPARATE_CHILDREN", "elk.algorithm": "rectpacking",
                       "elk.aspectRatio": group.pack ? "2.2" : "1.5", "elk.spacing.nodeNode": group.pack ? "12" : "18" } : {}) } });
    }
    for (const group of chart.groups) holders.get(group.parent).children.push(holders.get(group.id));
    for (const node of chart.nodes) holders.get(node.parent).children.push({ id: node.id, ...boxSize(node) });
    // Compact: an edge goes to ELK only inside the box holding both its ends; the others are drawn as curves.
    const parentOf = new Map([...chart.nodes, ...chart.groups].map((item) => [item.id, item.parent]));
    for (const edge of chart.edges) {
      const holder = compact ? parentOf.get(edge.from) : null;
      if (compact && holder !== parentOf.get(edge.to)) continue;
      holders.get(holder).edges.push({
        id: edge.id, sources: [edge.from], targets: [edge.to],
        labels: edge.label ? [{ text: edge.label, width: Math.ceil(textWidth(edge.label, 11) + 12), height: 18 }] : [],
      });
    }
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

  function curve(a, b) {  // from the side of box a facing box b, to the facing side of b
    const ac = { x: a.x + a.width / 2, y: a.y + a.height / 2 }, bc = { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    let start, end, c1, c2;
    if (b.x >= a.x + a.width + 8 || a.x >= b.x + b.width + 8) {
      const right = b.x >= a.x + a.width + 8;
      start = { x: right ? a.x + a.width : a.x, y: ac.y };
      end = { x: right ? b.x : b.x + b.width, y: bc.y };
      const pull = Math.max(36, Math.abs(end.x - start.x) / 2) * (right ? 1 : -1);
      [c1, c2] = [{ x: start.x + pull, y: start.y }, { x: end.x - pull, y: end.y }];
    } else {
      const below = bc.y > ac.y;
      start = { x: ac.x, y: below ? a.y + a.height : a.y };
      end = { x: bc.x, y: below ? b.y : b.y + b.height };
      const pull = Math.max(28, Math.abs(end.y - start.y) / 2) * (below ? 1 : -1);
      [c1, c2] = [{ x: start.x, y: start.y + pull }, { x: end.x, y: end.y - pull }];
    }
    return `M${start.x},${start.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${end.x},${end.y}`;
  }

  function render(chart, laid) {
    const where = new Map();  // every box, group and the root: its place on the page
    (function place(node, x, y) {
      const box = { x: x + (node.x || 0), y: y + (node.y || 0), width: node.width || 0, height: node.height || 0 };
      where.set(node.id, box);
      for (const child of node.children || []) place(child, box.x, box.y);
    })(laid, 0, 0);
    const dense = chart.edges.length > 30;  // many edges: drawn lighter until a box is hovered
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

    const groups = new Map();
    for (const group of chart.groups) {
      const box = where.get(group.id);
      if (!box) continue;
      const g = el("g", { class: `grp g-${group.kind}${group.target ? " link" : ""}${group.current ? " current" : ""}`,
                          "data-id": group.id });
      g.append(el("rect", { class: "grp-box", x: box.x, y: box.y, width: box.width, height: box.height, rx: 14 }),
               el("text", { x: box.x + 14, y: box.y + 23, class: "grp-title" }, group.title));
      if (group.target) {  // its title strip is what a click opens; the rest of it pans like the background
        g.append(el("rect", { class: "grp-hit", x: box.x, y: box.y, width: box.width, height: 34, rx: 14 }));
        g.setAttribute("tabindex", "0");
        g.setAttribute("role", "link");
        g.setAttribute("aria-label", group.title);
      }
      if (group.tip) g.append(el("title", {}, group.tip));
      groupsLayer.append(g);
      groups.set(group.id, g);
    }

    const links = new Map();  // box id → the edges and boxes it touches, for lighting them up
    const touch = (id, item) => { if (!links.has(id)) links.set(id, []); links.get(id).push(item); };
    const edgeOf = new Map(chart.edges.map((edge) => [edge.id, edge]));
    const laidEdges = [];  // [edge, the box it was laid out in]
    (function collect(node) {
      for (const edge of node.edges || []) laidEdges.push([edge, node.id]);
      for (const child of node.children || []) collect(child);
    })(laid);
    const drawn = new Set();
    for (const [laidEdge, holder] of laidEdges) {
      const edge = edgeOf.get(laidEdge.id);
      const section = laidEdge.sections?.[0];
      if (!edge || !section) continue;
      drawn.add(edge.id);
      const offset = where.get(laidEdge.container || holder) || { x: 0, y: 0 };
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
    for (const edge of chart.edges) {  // between boxes laid out apart: a curve from side to side
      const a = where.get(edge.from), b = where.get(edge.to);
      if (drawn.has(edge.id) || !a || !b) continue;
      // A dotted curve (a call into another file) stays hidden until one of its ends is hovered.
      const g = el("g", { class: `edge-g free${edge.dashed ? " dashed quiet" : ""}` });
      const path = el("path", { d: curve(a, b), class: "edge" });
      if (edge.arrow !== "none") path.setAttribute("marker-end", "url(#arrow)");
      if (edge.arrow === "both") path.setAttribute("marker-start", "url(#arrow-start)");
      g.append(path);
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
      (nodes.get(id) || groups.get(id))?.classList.toggle("on", on);
      for (const item of links.get(id) || []) {
        item.line.classList.toggle("on", on);
        (nodes.get(item.other) || groups.get(item.other))?.classList.toggle("on", on);
      }
    };
    for (const [id, g] of nodes) {
      g.addEventListener("pointerenter", () => light(id, true));
      g.addEventListener("pointerleave", () => light(id, false));
      g.addEventListener("focus", () => light(id, true));
      g.addEventListener("blur", () => light(id, false));
    }
    for (const [id, g] of groups) {
      const hit = g.querySelector(".grp-hit");
      if (!hit) continue;
      hit.addEventListener("pointerenter", () => light(id, true));
      hit.addEventListener("pointerleave", () => light(id, false));
    }
    const bounds = where.get("root");
    const items = [...chart.nodes, ...chart.groups];
    const current = items.find((item) => item.current && where.has(item.id));
    // Where a big diagram begins: its first box along the flow (left-most, or top-most going down).
    const down = chart.direction === "DOWN";
    const first = chart.nodes.map((node) => where.get(node.id)).filter(Boolean)
      .sort((a, b) => (down ? a.y - b.y || a.x - b.x : a.x - b.x || a.y - b.y))[0] || null;
    return { svg, world, bounds: { x: 0, y: 0, width: bounds.width, height: bounds.height }, first, overview: chart.overview,
             current: current ? where.get(current.id) : null,
             targets: new Map(items.filter((item) => item.target).map((item) => [item.id, item.target])) };
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
        if (this.pointers.size === 1) {
          this.origin = this.point(event);
          this.moved = false;
          this.slack = event.pointerType === "mouse" ? 4 : 10;  // a finger shakes more than a mouse: still a tap
          this.pressed = event.target.closest(".node.link") || (event.target.classList.contains("grp-hit") ? event.target.parentNode : null);
        }
        svg.setPointerCapture(event.pointerId);
      });
      svg.addEventListener("pointermove", (event) => {
        if (!this.pointers.has(event.pointerId)) return;
        const before = this.pointers.get(event.pointerId);
        const now = this.point(event);
        if (this.pointers.size === 1) {
          if (Math.hypot(now[0] - this.origin[0], now[1] - this.origin[1]) > this.slack) this.moved = true;
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
      // The frame changes size (a phone turned, its address bar shown or hidden, a window resized): the point that
      // was in the middle stays in the middle.
      this.size = null;
      new ResizeObserver(() => {
        const box = svg.getBoundingClientRect();
        if (!box.width || !box.height) return;
        if (this.size) this.panBy((box.width - this.size.width) / 2, (box.height - this.size.height) / 2);
        this.size = { width: box.width, height: box.height };
      }).observe(svg);
      svg.addEventListener("keydown", (event) => {
        const node = event.target.closest?.(".node.link, .grp.link");
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
    set(scale, x, y) { Object.assign(this, { scale, x, y }); this.apply(); }
    keep(scale) {  // another diagram, at the user's zoom: the current box centred, else all of it, else its beginning
      const box = this.svg.getBoundingClientRect();
      if (this.current) { this.focusOn(this.current, scale); return; }
      const width = this.bounds.width * scale, height = this.bounds.height * scale;
      if (width <= box.width && height <= box.height) {
        this.set(scale, (box.width - width) / 2, (box.height - height) / 2);
      } else {
        this.begin(scale);
      }
    }
    begin(scale) {  // the diagram's first box near the top left, at this zoom
      const box = this.svg.getBoundingClientRect();
      const first = this.first || { x: 0, y: 0, width: 0, height: 0 };
      this.set(scale, 40 - first.x * scale, Math.max(24, box.height / 3 - (first.y + first.height / 2) * scale));
    }
    start() {  // the current box centred; else the whole diagram if it stays readable, else its beginning at 70%
      if (this.current) { this.focusOn(this.current, Math.max(0.85, Math.min(1, this.fitScale()))); return; }
      if (this.overview || this.fitScale() >= 0.55) { this.fit(1); return; }
      this.begin(0.7);
    }
  }

  // ---- the page's one call ------------------------------------------------------------------------------------
  const laidOut = new Map();  // structure → its layout, so a click or going back lays nothing out again
  let turn = 0;
  let last = null;  // the latest diagram's structure and view
  async function draw(holder, chart, { onGo, onZoom }) {
    const mine = ++turn;
    const structure = chart.structure();
    let laid = laidOut.get(structure);
    if (!laid) {
      const elk = await loadElk();
      laid = await elk.layout(elkGraph(chart));
      laidOut.set(structure, laid);
    }
    if (mine !== turn) return null;
    const before = last;
    const picture = render(chart, laid);
    holder.replaceChildren(picture.svg);
    const view = new Viewport(picture, onGo);
    view.onZoom = onZoom;
    // The same diagram (a click inside it): exactly where the user was. Another one: at the user's zoom.
    if (before && before.structure === structure) view.set(before.view.scale, before.view.x, before.view.y);
    else if (before && !picture.overview) view.keep(before.view.scale);
    else view.start();
    last = { structure, view };
    return view;
  }

  window.Diagram = { Chart, draw, loadElk };
})();
