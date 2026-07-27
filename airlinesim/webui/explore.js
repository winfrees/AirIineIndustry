// Outcome Explorer — plain JS, no framework/build step, same as app.js.
// Talks to the /api/explore/* endpoints in server.py.
//
// The graph is hand-laid-out SVG rather than a library: the project ships no
// third-party runtime deps and the PWA has no CDN access, so a tidy-tree
// layout in ~40 lines is the honest option.

const $ = (id) => document.getElementById(id);
const els = {
  graph: $("graph"), viewport: $("viewport"), edges: $("edges"), nodes: $("nodes"),
  stats: $("stats"), ver: $("ver"), toast: $("toast"), busy: $("busy"),
  legend: $("legend"), nodeId: $("nodeId"), nodeBody: $("nodeBody"),
  derivExpr: $("derivExpr"), derivStatus: $("derivStatus"), varList: $("varList"),
  mutList: $("mutList"), branchCycles: $("branchCycles"),
  sweepKind: $("sweepKind"), sweepTarget: $("sweepTarget"), sweepFrom: $("sweepFrom"),
  sweepTo: $("sweepTo"), sweepCount: $("sweepCount"), sweepCycles: $("sweepCycles"),
  sweepDepth: $("sweepDepth"), sweepCost: $("sweepCost"), rootCycles: $("rootCycles"),
};

const NODE_W = 132, NODE_H = 46, COL_GAP = 62, ROW_GAP = 14;

