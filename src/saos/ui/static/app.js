"use strict";
// saos ui：流程图 + 右侧面板。数据全部来自本机服务（任务目录与每轮的 viz.json），这里只负责画。

const Q = new URLSearchParams(location.search);
const JOB = Q.get("job");
const TOKEN = Q.get("t") || "";
const POLL_MS = 1500;

const TEXT = {
  zh: {
    interview: "访谈与可行性判断", demand_confirm: "确认需求单", modeling: "建模与选材", model_approve: "确认建模说明", design: "自主设计", deliver: "交付报告",
    no_demand: "访谈中，尚无需求单", invalid_demand: "需求单未通过校验", first: "等待用户确认", reconfirm: "需求已改动，待重新确认",
    waiting: "等你确认", blocked: "待回答", active: "进行中", running: "运行中",
    rounds_n: (n) => `第 ${n} 轮`, no_rounds: "尚未记录轮次",
    st_achieved: "已达成", st_exhausted: "无法再优化", st_limit: "达到轮次上限", st_stopped: "按用户决定停止",
    report_ready: "报告已生成", report_todo: "待生成报告",
    back_demand: "需求变更", back_model: "修改模型", back_reach: "结构不可达", feasibility: "质疑结构 · 手算界限",
    explicit: "显式调试 · Interactive Extension", standalone: "后台运行 · standalone", no_daemon: "后台进程未运行",
    tab_control: "窗口控制", tab_layout: "布局", tab_spot: "点列图", tab_mtf: "MTF", tab_rounds: "轮次",
    blocking: "blocking 待确认问题：",
    viewer: "布局窗口", viewer_p: "切换 OpticStudio 的布局窗口。剖面图画不了的系统（非轴对称、非序列）会自动改用 3D Viewer。",
    v_cross: "剖面图", v_3d: "3D Viewer", v_shaded: "实体模型",
    analyses: "分析窗口", analyses_p: "Interactive Extension 连接期间，OpticStudio 的工具栏和 System Explorer 点不了。这里经后台进程打开分析窗口，之后每次改动镜头都会刷新；只看，不改镜头。后台进程正在跑长操作时，请求会排队到它结束。",
    system: "系统设置（只读）", refresh: "刷新", loading: "读取中…", queued: "已发送，后台进程忙时会排队…",
    opened: (n) => `已打开 ${n}`, failed: "失败", aperture: "孔径", units: "单位", stop: "光阑面", fields: "视场", waves: "波长", primary: "主",
    no_viz: "第 0 轮记录后，这里显示该轮的布局、点列图和 MTF。", viz_missing: "这一轮没有可视化数据（用 --no-viz 记录，或记录早于 saos ui）。",
    viz_failed: "这一轮的可视化导出失败：", spot_foot: (ref) => `点：批量光线追迹，相对主波长主光线，未计被拦光线。RMS / GEO：OpticStudio Standard Spot（参考 ${ref}，全部波长），单位 µm。`,
    mtf_foot: "曲线：OpticStudio FFT MTF 导出。", tangential: "子午 T", sagittal: "弧矢 S", diff: "衍射极限",
    layout_foot: "几何与光线坐标：ZOS-API（GetGlobalMatrix、GetSag、批量光线追迹）。",
    layout_hint_3d: "拖动旋转 · Shift 或右键拖动平移 · 滚轮缩放 · 双击复位", layout_hint_2d: "拖动平移 · 滚轮缩放 · 双击复位",
    image_z: "像面 z", round: "轮", tiers: "层级", gap: "目标差距", state: "状态", note: "说明", achieved: "达成", deliverable: "可交付",
    pick_round: "点一行在其他视图中查看该轮", sag_flat: "部分面读不到面形，按平面画。", select_round: (n) => `r${String(n).padStart(3, "0")}`,
    live: "当前镜头", live_unsaved: "未保存", no_live: "后台进程加载或改动镜头后，这里显示当前镜头的布局。", live_failed: "当前视图导出失败：",
    only_3d: "非轴对称系统只有 3D 视图", lens_data: "Lens Data", ld_type: "面型", ld_comment: "注释", ld_radius: "半径", ld_thickness: "厚度",
    ld_material: "材料", ld_sd: "半口径", ld_conic: "圆锥", ld_solve: "V 为变量，其他字母为求解或 pickup",
    clash_title: "结构交叠", crossing: (a, b, d) => `第 ${a} 面与第 ${b} 面在口径内相交（最深约 ${d}）`,
    overlap: (a, b, n) => `镜片 ${a} 与镜片 ${b} 相互穿插（${n} 个采样点落在另一片玻璃里）`,
  },
  en: {
    interview: "Interview & feasibility", demand_confirm: "Confirm demand", modeling: "Model & materials", model_approve: "Approve model", design: "Autonomous design", deliver: "Delivery report",
    no_demand: "interviewing, no demand yet", invalid_demand: "demand fails validation", first: "waiting for the user", reconfirm: "demand changed, confirm again",
    waiting: "your turn", blocked: "question", active: "in progress", running: "running",
    rounds_n: (n) => `round ${n}`, no_rounds: "no rounds yet",
    st_achieved: "achieved", st_exhausted: "exhausted", st_limit: "round limit", st_stopped: "stopped by user",
    report_ready: "report written", report_todo: "report pending",
    back_demand: "demand change", back_model: "revise model", back_reach: "unreachable", feasibility: "question · hand bounds",
    explicit: "Explicit · Interactive Extension", standalone: "Background · standalone", no_daemon: "daemon not running",
    tab_control: "Window controls", tab_layout: "Layout", tab_spot: "Spot Diagram", tab_mtf: "MTF", tab_rounds: "Rounds",
    blocking: "Blocking questions:",
    viewer: "Layout window", viewer_p: "Switch OpticStudio's layout window. Systems the cross-section cannot draw (non-axial, non-sequential) get the 3D Viewer.",
    v_cross: "Cross-section", v_3d: "3D Viewer", v_shaded: "Shaded model",
    analyses: "Analysis windows", analyses_p: "While Interactive Extension is connected, OpticStudio's ribbon and System Explorer are locked. These buttons open analysis windows through the daemon; they refresh after every lens change and never modify the lens. A busy daemon queues the request until its long operation ends.",
    system: "System settings (read only)", refresh: "Refresh", loading: "loading…", queued: "sent; queued while the daemon is busy…",
    opened: (n) => `opened ${n}`, failed: "failed", aperture: "Aperture", units: "Units", stop: "Stop surface", fields: "Fields", waves: "Wavelengths", primary: "primary",
    no_viz: "After round 0 is recorded, its layout, spot diagram and MTF appear here.", viz_missing: "This round has no visual data (recorded with --no-viz or before saos ui).",
    viz_failed: "The visual export of this round failed: ", spot_foot: (ref) => `Points: batch ray trace relative to the primary chief ray, vignetted rays left out. RMS / GEO: OpticStudio Standard Spot (reference ${ref}, all wavelengths), in µm.`,
    mtf_foot: "Curves: OpticStudio FFT MTF export.", tangential: "Tangential", sagittal: "Sagittal", diff: "Diff. limit",
    layout_foot: "Geometry and rays: ZOS-API (GetGlobalMatrix, GetSag, batch ray trace).",
    layout_hint_3d: "drag to rotate · Shift or right-drag to pan · wheel to zoom · double-click to reset", layout_hint_2d: "drag to pan · wheel to zoom · double-click to reset",
    image_z: "image z", round: "Round", tiers: "Tiers", gap: "Shortfall", state: "State", note: "Note", achieved: "achieved", deliverable: "deliverable",
    pick_round: "Click a row to view that round in the other tabs", sag_flat: "Some surface shapes could not be read; drawn flat.", select_round: (n) => `r${String(n).padStart(3, "0")}`,
    live: "Current lens", live_unsaved: "unsaved", no_live: "Once the daemon loads or changes the lens, its layout appears here.", live_failed: "The live view export failed: ",
    only_3d: "Non-axial systems have the 3D view only", lens_data: "Lens Data", ld_type: "Type", ld_comment: "Comment", ld_radius: "Radius", ld_thickness: "Thickness",
    ld_material: "Material", ld_sd: "Semi-dia.", ld_conic: "Conic", ld_solve: "V marks a variable, other letters a solve or pickup",
    clash_title: "Interference", crossing: (a, b, d) => `surfaces ${a} and ${b} cross inside their apertures (up to about ${d})`,
    overlap: (a, b, n) => `lenses ${a} and ${b} interpenetrate (${n} sample points inside the other's glass)`,
  },
};
const ANALYSIS_NAMES = {
  StandardSpot: ["点列图", "Spot Diagram"], FftMtf: ["FFT MTF", "FFT MTF"], RayFan: ["光线扇图", "Ray Fan"],
  FieldCurvatureAndDistortion: ["场曲 / 畸变", "Field Curv. / Distortion"], WavefrontMap: ["波前图", "Wavefront Map"],
  LongitudinalAberration: ["轴向像差", "Longitudinal Aberration"], LateralColor: ["垂轴色差", "Lateral Color"], SeidelDiagram: ["赛德尔图", "Seidel Diagram"],
  FftPsf: ["FFT PSF", "FFT PSF"], HuygensPsf: ["惠更斯 PSF", "Huygens PSF"], FootprintSettings: ["光斑足迹图", "Footprint Diagram"],
};
const DEFAULT_ANALYSES = Object.keys(ANALYSIS_NAMES);
const FIELD_COLORS = ["#4f8ff7", "#f26b3a", "#22b39a", "#e5a00d", "#e5508f", "#3fa34d", "#9b7cf2", "#58c4dd"];
const MATERIAL_COLORS = ["#5fb3a1", "#7b8fd4", "#c9a35a", "#b57cc4", "#6aaed6", "#d47b7b"];

let lang = "zh";
let state = null;
let stateText = "";
let tab = Q.get("tab");
let pinnedRound = null;
let roundShown = null;
let viz = null;
let liveSeen = null;
let lensDataOpen = Q.get("ld") === "1";
let dim = Q.get("dim") === "2d" ? "2d" : "3d";
let analyses = null;
const t = (k, ...a) => { const v = (TEXT[lang] || TEXT.zh)[k]; return typeof v === "function" ? v(...a) : (v ?? k); };
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, params = {}, body = null) {
  const qs = new URLSearchParams({ job: JOB, ...params }).toString();
  const res = await fetch(`/api/${path}?${qs}`, {
    method: body ? "POST" : "GET", headers: { "X-Saos-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify({ job: JOB, ...body }) : null,
  });
  const msg = await res.json();
  if (!msg.ok) throw new Error(msg.error?.message || "error");
  return msg.result;
}

function toast(text, err = false) {
  const el = $("toast");
  el.textContent = text; el.className = "toast" + (err ? " err" : ""); el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), err ? 6000 : 3000);
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}
const fmtG = (g) => (g == null ? "∞" : g === 0 ? "0" : Math.abs(g) >= 100 || Math.abs(g) < 0.01 ? g.toExponential(2) : g.toPrecision(3));
const sig = (v, n = 3) => (v === 0 ? "0" : Number(v.toPrecision(n)).toString());

// ---------------- 流程图 ----------------

const NODES = [
  { id: "interview", y: 14, h: 56 },
  { id: "demand_confirm", y: 106, h: 46, gate: true },
  { id: "modeling", y: 188, h: 56 },
  { id: "model_approve", y: 280, h: 46, gate: true },
  { id: "design", y: 362, h: 64 },
  { id: "deliver", y: 462, h: 56 },
];
const NX = 20, NW = 226;

function nodeSub(step) {
  const d = step.detail;
  if (step.id === "interview") return d ? t(d) : t("feasibility");
  if (step.id === "demand_confirm") return d ? t(d) : "saos demand confirm";
  if (step.id === "modeling") return step.status === "active" && state.progress ? `${state.progress.op}` : "lens describe";
  if (step.id === "model_approve") return typeof d === "string" ? d : "saos lens approve";
  if (step.id === "design") {
    if (!d) return "saos round";
    const last = d.last;
    if (!last) return t("no_rounds");
    return `${t("select_round", last.round)} · G ${fmtG(last.shortfall)} · ${last.tiers.join("+")}`;
  }
  if (step.id === "deliver") return d ? `${t("st_" + d)} · ${step.status === "done" ? t("report_ready") : t("report_todo")}` : "saos report";
  return "";
}