let tree = null;        // last /api/explore/tree payload
let targets = null;     // pickable mutation targets
let selected = null;    // selected node_id
let deriv = null;       // { expr, results: {node_id: {value, error}} }
let view = { x: 40, y: 40, k: 1 };
let toastTimer = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function money(n) {
  const a = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(1)}k`;
  return `${sign}$${a.toFixed(0)}`;
}

function num(v) {
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(Math.abs(v) < 10 ? 3 : 1);
}

function toast(msg, isErr) {
  els.toast.textContent = msg;
  els.toast.classList.toggle("err", !!isErr);
  els.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 3200);
}

let busyDepth = 0;
function setBusy(on) {
  busyDepth += on ? 1 : -1;
  if (busyDepth < 0) busyDepth = 0;
  els.busy.classList.toggle("hidden", busyDepth === 0);
}

async function api(path, body) {
  setBusy(true);
  try {
    const res = body === undefined
      ? await fetch(path)
      : await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    const data = await res.json().catch(() => ({ ok: false, message: "bad response" }));
    if (!res.ok || data.ok === false) {
      throw new Error(data.message || data.error || `HTTP ${res.status}`);
    }
    return data;
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------- layout
// Tidy tree: depth on X, one row per leaf on Y, parents centred on children.
function layout(nodes) {
  const byId = new Map(nodes.map((n) => [n.node_id, n]));
  const kids = new Map();
  let roots = [];
  for (const n of nodes) {
    if (n.parent_id && byId.has(n.parent_id)) {
      if (!kids.has(n.parent_id)) kids.set(n.parent_id, []);
      kids.get(n.parent_id).push(n.node_id);
    } else {
      roots.push(n.node_id);
    }
  }
  const pos = new Map();
  let row = 0;
  const walk = (id) => {
    const cs = kids.get(id) || [];
    const n = byId.get(id);
    if (!cs.length) {
      pos.set(id, { x: n.depth * (NODE_W + COL_GAP), y: row * (NODE_H + ROW_GAP) });
      row += 1;
      return pos.get(id).y;
    }
    const ys = cs.map(walk);
    const y = (Math.min(...ys) + Math.max(...ys)) / 2;
    pos.set(id, { x: n.depth * (NODE_W + COL_GAP), y });
    return y;
  };
  roots.forEach(walk);
  return { pos, kids, byId };
}

function svgEl(tag, attrs) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

function outcomeClass(nodeId) {
  if (!deriv) return "";
  const r = deriv.results[nodeId];
  if (!r) return "";
  if (r.error) return "err";
  if (typeof r.value === "boolean") return r.value ? "pass" : "fail";
  return "";  // numeric derivations annotate rather than colour
}

function derivLabel(nodeId) {
  if (!deriv) return "";
  const r = deriv.results[nodeId];
  if (!r) return "";
  if (r.error) return "!";
  if (typeof r.value === "boolean") return r.value ? "true" : "false";
  return num(r.value);
}

function render() {
  if (!tree) return;
  const { pos, byId } = layout(tree.nodes);
  els.edges.textContent = "";
  els.nodes.textContent = "";

  const pathIds = new Set();
  if (selected) {
    let cur = byId.get(selected);
    while (cur) {
      pathIds.add(cur.node_id);
      cur = cur.parent_id ? byId.get(cur.parent_id) : null;
    }
  }

  for (const n of tree.nodes) {
    if (!n.parent_id || !pos.has(n.parent_id)) continue;
    const a = pos.get(n.parent_id), b = pos.get(n.node_id);
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    const x2 = b.x, y2 = b.y + NODE_H / 2;
    const mx = (x1 + x2) / 2;
    const onPath = pathIds.has(n.node_id) && pathIds.has(n.parent_id);
    els.edges.appendChild(svgEl("path", {
      d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`,
      class: `edge${onPath ? " on-path" : ""}`,
    }));
  }

  for (const n of tree.nodes) {
    const p = pos.get(n.node_id);
    const cls = ["node", outcomeClass(n.node_id)];
    if (n.node_id === selected) cls.push("selected");
    if (!n.parent_id) cls.push("root");
    const g = svgEl("g", { class: cls.filter(Boolean).join(" "),
                           transform: `translate(${p.x},${p.y})` });
    g.appendChild(svgEl("rect", { width: NODE_W, height: NODE_H }));

    const label = n.label.length > 20 ? n.label.slice(0, 19) + "…" : n.label;
    const t1 = svgEl("text", { x: 8, y: 16 });
    t1.textContent = label;
    g.appendChild(t1);

    const human = (n.metrics && n.metrics.players.find((q) => q.is_human)) || null;
    const t2 = svgEl("text", { x: 8, y: 30, class: "sub" });
    t2.textContent = `day ${n.day}` + (human ? ` · ${money(human.net_worth)}` : "");
    g.appendChild(t2);

    const t3 = svgEl("text", { x: 8, y: 41, class: "sub val" });
    const dl = derivLabel(n.node_id);
    t3.textContent = human
      ? `LF ${human.load_factor.toFixed(2)} · ${money(human.profit)}/d${dl ? ` · ${dl}` : ""}`
      : dl;
    g.appendChild(t3);

    g.addEventListener("click", (ev) => { ev.stopPropagation(); select(n.node_id); });
    els.nodes.appendChild(g);
  }
  applyView();
  updateStats();
}

function applyView() {
  els.viewport.setAttribute("transform",
    `translate(${view.x},${view.y}) scale(${view.k})`);
}

function fit() {
  if (!tree || !tree.nodes.length) return;
  const { pos } = layout(tree.nodes);
  const xs = [...pos.values()].map((p) => p.x), ys = [...pos.values()].map((p) => p.y);
  const w = Math.max(...xs) + NODE_W, h = Math.max(...ys) + NODE_H;
  const box = els.graph.getBoundingClientRect();
  const k = Math.min((box.width - 60) / w, (box.height - 60) / h, 1.4);
  view.k = Math.max(0.15, k);
  view.x = 30 - Math.min(...xs) * view.k;
  view.y = (box.height - h * view.k) / 2 - Math.min(...ys) * view.k;
  applyView();
}

function updateStats() {
  if (!tree) return;
  const kb = (tree.state_bytes / 1024).toFixed(0);
  els.stats.textContent =
    `${tree.node_count}/${tree.max_nodes} nodes · ${kb} KB state · 1 cycle = ${tree.cycle_hours}h`;
}