function renderFlow() {
  const svg = $("flow");
  const steps = Object.fromEntries(state.steps.map((s) => [s.id, s]));
  const running = !!state.progress;
  const parts = [`<defs><filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.6"/></filter></defs>`];
  const cx = NX + NW / 2;
  // 主干连线
  for (let i = 0; i < NODES.length - 1; i++) {
    const a = NODES[i], b = NODES[i + 1];
    const sa = steps[a.id], sb = steps[b.id];
    const y1 = a.y + a.h, y2 = b.y - 6;
    const live = ["active", "waiting", "blocked"].includes(sb.status) && sa.status === "done";
    const cls = live ? `live ${sb.status === "waiting" ? "waiting" : ""}` : sa.status === "done" && sb.status !== "pending" ? "done" : "";
    const id = `e${i}`;
    parts.push(`<path id="${id}" class="edge ${cls}" d="M${cx},${y1} L${cx},${y2}"/>`);
    parts.push(`<path class="arrow ${live ? "live" : cls}" d="M${cx - 4.5},${y2 - 1} L${cx + 4.5},${y2 - 1} L${cx},${y2 + 6} Z"/>`);
    if (live) {
      const color = sb.status === "waiting" ? css("--wait") : css("--accent");
      parts.push(`<circle class="comet" r="3.2" fill="${color}"><animateMotion dur="${running ? 0.9 : 1.4}s" repeatCount="indefinite"><mpath href="#${id}"/></animateMotion></circle>`);
      parts.push(`<circle r="1.6" fill="#fff"><animateMotion dur="${running ? 0.9 : 1.4}s" repeatCount="indefinite"><mpath href="#${id}"/></animateMotion></circle>`);
    }
  }
  // 回退连线（虚线，右侧）
  const back = (from, to, label, lane, dy1 = 0) => {
    const a = NODES.find((n) => n.id === from), b = NODES.find((n) => n.id === to);
    const xr = NX + NW, x2 = xr + lane;
    const y1 = a.y + a.h / 2 + dy1, y2 = b.y + b.h / 2;
    parts.push(`<path class="edge back" d="M${xr},${y1} L${x2},${y1} L${x2},${y2} L${xr + 7},${y2}"/>`);
    parts.push(`<path class="arrow back" d="M${xr + 7},${y2 - 4} L${xr + 7},${y2 + 4} L${xr},${y2} Z"/>`);
    const ly = (y1 + y2) / 2;
    parts.push(`<text class="edge-label" x="${x2 + 3}" y="${ly}" transform="rotate(90 ${x2 + 3} ${ly})" text-anchor="middle">${esc(label)}</text>`);
  };
  // 三条回退线各走一条竖道，避免重叠；指向确认需求的两条在箭头处汇合 / each return edge has its own lane; the two into demand_confirm meet at the arrow
  // 从建模出发的一条上移 12，免得和进入建模的箭头共用同一段横线 / the edge leaving modeling starts 12 higher so it never shares the stub of the edge entering it
  back("design", "demand_confirm", t("back_demand"), 40);
  back("modeling", "demand_confirm", t("back_reach"), 26, -12);
  back("model_approve", "modeling", t("back_model"), 12);
  // 节点
  NODES.forEach((n, i) => {
    const s = steps[n.id];
    const cls = `node ${s.status} ${s.status === "active" && running ? "running" : ""}`;
    const r = n.gate ? n.h / 2 : 10;
    const x = n.gate ? NX + 26 : NX, w = n.gate ? NW - 52 : NW;
    let sub = nodeSub(s);
    const maxChars = n.gate ? 23 : 29;
    if (sub.length > maxChars) sub = sub.slice(0, maxChars - 1) + "…";
    parts.push(`<g class="${cls}">`);
    parts.push(`<rect class="glow" x="${x}" y="${n.y}" width="${w}" height="${n.h}" rx="${r}"/>`);
    parts.push(`<rect class="frame" x="${x}" y="${n.y}" width="${w}" height="${n.h}" rx="${r}"/>`);
    parts.push(`<rect class="chase" x="${x}" y="${n.y}" width="${w}" height="${n.h}" rx="${r}" pathLength="100"/>`);
    const icon = s.status === "done" ? "✓" : n.gate ? "◆" : String(i + 1);
    const tx = x + (n.gate ? 22 : 16);
    parts.push(`<text class="idx" x="${tx - 6}" y="${n.y + n.h / 2 - (sub ? 4 : -4)}" text-anchor="middle">${icon}</text>`);
    parts.push(`<text class="title" x="${tx + 6}" y="${n.y + n.h / 2 - (sub ? 4 : -4)}">${esc(t(n.id))}</text>`);
    if (sub) parts.push(`<text class="sub" x="${tx + 6}" y="${n.y + n.h / 2 + 13}">${esc(sub)}</text>`);
    if (["waiting", "blocked"].includes(s.status) || (s.status === "active" && running)) {
      const label = s.status === "active" ? t("running") : t(s.status);
      const bw = label.length * (lang === "zh" ? 11 : 6.4) + 12;
      parts.push(`<rect class="badge-bg ${s.status}" x="${x + w - bw - 6}" y="${n.y - 8}" width="${bw}" height="16" rx="8"/>`);
      parts.push(`<text class="badge badge-text" x="${x + w - bw / 2 - 6}" y="${n.y + 3.5}" text-anchor="middle">${esc(label)}</text>`);
    }
    parts.push(`</g>`);
  });
  svg.innerHTML = parts.join("");
  svg.setAttribute("viewBox", `0 0 300 ${NODES[NODES.length - 1].y + 70}`);
  const note = $("flow-note");
  note.innerHTML = state.blocking.length ? `<div class="q"><b>${esc(t("blocking"))}</b><br>${state.blocking.map(esc).join("<br>")}</div>` : "";
}

function renderTop() {
  document.title = `saos · ${state.name || JOB}`;
  $("job-name").textContent = state.job;
  const chip = $("mode-chip");
  const mode = state.daemon.running ? state.daemon.mode : null;
  chip.textContent = mode === "extension" ? t("explicit") : mode ? t("standalone") : t("no_daemon");
  chip.className = "chip" + (mode === "extension" ? " explicit" : "");
  const run = $("run-chip");
  if (state.progress) {
    run.hidden = false;
    const started = Date.parse(state.progress.started);
    const detail = state.progress.detail ? ` ${state.progress.detail}` : "";
    $("run-text").textContent = `${state.progress.op}${detail} · ${fmtDuration((Date.now() - started) / 1000)}`;
  } else run.hidden = true;
}

// ---------------- 面板与标签 ----------------

const explicit = () => state?.daemon.running && state.daemon.mode === "extension";
const vizRounds = () => (state?.rounds || []).filter((r) => r.viz);

function renderTabs() {
  const list = [];
  if (explicit()) list.push("control");
  list.push("layout", "spot", "mtf", "rounds");
  if (!tab || !list.includes(tab)) tab = list[0];
  $("tabs").innerHTML = list.map((k) => `<button class="tab ${k === tab ? "on" : ""}" data-tab="${k}" role="tab">${esc(t("tab_" + k))}</button>`).join("");
  const vr = vizRounds();
  const sel = $("round-select");
  const withLive = tab === "layout" && state?.live != null;
  sel.hidden = !["layout", "spot", "mtf"].includes(tab) || (!vr.length && !withLive);
  const opts = (withLive ? `<option value="live">${esc(t("live"))}</option>` : "") +
    vr.map((r) => `<option value="${r.round}">${t("select_round", r.round)} · G ${fmtG(r.shortfall)}</option>`).join("");
  if (sel.dataset.opts !== opts) { sel.innerHTML = opts; sel.dataset.opts = opts; }
  if (roundShown != null) sel.value = String(roundShown);
  $("dim-seg").hidden = tab !== "layout";
}

// 布局默认看当前镜头（后台进程每次改动镜头后重写 view.json）；点列图和 MTF 只有轮次有 / the layout follows the current lens by default
const wantsLive = () => tab === "layout" && state?.live != null && (pinnedRound === "live" || pinnedRound == null);

async function ensureViz() {
  if (wantsLive()) {
    if (roundShown === "live" && viz && liveSeen === state.live) return;
    try { viz = await api("live"); } catch (e) { viz = { ok: false, error: e.message, live: true }; }
    roundShown = "live"; liveSeen = state.live;
    return;
  }
  const vr = vizRounds();
  if (!vr.length) { viz = null; roundShown = null; return; }
  const want = typeof pinnedRound === "number" && vr.some((r) => r.round === pinnedRound) ? pinnedRound : vr[vr.length - 1].round;
  if (want === roundShown && viz) return;
  try { viz = await api("viz", { round: want }); } catch (e) { viz = { ok: false, error: e.message }; }
  roundShown = want;
}

async function renderView() {
  const view = $("view");
  if (tab === "control") return renderControl(view);
  if (tab === "rounds") return renderRounds(view);
  await ensureViz();
  renderTabs();
  if (!viz) { view.innerHTML = `<div class="empty">${esc(t(tab === "layout" ? "no_live" : "no_viz"))}</div>`; return; }
  if (!viz.ok) {
    const why = roundShown === "live" ? t("live_failed") + (viz.error || "") : viz.error?.includes?.("no viz.json") ? t("viz_missing") : t("viz_failed") + (viz.error || "");
    view.innerHTML = `<div class="empty">${esc(why)}</div>`; return;
  }
  if (tab === "spot") renderSpot(view);
  else if (tab === "mtf") renderMtf(view);
  else renderLayout(view);
}

// ---------------- 窗口控制（显性调试）----------------

async function renderControl(view) {
  const names = analyses || DEFAULT_ANALYSES;
  const li = lang === "zh" ? 0 : 1;
  view.innerHTML = `<div class="cards">
    <div class="card"><h3>${esc(t("viewer"))}</h3><p>${esc(t("viewer_p"))}</p>
      <div class="btn-grid">${[["cross-section", "v_cross"], ["3d", "v_3d"], ["shaded", "v_shaded"]].map(([v, k]) => `<button class="btn" data-viewer="${v}"><span>${esc(t(k))}</span><small>${v}</small></button>`).join("")}</div></div>
    <div class="card"><h3>${esc(t("analyses"))}</h3><p>${esc(t("analyses_p"))}</p>
      <div class="btn-grid">${names.map((a) => `<button class="btn" data-analysis="${a}"><span>${esc((ANALYSIS_NAMES[a] || [a, a])[li])}</span><small>${a}</small></button>`).join("")}</div></div>
    <div class="card"><h3>${esc(t("system"))}<button class="link" id="sys-refresh">${esc(t("refresh"))}</button></h3><div id="sys-body" class="foot">${esc(t("loading"))}</div></div>
  </div>`;
  view.querySelectorAll("[data-viewer],[data-analysis]").forEach((b) => b.addEventListener("click", () => windowAction(b)));
  $("sys-refresh").addEventListener("click", loadSystem);
  loadSystem();
  if (!analyses) api("analyses").then((r) => { analyses = r.analyses; if (tab === "control") renderControl(view); }).catch(() => {});
}

async function windowAction(btn) {
  const body = btn.dataset.viewer ? { viewer: btn.dataset.viewer } : { analysis: btn.dataset.analysis };
  btn.disabled = true;
  toast(t("queued"));
  try {
    await api("view", {}, body);
    toast(t("opened", btn.dataset.viewer || btn.dataset.analysis));
  } catch (e) { toast(`${t("failed")}: ${e.message}`, true); }
  btn.disabled = false;
}

async function loadSystem() {
  const el = $("sys-body");
  if (!el) return;
  el.className = "foot"; el.textContent = t("loading");
  try {
    const r = await api("system");
    const s = r.system;
    const unit = s.fields.type === "Angle" ? "°" : "";
    el.className = "";
    el.innerHTML = `<dl class="kv"><dt>${esc(t("aperture"))}</dt><dd>${esc(s.aperture.type)} = ${sig(s.aperture.value, 6)}</dd>
      <dt>${esc(t("units"))}</dt><dd>${esc(s.lens_units)}</dd><dt>${esc(t("stop"))}</dt><dd>${s.stop_surface}</dd></dl>
      <table><tr><th>#</th><th>${esc(t("fields"))} · ${esc(s.fields.type)}</th><th class="num">x</th><th class="num">y</th><th class="num">w</th></tr>
      ${s.fields.items.map((f, i) => `<tr><td>${i + 1}</td><td></td><td class="num">${sig(f.x, 6)}${unit}</td><td class="num">${sig(f.y, 6)}${unit}</td><td class="num">${sig(f.weight, 4)}</td></tr>`).join("")}</table>
      <table style="margin-top:8px"><tr><th>#</th><th>${esc(t("waves"))}</th><th class="num">µm</th><th class="num">w</th></tr>
      ${s.wavelengths.map((w, i) => `<tr><td>${i + 1}</td><td>${w.primary ? `<span class="tag ok">${esc(t("primary"))}</span>` : ""}</td><td class="num">${sig(w.um, 6)}</td><td class="num">${sig(w.weight, 4)}</td></tr>`).join("")}</table>`;
  } catch (e) { el.className = "foot warn"; el.textContent = e.message; }
}

// ---------------- 画布工具 ----------------

function canvasIn(el, w, h) {
  const c = document.createElement("canvas");
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  c.style.height = `${h}px`;
  el.appendChild(c);
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  return [c, g];
}

function waveColors(waves) {
  // 按波长排序取色：最短蓝、最长红（三个波长时为蓝、绿、红）
  const order = waves.map((w, i) => [w.um, i]).sort((a, b) => a[0] - b[0]);
  const out = new Array(waves.length);
  order.forEach(([, i], k) => {
    const hue = order.length === 1 ? 140 : 220 - (220 * k) / (order.length - 1);
    out[i] = `hsl(${hue}, 78%, 62%)`;
  });
  return out;
}

function fieldLabel(sys, f) {
  // 只有 y 视场时写 H y，否则写 (x, y)，免得 ±x、±y 四个视场同名
  const item = sys.fields.items[f];
  const angle = sys.fields.type === "Angle";
  const unit = angle ? "°" : sys.lens_units === "Millimeters" ? " mm" : "";
  if (sys.fields.items.every((it) => it.x === 0)) return `${angle ? "FoV" : "H"} ${sig(item.y)}${unit}`;
  return `(${sig(item.x)}, ${sig(item.y)})${unit}`;
}
const toUm = (sys, v) => (sys.lens_units === "Millimeters" ? v * 1000 : sys.lens_units === "Microns" ? v : sys.lens_units === "Inches" ? v * 25400 : v * 10000);

// ---------------- 点列图 ----------------