// ---------------------------------------------------------------- panning
let drag = null;
els.graph.addEventListener("mousedown", (e) => {
  drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
  els.graph.classList.add("panning");
});
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  view.x = drag.vx + (e.clientX - drag.x);
  view.y = drag.vy + (e.clientY - drag.y);
  applyView();
});
window.addEventListener("mouseup", () => {
  drag = null;
  els.graph.classList.remove("panning");
});
els.graph.addEventListener("wheel", (e) => {
  e.preventDefault();
  const box = els.graph.getBoundingClientRect();
  const mx = e.clientX - box.left, my = e.clientY - box.top;
  const k2 = Math.min(3, Math.max(0.15, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
  // keep the point under the cursor fixed while zooming
  view.x = mx - (mx - view.x) * (k2 / view.k);
  view.y = my - (my - view.y) * (k2 / view.k);
  view.k = k2;
  applyView();
}, { passive: false });
els.graph.addEventListener("click", () => select(null));

// ---------------------------------------------------------------- selection
function select(nodeId) {
  selected = nodeId;
  render();
  if (!nodeId) {
    els.nodeId.textContent = "";
    els.nodeBody.className = "empty";
    els.nodeBody.textContent = "Select a node in the graph.";
    return;
  }
  showNode(nodeId);
}

async function showNode(nodeId) {
  let d;
  try {
    d = await api(`/api/explore/node?id=${encodeURIComponent(nodeId)}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  els.nodeId.textContent = d.node_id;
  els.nodeBody.className = "";
  const m = d.metrics;
  const rows = m.players.map((p) => `
    <tr>
      <td>${esc(p.name)}${p.is_human ? " ★" : ""}</td>
      <td class="num">${money(p.net_worth)}</td>
      <td class="num">${money(p.profit)}</td>
      <td class="num">${p.load_factor.toFixed(2)}</td>
      <td class="num">${p.pax.toFixed(0)}</td>
    </tr>`).join("");

  const recipe = d.path.map((s) => {
    const muts = s.mutations.map((x) => `<code>${esc(x.describe)}</code>`).join(", ");
    return `<div>${esc(s.node_id)}: ${muts || "<em>no edits</em>"} → run ${s.cycles}</div>`;
  }).join("");

  const dr = deriv && deriv.results[nodeId];
  const drLine = dr
    ? `<div class="status ${dr.error ? "bad" : "ok"}">${esc(deriv.expr)} = ${
        dr.error ? esc(dr.error) : esc(String(dr.value))}</div>`
    : "";

  els.nodeBody.innerHTML = `
    <table class="metrics">
      <thead><tr><th>carrier</th><th>net worth</th><th>profit/d</th><th>LF</th><th>pax</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="recipe"><strong>day ${m.day}</strong> · fuel $${m.fuel_spot.toFixed(2)}/L ·
       gates ${m.gates_used}/${m.gates_total} · state ${(d.state_bytes / 1024).toFixed(0)} KB</p>
    ${drLine}
    <p class="recipe"><strong>recipe from root</strong>${recipe}</p>
    <div class="node-actions">
      <button class="btn ghost small" id="btnDelete" ${d.parent_id ? "" : "disabled"}>Delete subtree</button>
    </div>`;

  const del = $("btnDelete");
  if (del) del.addEventListener("click", async () => {
    if (!confirm(`Delete ${nodeId} and everything under it?`)) return;
    try {
      const r = await api("/api/explore/delete", { node_id: nodeId });
      toast(`removed ${r.removed} node(s)`);
      selected = null;
      await refresh();
      select(null);
    } catch (e) { toast(e.message, true); }
  });
}

// ---------------------------------------------------------------- controls
function targetOptions(kind) {
  const spec = (tree.mutation_kinds || []).find((k) => k.kind === kind);
  const tk = spec ? spec.target_kind : "route_op";
  if (!targets) return [];
  if (tk === "route_op") {
    return [{ id: "*", label: "every route op" }]
      .concat((targets.players || []).map((p) => ({ id: p.id, label: `all of ${p.name}` })))
      .concat((targets.route_ops || []).map((o) => ({ id: o.id, label: o.label })));
  }
  if (tk === "player") {
    return [{ id: "*", label: "every carrier" }]
      .concat((targets.players || []).map((p) => ({ id: p.id, label: p.name })));
  }
  if (tk === "airport") {
    return [{ id: "*", label: "every airport" }]
      .concat((targets.airports || []).map((a) => ({ id: a.id, label: a.label })));
  }
  if (tk === "route") {
    return [{ id: "*", label: "every route" }]
      .concat((targets.routes || []).map((r) => ({ id: r.id, label: r.label })));
  }
  return [{ id: "*", label: "global" }];
}

function fillSelect(sel, opts) {
  sel.textContent = "";
  for (const o of opts) {
    const el = document.createElement("option");
    el.value = o.id;
    el.textContent = o.label;
    sel.appendChild(el);
  }
}

function addMutRow(kind, target, value) {
  const row = document.createElement("div");
  row.className = "mut-row";
  const kindSel = document.createElement("select");
  fillSelect(kindSel, tree.mutation_kinds.map((k) => ({ id: k.kind, label: k.label })));
  kindSel.value = kind || tree.mutation_kinds[0].kind;
  const tgtSel = document.createElement("select");
  fillSelect(tgtSel, targetOptions(kindSel.value));
  if (target) tgtSel.value = target;
  const val = document.createElement("input");
  val.type = "number"; val.step = "any";
  val.value = value === undefined ? 1 : value;
  const rm = document.createElement("button");
  rm.type = "button"; rm.className = "rm"; rm.textContent = "✕";
  rm.title = "remove this edit";
  rm.addEventListener("click", () => row.remove());
  kindSel.addEventListener("change", () => fillSelect(tgtSel, targetOptions(kindSel.value)));
  row.append(kindSel, tgtSel, val, rm);
  els.mutList.appendChild(row);
}

function readMutations() {
  return [...els.mutList.querySelectorAll(".mut-row")].map((r) => {
    const [kindSel, tgtSel, val] = r.children;
    return { kind: kindSel.value, target: tgtSel.value, value: parseFloat(val.value) };
  }).filter((m) => Number.isFinite(m.value));
}

function updateSweepCost() {
  const b = Math.max(1, parseInt(els.sweepCount.value, 10) || 1);
  const d = Math.max(1, parseInt(els.sweepDepth.value, 10) || 1);
  let total = 0;
  for (let i = 1; i <= d; i++) total += Math.pow(b, i);
  els.sweepCost.textContent = `= ${total} node${total === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------- data
async function refresh() {
  tree = await api("/api/explore/tree");
  if (!targets) targets = await api("/api/explore/targets");
  if (deriv) await applyDerivation(deriv.expr, true);
  render();
}

async function applyDerivation(expr, quiet) {
  if (!expr || !expr.trim()) { deriv = null; return; }
  let r;
  try {
    r = await api("/api/explore/evaluate", { expr });
  } catch (e) {
    deriv = null;
    els.derivStatus.className = "status bad";
    els.derivStatus.textContent = e.message;
    els.legend.classList.add("hidden");
    render();
    return;
  }
  deriv = { expr, results: r.results };
  const vals = Object.values(r.results);
  const bools = vals.filter((v) => typeof v.value === "boolean");
  const errs = vals.filter((v) => v.error).length;
  els.derivStatus.className = "status ok";
  els.derivStatus.textContent = bools.length
    ? `${bools.filter((v) => v.value).length} true · ${bools.filter((v) => !v.value).length} false` +
      (errs ? ` · ${errs} error` : "")
    : `${vals.length - errs} numeric result(s)` + (errs ? ` · ${errs} error` : "");
  els.legend.classList.toggle("hidden", !bools.length && !errs);
  els.legend.innerHTML =
    `<span class="sw"><i class="dot pass"></i>true</span>` +
    `<span class="sw"><i class="dot fail"></i>false</span>` +
    (errs ? `<span class="sw"><i class="dot err"></i>error</span>` : "");
  if (!quiet) render();
}

function renderVarList() {
  const names = (targets.players || []).map((p) => p.id.toLowerCase()).join("</code>, <code>");
  els.varList.innerHTML =
    `<div>carriers: <code>human</code>, <code>ai</code>, <code>p0</code>…, <code>${names}</code></div>` +
    `<div>fields: <code>cash</code>, <code>debt</code>, <code>assets</code>, <code>net_worth</code>, ` +
    `<code>fleet</code>, <code>routes</code>, <code>flying</code>, <code>pax</code>, ` +
    `<code>revenue</code>, <code>profit</code>, <code>load_factor</code>, <code>grounded</code></div>` +
    `<div>world: <code>day</code>, <code>fuel_spot</code>, <code>gate_utilization</code></div>` +
    `<div>allowed: arithmetic, comparisons, <code>and/or/not</code>, ` +
    `<code>abs min max round</code></div>`;
}

// ---------------------------------------------------------------- wiring
$("formDerivation").addEventListener("submit", async (e) => {
  e.preventDefault();
  await applyDerivation(els.derivExpr.value);
  if (selected) showNode(selected);
});

$("btnClearDeriv").addEventListener("click", () => {
  els.derivExpr.value = "";
  deriv = null;
  els.derivStatus.textContent = "";
  els.legend.classList.add("hidden");
  render();
});

$("btnAddMut").addEventListener("click", () => addMutRow());

$("formBranch").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/explore/branch", {
      parent: selected || tree.root_id,
      mutations: readMutations(),
      cycles: parseInt(els.branchCycles.value, 10) || 0,
    });
    await refresh();
    select(r.node_id);
    toast(`branched ${r.node_id}`);
  } catch (err) { toast(err.message, true); }
});

$("formSweep").addEventListener("submit", async (e) => {
  e.preventDefault();
  const depth = parseInt(els.sweepDepth.value, 10) || 1;
  const body = {
    parent: selected || tree.root_id,
    kind: els.sweepKind.value,
    target: els.sweepTarget.value,
    from: parseFloat(els.sweepFrom.value),
    to: parseFloat(els.sweepTo.value),
    count: parseInt(els.sweepCount.value, 10) || 1,
    cycles: parseInt(els.sweepCycles.value, 10) || 0,
  };
  try {
    const r = depth > 1
      ? await api("/api/explore/expand", { ...body, depth })
      : await api("/api/explore/sweep", body);
    await refresh();
    fit();
    toast(`created ${r.created.length} node(s)`);
  } catch (err) { toast(err.message, true); }
});

$("btnReset").addEventListener("click", async () => {
  if (!confirm("Discard the whole tree and start from a fresh root?")) return;
  try {
    await api("/api/explore/reset", {
      cycles: parseInt(els.rootCycles.value, 10) || 0,
    });
    deriv = null;
    selected = null;
    els.derivStatus.textContent = "";
    els.legend.classList.add("hidden");
    await refresh();
    fit();
    select(null);
    toast("tree reset");
  } catch (e) { toast(e.message, true); }
});

$("btnFit").addEventListener("click", fit);
els.sweepCount.addEventListener("input", updateSweepCost);
els.sweepDepth.addEventListener("input", updateSweepCost);
els.sweepKind.addEventListener("change",
  () => fillSelect(els.sweepTarget, targetOptions(els.sweepKind.value)));

async function boot() {
  try {
    tree = await api("/api/explore/tree");
    targets = await api("/api/explore/targets");
  } catch (e) {
    toast(`explorer unavailable: ${e.message}`, true);
    return;
  }
  fillSelect(els.sweepKind, tree.mutation_kinds.map((k) => ({ id: k.kind, label: k.label })));
  els.sweepKind.value = "price_scale";
  fillSelect(els.sweepTarget, targetOptions("price_scale"));
  addMutRow("price_scale", "*", 1.1);
  renderVarList();
  updateSweepCost();
  els.derivExpr.placeholder = "human.net_worth > ai.net_worth";
  render();
  fit();
  // The version comes from the game snapshot — same source the game GUI uses,
  // so both screens name the build that is actually serving.
  fetch("/api/state").then((r) => r.json())
    .then((s) => { els.ver.textContent = `v${s.engine_version}`; })
    .catch(() => {});
}

boot();