function renderSpot(view) {
  const sys = viz.system, spot = viz.spot;
  const colors = waveColors(sys.wavelengths);
  const cols = spot.fields.length <= 1 ? 1 : spot.fields.length <= 4 ? 2 : 3;
  // 每格至少 CELL 宽，视图放不下就横向滚动，而不是把点列图压扁
  const CELL = 220, GAP = 12;
  view.innerHTML = `<div class="figure" style="min-width:${cols * CELL + (cols - 1) * GAP + 30}px">
    <div class="fig-head"><div class="legend">${sys.wavelengths.map((w, i) => `<span><i style="background:${colors[i]}"></i>${Math.round(w.um * 1000)} nm</span>`).join("")}</div></div>
    <div class="spot-grid" style="grid-template-columns:repeat(${cols},minmax(${CELL}px,1fr))"></div></div>
    <div class="foot">${esc(t("spot_foot", spot.analysis_settings?.ReferTo || "—"))}</div>`;
  const grid = view.querySelector(".spot-grid");
  const pw = sys.primary_wave - 1;
  const width = (grid.clientWidth - (cols - 1) * GAP) / cols - 14;
  spot.fields.forEach((fd, f) => {
    const cell = document.createElement("div");
    cell.className = "spot-cell";
    const rms = spot.rms_radius?.[f]?.[pw], geo = spot.geo_radius?.[f]?.[pw];
    // Standard Spot 的 RMS/GEO 以 µm 计；波长设为全部时各波长返回同一个综合值（2024 R1 实测）
    cell.innerHTML = `<div class="cap"><b>${esc(fieldLabel(sys, f))}</b>${rms != null ? `<span>RMS ${sig(rms)} µm · GEO ${sig(geo)} µm</span>` : ""}</div>`;
    grid.appendChild(cell);
    const size = Math.min(width, 290);
    const [, g] = canvasIn(cell, width, size);
    let half = 0;
    fd.points.forEach((pts) => pts.forEach(([x, y]) => { half = Math.max(half, Math.abs(x), Math.abs(y)); }));
    half = half > 0 ? Number((half * 1.08).toPrecision(2)) : 0.001;
    const pad = { l: 46, r: 8, t: 4, b: 30 };
    const box = Math.min(width - pad.l - pad.r, size - pad.t - pad.b);
    const x0 = pad.l + (width - pad.l - pad.r - box) / 2, y0 = pad.t;
    g.fillStyle = css("--plot-bg"); g.fillRect(x0, y0, box, box);
    g.strokeStyle = css("--grid"); g.lineWidth = 1; g.strokeRect(x0 + 0.5, y0 + 0.5, box - 1, box - 1);
    const sx = (x) => x0 + box / 2 + (x / half) * (box / 2), sy = (y) => y0 + box / 2 - (y / half) * (box / 2);
    fd.points.forEach((pts, w) => {
      g.fillStyle = colors[w]; g.globalAlpha = 0.85;
      pts.forEach(([x, y]) => g.fillRect(sx(x) - 0.75, sy(y) - 0.75, 1.5, 1.5));
    });
    g.globalAlpha = 1;
    g.fillStyle = css("--text-2"); g.font = `11px ${css("--mono")}`;
    g.textAlign = "right"; g.textBaseline = "middle";
    [[half, y0 + 6], [0, y0 + box / 2], [-half, y0 + box - 6]].forEach(([v, y]) => g.fillText(sig(v, 2), x0 - 6, y));
    g.textAlign = "center"; g.textBaseline = "top";
    [[-half, x0 + 14], [0, x0 + box / 2], [half, x0 + box - 14]].forEach(([v, x]) => g.fillText(sig(v, 2), x, y0 + box + 5));
    g.fillStyle = css("--text-3"); g.fillText(sys.lens_units === "Millimeters" ? "mm" : sys.lens_units, x0 + box / 2, y0 + box + 17);
  });
}

// ---------------- MTF ----------------

function renderMtf(view) {
  const mtf = viz.mtf;
  if (!mtf || !mtf.series?.length) { view.innerHTML = `<div class="empty">${esc(t("viz_failed") + (viz.errors?.FftMtf || "FftMtf"))}</div>`; return; }
  const curves = [];
  let fi = 0;
  mtf.series.forEach((s) => {
    const isDiff = /diff/i.test(s.description);
    s = { ...s, description: s.description.replace(/^Field:\s*/, "") };
    const color = isDiff ? css("--text-2") : FIELD_COLORS[fi++ % FIELD_COLORS.length];
    Object.entries(s.y).forEach(([label, ys]) => {
      const diff = isDiff || /diff/i.test(label);
      curves.push({ x: s.x, y: ys, color: diff ? css("--text-2") : color, dash: diff ? [8, 3, 2, 3] : /sag/i.test(label) ? [6, 4] : [], name: s.description });
    });
  });
  const named = mtf.series.filter((s) => !/diff/i.test(s.description)).map((s) => ({ ...s, description: s.description.replace(/^Field:\s*/, "") }));
  const settings = Object.entries(mtf.settings || {}).filter(([, v]) => v != null).map(([k, v]) => `${k} ${v}`).join(" · ");
  view.innerHTML = `<div class="figure plot"><div class="fig-head"><div class="legend">${named.map((s, i) => `<span><i style="background:${FIELD_COLORS[i % FIELD_COLORS.length]}"></i>${esc(s.description)}</span>`).join("")}
    <span><i class="line"></i>${esc(t("tangential"))}</span><span><i class="line dash"></i>${esc(t("sagittal"))}</span></div></div><div id="mtf-plot"></div></div>
    <div class="foot">${esc(t("mtf_foot"))} ${esc(settings)}</div>`;
  const host = $("mtf-plot");
  const head = view.querySelector(".fig-head").offsetHeight;
  const W = host.clientWidth, H = Math.max(300, Math.min(520, view.clientHeight - head - 90));
  const [, g] = canvasIn(host, W, H);
  const pad = { l: 46, r: 14, t: 10, b: 38 };
  const xmax = Math.max(...curves.flatMap((c) => c.x));
  const px = (x) => pad.l + (x / xmax) * (W - pad.l - pad.r), py = (y) => pad.t + (1 - y) * (H - pad.t - pad.b);
  g.fillStyle = css("--plot-bg"); g.fillRect(pad.l, pad.t, W - pad.l - pad.r, H - pad.t - pad.b);
  g.strokeStyle = css("--grid"); g.lineWidth = 1;
  g.fillStyle = css("--text-2"); g.font = `11px ${css("--mono")}`;
  for (let k = 0; k <= 5; k++) {
    const y = py(k / 5);
    g.beginPath(); g.moveTo(pad.l, y + 0.5); g.lineTo(W - pad.r, y + 0.5); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle"; g.fillText((k / 5).toFixed(1), pad.l - 6, y);
  }
  const step = niceStep(xmax / 5);
  for (let x = 0; x <= xmax + 1e-9; x += step) {
    const X = px(x);
    g.beginPath(); g.moveTo(X + 0.5, pad.t); g.lineTo(X + 0.5, H - pad.b); g.stroke();
    g.textAlign = "center"; g.textBaseline = "top"; g.fillText(sig(x, 3), X, H - pad.b + 5);
  }
  g.fillStyle = css("--text-3"); g.fillText(mtf.series[0].x_label || "cycles/mm", pad.l + (W - pad.l - pad.r) / 2, H - pad.b + 20);
  curves.forEach((c) => {
    g.strokeStyle = c.color; g.lineWidth = 1.6; g.setLineDash(c.dash); g.beginPath();
    let pen = false;
    c.x.forEach((x, i) => {
      const y = c.y[i];
      if (y == null) { pen = false; return; }
      pen ? g.lineTo(px(x), py(y)) : g.moveTo(px(x), py(y));
      pen = true;
    });
    g.stroke();
  });
  g.setLineDash([]);
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}

// ---------------- 布局（2D / 3D）----------------

const CAMERA0 = { yaw: -0.62, pitch: 0.32, zoom: 1, panX: 0, panY: 0 };
const camera = { ...CAMERA0 };

function elements(surfaces) {
  // 相邻两面之间是玻璃（不是空气、也不是反射镜）就组成一个镜片；胶合面同时属于前后两片
  const out = [];
  for (let i = 0; i < surfaces.length - 1; i++) {
    const s = surfaces[i], n = surfaces[i + 1];
    const m = (s.material || "").toUpperCase();
    if (m && m !== "MIRROR" && s.rim && n.rim) out.push({ front: s, back: n, material: s.material });
  }
  return out;
}

function clashOf(v) {
  const c = v.interference || {};
  const lines = [...(c.crossings || []).map((x) => t("crossing", x.surfaces[0], x.surfaces[1], sig(x.depth, 2))),
    ...(c.overlaps || []).map((x) => t("overlap", x.lenses[0].join("–"), x.lenses[1].join("–"), x.points))];
  const hit = new Set([...(c.crossings || []).flatMap((x) => x.surfaces), ...(c.overlaps || []).flatMap((x) => x.lenses.flat())]);
  return { lines, hit };
}

function lensDataTable(rows) {
  const num = (v) => (v == null ? "" : !isFinite(v) ? "∞" : sig(v, 6));
  const mark = (s, k) => { const v = (s.solves || {})[k]; return !v || ["Fixed", "Automatic", "None"].includes(v) ? "" : v === "Variable" ? " V" : ` ${v[0]}`; };
  return `<details class="lens-data" ${lensDataOpen ? "open" : ""}><summary>${esc(t("lens_data"))}</summary>
    <div class="table-wrap"><table><tr><th>#</th><th>${esc(t("ld_type"))}</th><th>${esc(t("ld_comment"))}</th><th class="num">${esc(t("ld_radius"))}</th>
    <th class="num">${esc(t("ld_thickness"))}</th><th>${esc(t("ld_material"))}</th><th class="num">${esc(t("ld_sd"))}</th><th class="num">${esc(t("ld_conic"))}</th></tr>
    ${rows.map((s) => `<tr><td>${s.index}${s.is_stop ? " STO" : ""}</td><td>${esc(s.type)}</td><td>${esc(s.comment)}</td>
      <td class="num">${num(s.radius)}${mark(s, "radius")}</td><td class="num">${num(s.thickness)}${mark(s, "thickness")}</td>
      <td>${esc(s.material)}${mark(s, "material")}</td><td class="num">${num(s.semi_diameter)}${mark(s, "semi_diameter")}</td>
      <td class="num">${num(s.conic)}${mark(s, "conic")}</td></tr>`).join("")}</table></div>
    <div class="foot">${esc(t("ld_solve"))}</div></details>`;
}

function renderLayout(view) {
  const sys = viz.system;
  const surfs = viz.surfaces;
  // 非轴对称系统画不出有意义的剖面，只给 3D / a non-axial system has no meaningful section: 3D only
  const only3d = !!sys.non_axial;
  if (only3d) dim = "3d";
  $("dim-seg").querySelectorAll("button").forEach((x) => { x.classList.toggle("on", x.dataset.dim === dim); x.disabled = only3d && x.dataset.dim === "2d"; x.title = x.disabled ? t("only_3d") : ""; });
  const clash = clashOf(viz);
  const title = roundShown === "live" ? `${t("live")} · ${viz.lens || ""}${viz.dirty ? ` · ${t("live_unsaved")}` : ""}` : t("select_round", roundShown);
  const els = elements(surfs);
  const mats = [...new Set(els.map((e) => e.material.toLowerCase()))];
  const matColor = (m) => MATERIAL_COLORS[mats.indexOf(m.toLowerCase()) % MATERIAL_COLORS.length];
  const image = surfs[surfs.length - 1];
  const stopSd = sys.stop_semi_diameter;
  const unit = sys.lens_units === "Millimeters" ? "mm" : sys.lens_units;
  // 图例放在画布外，不再盖住光路；画布有最小尺寸，视图放不下时上下、左右滚动
  view.innerHTML = `<div class="layout-view"><div class="figure">
    <div class="fig-head"><span class="info">λ ${sig(sys.wavelengths[sys.primary_wave - 1].um, 4)} µm${stopSd ? ` · STOP Ø ${(2 * stopSd).toFixed(2)} ${unit}` : ""}${image?.vertex ? ` · ${esc(t("image_z"))} ${image.vertex[2].toFixed(2)} ${unit}` : ""}</span>
      <span class="fig-title">${esc(title)}</span></div>
    ${clash.lines.length ? `<div class="clash"><b>${esc(t("clash_title"))}</b>${clash.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>` : ""}
    <div class="legend">${sys.fields.items.map((f, i) => `<span><i class="line" style="border-color:${FIELD_COLORS[i % FIELD_COLORS.length]}"></i>${esc(fieldLabel(sys, i))}</span>`).join("")}</div>
    <div class="stage"><canvas></canvas><span class="zoom-chip"></span></div>
    <div class="legend" style="margin:8px 0 0">${mats.map((m) => `<span><span class="swatch" style="background:${matColor(m)}55;border-color:${matColor(m)}"></span>${esc(m)}</span>`).join("")}</div></div>
    <div class="foot ${viz.errors?.sag ? "warn" : ""}">${esc(viz.errors?.sag ? t("sag_flat") + " " : "")}${esc(t("layout_foot"))} <span id="layout-hint"></span></div>
    ${viz.lens_data ? lensDataTable(viz.lens_data) : ""}</div>`;
  const details = view.querySelector(".lens-data");
  if (details) details.addEventListener("toggle", () => { lensDataOpen = details.open; });
  const stage = view.querySelector(".stage");
  const canvas = stage.querySelector("canvas");
  const zoomChip = stage.querySelector(".zoom-chip");

  // 所有点用于取景；起点按第一面方向往回延长一段
  const verts = surfs.filter((s) => s.vertex).map((s) => s.vertex);
  const track = verts.length > 1 ? Math.hypot(...verts[verts.length - 1].map((v, k) => v - verts[0][k])) : 10;
  const rays = viz.layout.rays.map((r) => {
    const pts = r.points.slice();
    if (r.start_dir) pts.unshift(pts[0].map((v, k) => v - r.start_dir[k] * track * 0.28));
    return { ...r, pts };
  });
  const all = [...surfs.flatMap((s) => s.rim || []), ...rays.flatMap((r) => r.pts)];
  const lo = [0, 1, 2].map((k) => Math.min(...all.map((p) => p[k]))), hi = [0, 1, 2].map((k) => Math.max(...all.map((p) => p[k])));
  const center = lo.map((v, k) => (v + hi[k]) / 2);

  function project(p, yaw, pitch) {
    // 基准视图：z 向右、y 向上、x 朝向观察者；先绕 y 轴转 yaw，再绕屏幕横轴转 pitch（正交投影）
    const u = p[2] - center[2], v = p[1] - center[1], w = p[0] - center[0];
    const u1 = u * Math.cos(yaw) + w * Math.sin(yaw), w1 = -u * Math.sin(yaw) + w * Math.cos(yaw);
    const v2 = v * Math.cos(pitch) - w1 * Math.sin(pitch);
    return [u1, v2];
  }

  function draw() {
    const W = stage.clientWidth, H = stage.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    const yaw = dim === "2d" ? 0 : camera.yaw, pitch = dim === "2d" ? 0 : camera.pitch;
    const P = all.map((p) => project(p, yaw, pitch));
    const spanU = Math.max(...P.map((p) => p[0])) - Math.min(...P.map((p) => p[0])) || 1;
    const spanV = Math.max(...P.map((p) => p[1])) - Math.min(...P.map((p) => p[1])) || 1;
    const scale = Math.min((W - 80) / spanU, (H - 60) / spanV) * camera.zoom;
    const midU = (Math.max(...P.map((p) => p[0])) + Math.min(...P.map((p) => p[0]))) / 2;
    const midV = (Math.max(...P.map((p) => p[1])) + Math.min(...P.map((p) => p[1]))) / 2;
    const S = (p) => { const [u, v] = project(p, yaw, pitch); return [W / 2 + camera.panX + (u - midU) * scale, H / 2 + camera.panY - (v - midV) * scale]; };
    $("layout-hint").textContent = t(dim === "2d" ? "layout_hint_2d" : "layout_hint_3d");
    zoomChip.textContent = camera.zoom === 1 ? "" : `${Math.round(camera.zoom * 100)} %`;
    const placed = [];
    const line = (pts, close = false) => { g.beginPath(); pts.forEach((p, i) => { const [x, y] = S(p); i ? g.lineTo(x, y) : g.moveTo(x, y); }); if (close) g.closePath(); };

    // 光轴
    if (verts.length > 1) {
      g.strokeStyle = css("--text-3"); g.setLineDash([6, 4]); g.lineWidth = 0.8;
      line([verts[0].map((v, k) => v - (verts[verts.length - 1][k] - verts[0][k]) * 0.3), verts[verts.length - 1].map((v, k) => v + (verts[verts.length - 1][k] - verts[0][k]) * 0.06)]);
      g.stroke(); g.setLineDash([]);
    }
    // 像面
    if (image?.rim) {
      g.strokeStyle = "#e0705f"; g.fillStyle = "#e0705f22"; g.lineWidth = 1.4;
      if (dim === "2d") { line(image.profile_y); g.stroke(); }
      else {
        const c = image.vertex, r = image.rim;
        const a = r[0].map((v, k) => v - c[k]), b = r[8].map((v, k) => v - c[k]);
        const sq = [[1, 1], [-1, 1], [-1, -1], [1, -1]].map(([i, j]) => c.map((v, k) => v + i * a[k] + j * b[k]));
        line(sq, true); g.fill(); g.stroke();
      }
    }
    // 光阑等空气中的孔径：画成两段挡光片
    surfs.forEach((s) => {
      if (!s.is_stop || !s.profile_y || s === image) return;
      const top = s.profile_y[s.profile_y.length - 1], bot = s.profile_y[0];
      const out = (p, sgn) => p.map((v, k) => (k === 1 ? v + sgn * s.semi_diameter * 0.35 : v));
      g.strokeStyle = css("--text-2"); g.lineWidth = 2;
      line([top, out(top, 1)]); g.stroke(); line([bot, out(bot, -1)]); g.stroke();
    });
    // 光线（2D 只画子午扇）
    g.lineWidth = 1;
    rays.forEach((r) => {
      if (dim === "2d" && r.px !== 0) return;
      g.strokeStyle = FIELD_COLORS[r.field % FIELD_COLORS.length]; g.globalAlpha = 0.8;
      line(r.pts); g.stroke();
    });
    g.globalAlpha = 1;
    // 镜片
    els.forEach((e, k) => {
      const col = matColor(e.material);
      g.strokeStyle = col; g.fillStyle = col + "33"; g.lineWidth = 1.3;
      if (dim === "2d") {
        line([...e.front.profile_y, ...e.back.profile_y.slice().reverse()], true); g.fill(); g.stroke();
      } else {
        line(e.front.rim, true); g.fill(); g.stroke();
        line(e.back.rim, true); g.fill(); g.stroke();
        [0, 8, 16, 24].forEach((j) => { line([e.front.rim[j], e.back.rim[j]]); g.stroke(); });
        g.globalAlpha = 0.55; g.lineWidth = 0.9;
        [e.front, e.back].forEach((s) => { line(s.profile_y); g.stroke(); line(s.profile_x); g.stroke(); });
        g.globalAlpha = 1;
      }
      // 标签 L1, L2 …（胶合件按片计）
      const topF = e.front.profile_y[e.front.profile_y.length - 1], topB = e.back.profile_y[e.back.profile_y.length - 1];
      const [x1, y1] = S(topF), [x2, y2] = S(topB);
      // 挤在一起的镜片只标不重叠的标签，免得 L3–L6 叠成一团；标签最后统一画，不被后面镜片的填充盖住
      const lx = (x1 + x2) / 2, ly = Math.min(y1, y2) - 8, box = [lx - 11, ly - 14, lx + 11, ly];
      if (placed.some((b) => b.box[0] < box[2] && box[0] < b.box[2] && b.box[1] < box[3] && box[1] < b.box[3])) return;
      placed.push({ box, text: `L${k + 1}`, x: lx, y: ly });
    });
    g.font = `600 12px ${css("--font")}`; g.textAlign = "center"; g.textBaseline = "bottom";
    g.lineWidth = 3; g.lineJoin = "round"; g.strokeStyle = css("--plot-bg"); g.fillStyle = css("--text-2");
    placed.forEach((p) => { g.strokeText(p.text, p.x, p.y); g.fillText(p.text, p.x, p.y); });
    // 交叠的面描红 / surfaces involved in interference drawn red
    if (clash.hit.size) {
      g.strokeStyle = "#e5484d"; g.lineWidth = 2.2;
      surfs.forEach((s) => {
        if (!clash.hit.has(s.index) || !s.profile_y) return;
        if (dim === "2d") { line(s.profile_y); g.stroke(); }
        else { line(s.rim, true); g.stroke(); line(s.profile_y); g.stroke(); }
      });
    }
    // 反射镜
    surfs.forEach((s) => {
      if ((s.material || "").toUpperCase() !== "MIRROR" || !s.profile_y) return;
      g.strokeStyle = "#c8ccd4"; g.lineWidth = 2.4; line(s.profile_y); g.stroke();
      if (dim === "3d" && s.rim) { g.lineWidth = 1; line(s.rim, true); g.stroke(); }
    });
    // 坐标轴小图标
    if (dim === "3d") {
      const ox = W - 46, oy = H - 44, L = 22;
      [["x", [1, 0, 0]], ["y", [0, 1, 0]], ["z", [0, 0, 1]]].forEach(([name, d]) => {
        const q = project(d.map((v, k) => v + center[k]), yaw, pitch);
        g.strokeStyle = css("--text-3"); g.lineWidth = 1; g.beginPath(); g.moveTo(ox, oy); g.lineTo(ox + q[0] * L, oy - q[1] * L); g.stroke();
        g.fillStyle = css("--text-3"); g.font = `11px ${css("--mono")}`; g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText(name, ox + q[0] * (L + 9), oy - q[1] * (L + 9));
      });
      g.beginPath(); g.arc(ox, oy, 2.5, 0, 2 * Math.PI); g.fill();
    }
  }
  draw();
  let drag = null;
  canvas.oncontextmenu = (e) => e.preventDefault();
  canvas.onpointerdown = (e) => {
    // 2D 只能平移；3D 左键旋转，Shift 或右键平移
    drag = { x: e.clientX, y: e.clientY, pan: dim === "2d" || e.shiftKey || e.button === 2 };
    canvas.classList.toggle("panning", drag.pan);
    canvas.setPointerCapture(e.pointerId);
  };
  canvas.onpointermove = (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (drag.pan) { camera.panX += dx; camera.panY += dy; }
    else {
      camera.yaw += dx * 0.008;
      camera.pitch = Math.max(-1.45, Math.min(1.45, camera.pitch + dy * 0.008));
    }
    drag.x = e.clientX; drag.y = e.clientY;
    draw();
  };
  canvas.onpointerup = () => { drag = null; canvas.classList.remove("panning"); };
  canvas.onwheel = (e) => { e.preventDefault(); camera.zoom = Math.max(0.3, Math.min(12, camera.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12))); draw(); };
  canvas.ondblclick = () => { Object.assign(camera, CAMERA0); draw(); };
  renderLayout.redraw = draw;
}

// ---------------- 轮次 ----------------

function renderRounds(view) {
  const rounds = state.rounds;
  if (!rounds.length) { view.innerHTML = `<div class="empty">${esc(t("no_rounds"))}</div>`; return; }
  view.innerHTML = `<div class="table-wrap"><div class="figure"><div id="g-plot"></div></div><div class="foot">${esc(t("pick_round"))}</div>
    <table><tr><th>${esc(t("round"))}</th><th>${esc(t("tiers"))}</th><th class="num">${esc(t("gap"))}</th><th>${esc(t("state"))}</th><th>${esc(t("note"))}</th></tr>
    ${rounds.slice().reverse().map((r) => `<tr class="${r.viz ? "pick" : ""} ${r.round === roundShown ? "sel" : ""}" data-round="${r.round}">
      <td class="num" style="text-align:left">${t("select_round", r.round)}</td><td>${r.tiers.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</td>
      <td class="num">${fmtG(r.shortfall)}</td><td>${r.achieved ? `<span class="tag ok">${esc(t("achieved"))}</span>` : ""}${r.deliverable ? `<span class="tag">${esc(t("deliverable"))}</span>` : ""}</td>
      <td>${esc(r.note)}</td></tr>`).join("")}</table></div>`;
  view.querySelectorAll("tr.pick").forEach((tr) => tr.addEventListener("click", () => {
    pinnedRound = Number(tr.dataset.round);
    tab = "layout";
    renderTabs(); renderView();
  }));
  const host = $("g-plot");
  const W = host.clientWidth, H = 180;
  const [, g] = canvasIn(host, W, H);
  const pad = { l: 46, r: 14, t: 10, b: 26 };
  const finite = rounds.filter((r) => r.shortfall != null);
  if (!finite.length) return;
  const ys = finite.map((r) => r.shortfall);
  const positive = ys.filter((v) => v > 0);
  const logScale = positive.length && Math.max(...positive) / Math.min(...positive) > 100;
  const fy = (v) => (logScale ? Math.log10(Math.max(v, Math.min(...positive) / 2)) : v);
  const lo = logScale ? fy(Math.min(...positive) / 2) : 0, hi = Math.max(...ys.map(fy)) || 1;
  const n = rounds[rounds.length - 1].round || 1;
  const px = (k) => pad.l + (k / n) * (W - pad.l - pad.r), py = (v) => pad.t + (1 - (fy(v) - lo) / (hi - lo || 1)) * (H - pad.t - pad.b);
  g.fillStyle = css("--plot-bg"); g.fillRect(pad.l, pad.t, W - pad.l - pad.r, H - pad.t - pad.b);
  g.strokeStyle = css("--grid"); g.strokeRect(pad.l + 0.5, pad.t + 0.5, W - pad.l - pad.r - 1, H - pad.t - pad.b - 1);
  g.strokeStyle = css("--accent"); g.lineWidth = 1.5; g.beginPath();
  finite.forEach((r, i) => (i ? g.lineTo(px(r.round), py(r.shortfall)) : g.moveTo(px(r.round), py(r.shortfall))));
  g.stroke();
  finite.forEach((r) => {
    g.fillStyle = r.achieved ? css("--done") : css("--accent");
    g.beginPath(); g.arc(px(r.round), py(r.shortfall), r.round === roundShown ? 4.5 : 3, 0, 2 * Math.PI); g.fill();
  });
  g.fillStyle = css("--text-2"); g.font = `11px ${css("--mono")}`; g.textAlign = "right"; g.textBaseline = "middle";
  g.fillText(fmtG(Math.max(...ys)), pad.l - 6, pad.t + 6);
  g.fillText(logScale ? fmtG(Math.min(...positive) / 2) : "0", pad.l - 6, H - pad.b - 4);
  g.textAlign = "center"; g.textBaseline = "top";
  g.fillText("r000", px(0), H - pad.b + 5); g.fillText(t("select_round", n), px(n), H - pad.b + 5);
}

// ---------------- 轮询与事件 ----------------

async function poll() {
  try {
    const next = await api("state");
    const text = JSON.stringify(next);
    if (text !== stateText) {
      const roundsChanged = !state || JSON.stringify(state.rounds) !== JSON.stringify(next.rounds);
      const modeChanged = !state || state.daemon.mode !== next.daemon.mode || state.daemon.running !== next.daemon.running;
      state = next; stateText = text;
      lang = next.lang === "en" ? "en" : "zh";
      document.documentElement.lang = lang;
      renderFlow();
      renderTabs();
      const liveChanged = tab === "layout" && roundShown === "live" && next.live !== liveSeen;
      if ((roundsChanged || liveChanged) && tab !== "control") renderView();
      if (modeChanged) { analyses = null; renderView(); }
    }
    renderTop();
  } catch (e) {
    $("mode-chip").textContent = e.message;
  }
  setTimeout(poll, POLL_MS);
}

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b || b.dataset.tab === tab) return;
  tab = b.dataset.tab;
  renderTabs(); renderView();
});
$("round-select").addEventListener("change", (e) => { pinnedRound = e.target.value === "live" ? "live" : Number(e.target.value); renderView(); });
$("dim-seg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-dim]");
  if (!b || b.disabled) return;
  dim = b.dataset.dim;
  $("dim-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  if (tab === "layout" && renderLayout.redraw) renderLayout.redraw();
});
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state && tab !== "control") renderView(); }, 150);
});

$("dim-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.dim === dim));
if (!JOB) document.body.innerHTML = `<div class="empty">?job= missing</div>`;
else poll();
