"use strict";
// saos ui：流程图 + 右侧面板。数据全部来自本机服务（任务目录与每轮的 viz.json），这里只负责画。

const Q = new URLSearchParams(location.search);
const JOB = Q.get("job");
const TOKEN = Q.get("t") || "";
const POLL_MS = 1500;

const TEXT = {
  zh: {
    interview: "访谈与可行性判断", demand_confirm: "确认需求单", modeling: "起型、探索与建模", model_approve: "确认建模说明", design: "自主设计", deliver: "交付报告",
    back_reject: "调整设计", legacy_job: "历史任务：请新建任务并重测",
    no_demand: "访谈中，尚无需求单", invalid_demand: "需求单未通过校验", first: "等待用户确认", reconfirm: "需求已改动，待重新确认",
    waiting: "等你确认", blocked: "待回答", active: "进行中", running: "运行中",
    rounds_n: (n) => `第 ${n} 轮`, no_rounds: "尚未记录轮次", rtc_unclosed: "rtc 结果待 OpticStudio 收尾优化",
    confirm_pending: (n) => `第 ${n} 轮在 rtc 上算，待 OpticStudio 复测：saos round --confirm`, confirm_failed: (n) => `第 ${n} 轮 OpticStudio 复测结论与 rtc 不同，回到设计`,
    tag_rtc: "rtc", tag_confirmed: "OpticStudio 复测", tag_disagrees: "复测不符",
    starts_done: (n, k) => `已生成 ${n} 个起点 · ${k} 个未评估`,
    explore_run: (n) => `结构探索：${n} 个候选运行中`, explore_done: (n) => `已比较 ${n} 个候选`,
    st_achieved: "已达成", st_exhausted: "无法再优化", st_limit: "达到轮次上限", st_stopped: "按用户决定停止",
    report_ready: "报告已生成", report_todo: "待生成报告", deliver_cmd: "saos scan（公差，按需）· saos report",
    back_demand: "需求变更", back_model: "结构或计算前提改变", back_reach: "结构不可达", feasibility: "质疑结构 · 核算界限", feasibility_rtc: "rtc 核算 · 不占许可", feasibility_written: "可行性记录已写",
    standalone: "后台运行 · standalone", rtc_instance: "rtc 实例 · 交付之前各阶段，不占许可", no_daemon: "后台进程未运行",
    rtc_dual: (started) => `rtc 计算 · OpticStudio ${started ? "待命" : "未启动"}`,
    rtc_switch: "后台进程只有 rtc（daemon start --backend rtc）；建模说明页面和交付需要 OpticStudio：存盘后 saos daemon stop，再 saos daemon start。",
    tab_layout: "布局", tab_spot: "点列图", tab_mtf: "MTF", tab_rounds: "轮次",
    blocking: "blocking 待确认问题：",
    loading: "读取中…", failed: "失败",
    no_viz: "第 0 轮记录后，这里显示该轮的布局、点列图和 MTF。", viz_missing: "这一轮没有可视化数据（用 --no-viz 记录，或记录早于 saos ui）。",
    viz_failed: "这一轮的可视化导出失败：", spot_foot: (ref) => `点：批量光线追迹，相对主波长主光线，未计被拦光线。RMS / GEO：OpticStudio Standard Spot（参考 ${ref}，全部波长），单位 µm。Standard Spot 每个波长追的光线数与波长权重成正比，权重小于 1 的波长常漏掉边缘光线，所以 GEO 可能小于图中最远的点。`,
    spot_foot_rtc: () => "点：rtc 批量光线追迹，相对主波长主光线，未计被拦光线。RMS / GEO：同一批点按波长权重合算，单位 µm；未与 OpticStudio Standard Spot 对账。",
    fan_foot: "光线扇：rtc 批量光线追迹，子午扇为 ey 对 Py、弧矢扇为 ex 对 Px，相对主波长主光线；追不到像面的点断开。",
    mtf_foot_rtc: "曲线：rtc FFT MTF（64×64 光瞳采样，各波长以自己的主光线为参考），未与 OpticStudio 对账。",
    mtf_foot: "曲线：OpticStudio FFT MTF 导出。", mtf_partial: "全部视场一起算不出，改为逐个视场算：", tangential: "子午 T", sagittal: "弧矢 S", diff: "衍射极限",
    layout_foot: "几何与光线坐标：ZOS-API（GetGlobalMatrix、GetSag、批量光线追迹）。",
    layout_foot_rtc: "几何与光线坐标：rtc（矢高、实际光线追迹；物面以外的半口径与求解按 OpticStudio 的规则在 rtc 里重算）。交付前在 OpticStudio 上复测。",
    unverified: (items) => `未与 OpticStudio 对账：${items.join("；")}。`,
    model_evidence: "模型确认记录提示：",
    redescribe: "结构已变：重新描述并确认",
    modeling_sub: "rtc 上起型 · 初筛 · 建模说明", approval_sub: "方向 · 取舍 · 未知项", design_sub: "rtc 上优化 · 到期复核 · round",
    layout_hint_3d: "拖动旋转 · Shift 或右键拖动平移 · 滚轮以光标为中心缩放 · 双击复位 · 指向某面看参数", layout_hint_2d: "拖动平移 · Shift 或右键拖动测距（吸附面上的点）· 滚轮以光标为中心缩放 · 双击复位 · Esc 清除测距 · 指向某面看参数",
    image_z: "像面 z", round: "轮", tiers: "层级", gap: "目标差距", state: "状态", note: "说明", achieved: "达成", deliverable: "可交付",
    pick_round: "点一行在其他视图中查看该轮", sag_flat: "部分面读不到面形，按平面画。", select_round: (n) => `r${String(n).padStart(3, "0")}`,
    live: "当前镜头", live_unsaved: "未保存", no_live: "后台进程加载或改动镜头后，这里显示当前镜头的布局。", live_failed: "当前视图导出失败：",
    only_3d: "各面不在同一个对称平面里，只有 3D 视图", disconnected: "与 saos ui 服务断开，正在重连…", notice_close: "知道了", lens_data: "Lens Data", ld_type: "面型", ld_comment: "注释", ld_radius: "半径", ld_thickness: "厚度",
    gate_demand: "确认需求单", gate_model: "确认建模说明", gate_weak: "依据薄弱的界限", gate_missing: (ids) => `这些条目还没有来源，不能确认：${ids}`,
    gate_open: "打开说明页", gate_rebuild: (r) => `重建原因：${r}`, gate_no_rebuild: "结构与上次确认不同，说明里还没写重建原因：请 agent 补上后重新出说明",
    gate_no_search: "说明里没有起型检索记录：请 agent 补上后重新出说明", gate_note: "备注（可不填）", gate_done: "已记录你的确认", gate_old: "这份说明是旧格式，窗口里不能确认：请 agent 重新出说明",
    gate_revise: "要求修改", gate_revise_need: "先在备注里写要改什么", gate_revised: (n) => `已要求修改，等 agent 出新版：${n}`, gate_revise_done: "修改意见已记下",
    mode_auto: "自主运行：agent 自行通过关卡并记录理由，报告标明未经你确认", mode_to_interactive: "改为由我确认", mode_switched: "已改为参与模式：之后的关卡由你确认",
    ld_material: "材料", ld_sd: "半口径", ld_conic: "圆锥", ld_solve: "V 为变量，其他字母为求解或 pickup",
    clash_title: "结构交叠", crossing: (a, b, d) => `第 ${a} 面与第 ${b} 面在口径内相交（最深约 ${d}）`,
    overlap: (a, b, n) => `镜片 ${a} 与镜片 ${b} 相互穿插（${n} 个采样点落在另一片玻璃里）`,
    live_optimising: "优化中，光线在结束后更新", opt_grid: "网格", opt_labels: "面号", png_title: "把当前画面存成 PNG", tip_air: "空气",
    ruler: (d, dz, dy, u) => `${d} ${u}（Δz ${dz} · Δy ${dy}）`,
    tip_lens: (k, m, ct, et, u) => `L${k} ${m} · 中心厚 ${ct} · 边缘厚 ${et} ${u}`,
    tab_detectors: "探测器", nsc_title: "非序列系统", nsc_foot: "物体轮廓按非序列编辑器的参数画（ZOS-API GetMatrix 给出全局位置）；画不出轮廓的类型只列在表里。2D 为全局 YZ 截面。",
    nsc_undrawn: "画不出轮廓的物体：", nsc_overlap: (a, b, d) => `第 ${a} 个物体与第 ${b} 个物体部分重叠（深约 ${d}），又没有嵌套关系`,
    th_obj: ["#", "类型", "注释", "材料", "体散射", "面属性"], face_is: { reflective: "反射", absorbing: "吸收" }, face_word: "面", kind_source: "光源", kind_detector: "探测器",
    det_none: "这一轮没有探测器读数。", det_note: "读数是这一轮判定最后一次追迹留在内存里的结果；判定没有追迹时为空。图为照度（W/cm²），每个像素一格。",
    det_power: "功率", det_hits: "命中光线", det_pixels: "像素", det_peak: "最大照度",
    tab_fan: "光线扇图", tab_field: "场曲 / 畸变", tab_starts: "起点",
    gap: (name, b) => `这一轮的 ${b || "后端"} 导出里没有${name}。缺口如实显示，界面不另行计算。`,
    fan_t: "子午 ey(py)", fan_s: "弧矢 ex(px)", pupil: "归一化光瞳",
    curv: "场曲（离像面距离）", dist: "畸变 %", field_axis: "视场",
    starts_none: "还没有 starts.generate 的记录。生成起点后，这里列出来源、缩放、超范围项、一阶与三阶预判和初筛结果。",
    st_form: "形式", st_source: "来源", st_scale: "缩放", st_tight: "最吃紧", st_extra: "超出来源示例", st_efl: "EFL", st_bfl: "BFL", st_fno: "f/#",
    st_petz: "Petzval R/f", st_seidel: "三阶像差合计（waves）", st_aoi: "最大入射角", st_excluded: "未入选的形式", st_noteval: "未评估（材料/计算）",
    st_screen: "零优化初筛", st_short: (s) => `同预算短优化 · ${s} s`, st_running: "运行中", st_verdict: { valid: "有效", invalid: "预算内无效", material: "未评估：材料", computation: "未评估：计算" },
    st_g: "G 起点 → 终点", st_warn: "数值警告", st_page: "比较页", st_foot: "只读 explore/starts 与 scans 中已有文件；预判由 OpticStudio 的操作数和原生分析给出，不是优化结论。",
    hover_row: "指向表格一行，布局中高亮对应的面",
    tab_metrics: "指标", metrics_none: "需求单里还没有带判定方法的条目。",
    metric_missing: "需求单里已没有这一条", metric_bound: "界限", metric_no_value: "无有效值", merit: "评价函数",
    metric_stale: "镜头改动之前的值", metric_slow: "单项超过自动测量的时限，取 saos check 或 round 的值", metric_pending: "等待测量",
    metric_src: { live: "自动", check: "check", round: "轮次" },
    metrics_foot: "“当前镜头”一行由后台进程在每次改动镜头后自动测量（每次限时 SAOS_LIVE_S 秒，默认 3 秒；超时的项取 saos check 或 round 的值），灰色为镜头改动之前的值；其余行是每轮账本记录的测量值，虚线为需求单的界限。saos ui open --metrics 可以改选和排序条目。",
    lens: "镜头", judge: "判定", tl_title: "轮次", tl_rounds: (n) => `已记录 ${n} 轮`, tl_none: "还没有轮次", live_tick: "当前",
    k_live: "当前镜头", k_review: "回看", k_hint: "点刻度或拖动，回看某一轮", k_follow: "跟随最新",
    need_none: "需求单里还没有带判定方法的条目。", need_empty: "这一条还没有测量值。", need_live: "当前",
    expand: "放大", close: "关闭", gate_wait: "等你确认", gate_signed: "已确认", gate_signed_at: (h) => `已于 ${h} 确认`, gate_auto: "自主运行",
    fk_demand: "需求", fk_weak: "依据薄弱", fk_missing: "缺来源", fk_model: "模型", fk_rebuild: "重建", fk_todo: "待补",
    need_curve: "曲线", need_table: "表格", need_trend: "各轮", display_only: "只看", iteration: (n) => `第 ${n} 步`,
    pick_title: "指标目录", pick_save: "保存", pick_catalog: "目录", pick_chosen: "追加显示（不参与判定）", pick_none: "还没有追加的指标。",
    pick_hint: "点左侧条目加入；key 是显示名，参数按 JSON 填写（悬停看可用参数）。需求单里绑定的指标自动显示，不用在这里加。",
    pick_saved: "已保存，下一帧开始计算", pick_later: "后台进程忙或未连接，空闲后自动再存", pick_unavailable: "读不到指标目录（需要后台进程已加载镜头）：",
    pick_stale: "目录未更新：", pick_need_key: "每一项都要有 key", pick_bad_params: "参数不是 JSON 对象", pick_dup: "key 有重复", pick_conflict: "与需求条目同名",
    avail_available: "可算", avail_unsupported: "当前后端不支持", avail_unknown: "未知", live_error: "实时计算出错",
    gate_ok: "确认", gate_note_short: "备注（要求修改时必填）", ld_objects: "非序列物体", no_ld: "等待镜头数据",
  },
  en: {
    interview: "Interview & feasibility", demand_confirm: "Confirm demand", modeling: "Start designs & model", model_approve: "Approve model", design: "Autonomous design", deliver: "Delivery report",
    back_reject: "revise design", legacy_job: "Legacy job: create a new job and remeasure",
    no_demand: "interviewing, no demand yet", invalid_demand: "demand fails validation", first: "waiting for the user", reconfirm: "demand changed, confirm again",
    waiting: "your turn", blocked: "question", active: "in progress", running: "running",
    rounds_n: (n) => `round ${n}`, no_rounds: "no rounds yet", rtc_unclosed: "rtc results await a closing OpticStudio optimisation",
    confirm_pending: (n) => `round ${n} computed on rtc awaits OpticStudio: saos round --confirm`, confirm_failed: (n) => `OpticStudio disagreed with rtc on round ${n}; back to design`,
    tag_rtc: "rtc", tag_confirmed: "OpticStudio re-measured", tag_disagrees: "re-measure differs",
    starts_done: (n, k) => `${n} ${n === 1 ? "start" : "starts"} · ${k} not evaluated`,
    explore_run: (n) => `exploring ${n} ${n === 1 ? "candidate" : "candidates"}`, explore_done: (n) => `compared ${n} ${n === 1 ? "candidate" : "candidates"}`,
    st_achieved: "achieved", st_exhausted: "exhausted", st_limit: "round limit", st_stopped: "stopped by user",
    report_ready: "report written", report_todo: "report pending", deliver_cmd: "saos scan (tolerance, if needed) · saos report",
    back_demand: "demand change", back_model: "revise model", back_reach: "unreachable", feasibility: "question · check bounds", feasibility_rtc: "rtc checks · no licence", feasibility_written: "feasibility recorded",
    standalone: "Background · standalone", rtc_instance: "rtc instance · every stage before delivery, no licence", no_daemon: "daemon not running",
    rtc_dual: (started) => `rtc · OpticStudio ${started ? "standing by" : "not started"}`,
    rtc_switch: "The daemon holds rtc only (daemon start --backend rtc); the model page and delivery need OpticStudio: save, saos daemon stop, then saos daemon start.",
    tab_layout: "Layout", tab_spot: "Spot Diagram", tab_mtf: "MTF", tab_rounds: "Rounds",
    blocking: "Blocking questions:",
    loading: "loading…", failed: "failed",
    no_viz: "After round 0 is recorded, its layout, spot diagram and MTF appear here.", viz_missing: "This round has no visual data (recorded with --no-viz or before saos ui).",
    viz_failed: "The visual export of this round failed: ", spot_foot: (ref) => `Points: batch ray trace relative to the primary chief ray, vignetted rays left out. RMS / GEO: OpticStudio Standard Spot (reference ${ref}, all wavelengths), in µm. Standard Spot traces rays per wavelength in proportion to its weight, so wavelengths weighted below 1 often lose their rim rays and GEO can be smaller than the farthest point drawn.`,
    spot_foot_rtc: () => "Points: rtc batch ray trace relative to the primary chief ray, vignetted rays left out. RMS / GEO: the same points over all wavelengths by weight, in µm; not reconciled with OpticStudio's Standard Spot.",
    fan_foot: "Ray fan: rtc batch ray trace, ey against Py (tangential) and ex against Px (sagittal), relative to the primary chief ray; rays missing the image break the curve.",
    mtf_foot_rtc: "Curves: rtc FFT MTF (64×64 pupil samples, each wavelength about its own chief ray), not reconciled with OpticStudio.",
    mtf_foot: "Curves: OpticStudio FFT MTF export.", mtf_partial: "Not computable for all fields at once, so computed field by field: ", tangential: "Tangential", sagittal: "Sagittal", diff: "Diff. limit",
    layout_foot: "Geometry and rays: ZOS-API (GetGlobalMatrix, GetSag, batch ray trace).",
    layout_foot_rtc: "Geometry and rays: rtc (sag, real ray trace; semi-diameters except the object's, and solves, recomputed in rtc by OpticStudio's rules). Re-measured on OpticStudio before delivery.",
    unverified: (items) => `Not reconciled with OpticStudio: ${items.join("; ")}.`,
    model_evidence: "Model approval evidence: ",
    redescribe: "Structure changed: describe and approve",
    modeling_sub: "Start on rtc · screen · describe", approval_sub: "Direction · tradeoffs", design_sub: "Optimise on rtc · review on expiry · round",
    layout_hint_3d: "drag to rotate · Shift or right-drag to pan · wheel zooms at the cursor · double-click to reset · hover a surface for its data", layout_hint_2d: "drag to pan · Shift or right-drag to measure (snaps to surface points) · wheel zooms at the cursor · double-click to reset · Esc clears the ruler · hover a surface for its data",
    image_z: "image z", round: "Round", tiers: "Tiers", gap: "Shortfall", state: "State", note: "Note", achieved: "achieved", deliverable: "deliverable",
    pick_round: "Click a row to view that round in the other tabs", sag_flat: "Some surface shapes could not be read; drawn flat.", select_round: (n) => `r${String(n).padStart(3, "0")}`,
    live: "Current lens", live_unsaved: "unsaved", no_live: "Once the daemon loads or changes the lens, its layout appears here.", live_failed: "The live view export failed: ",
    only_3d: "The surfaces share no plane of symmetry: 3D view only", disconnected: "Lost the saos ui service; reconnecting…", notice_close: "Dismiss", lens_data: "Lens Data", ld_type: "Type", ld_comment: "Comment", ld_radius: "Radius", ld_thickness: "Thickness",
    gate_demand: "Confirm demand", gate_model: "Approve model", gate_weak: "Limits on a weak basis", gate_missing: (ids) => `These items have no source yet, so the demand cannot be confirmed: ${ids}`,
    gate_open: "Open the page", gate_rebuild: (r) => `Why it was rebuilt: ${r}`, gate_no_rebuild: "The structure differs from the last approval and the description gives no reason yet: ask the agent to add it and describe again",
    gate_no_search: "The description records no starting-form search: ask the agent to add it and describe again", gate_note: "Note (optional)", gate_done: "Your confirmation is recorded", gate_old: "This description predates window approval: ask the agent to describe again",
    gate_revise: "Ask for changes", gate_revise_need: "Write what to change in the note first", gate_revised: (n) => `Changes requested, waiting for a new version: ${n}`, gate_revise_done: "Your change request is recorded",
    mode_auto: "Autonomous run: the agent passes the gates with its reasons on record, and the report says you did not confirm them", mode_to_interactive: "Let me confirm instead", mode_switched: "Now interactive: you confirm the next gates",
    ld_material: "Material", ld_sd: "Semi-dia.", ld_conic: "Conic", ld_solve: "V marks a variable, other letters a solve or pickup",
    clash_title: "Interference", crossing: (a, b, d) => `surfaces ${a} and ${b} cross inside their apertures (up to about ${d})`,
    overlap: (a, b, n) => `lenses ${a} and ${b} interpenetrate (${n} sample points inside the other's glass)`,
    live_optimising: "optimising, rays update when it ends", opt_grid: "Grid", opt_labels: "Surface #", png_title: "Save the current view as PNG", tip_air: "air",
    ruler: (d, dz, dy, u) => `${d} ${u} (Δz ${dz} · Δy ${dy})`,
    tip_lens: (k, m, ct, et, u) => `L${k} ${m} · centre ${ct} · edge ${et} ${u}`,
    tab_detectors: "Detectors", nsc_title: "Non-sequential system", nsc_foot: "Outlines follow the non-sequential editor's parameters (global placement from ZOS-API GetMatrix); types without an outline are only listed. 2D is the global YZ section.",
    nsc_undrawn: "Objects without an outline: ", nsc_overlap: (a, b, d) => `objects ${a} and ${b} overlap (about ${d}) without nesting`,
    th_obj: ["#", "Type", "Comment", "Material", "Volume physics", "Faces"], face_is: { reflective: "reflective", absorbing: "absorbing" }, face_word: "face", kind_source: "source", kind_detector: "detector",
    det_none: "This round has no detector readings.", det_note: "Readings are what the last trace of this round's checks left in memory; empty when the checks traced nothing. The map is irradiance (W/cm²), one cell per pixel.",
    det_power: "Power", det_hits: "Ray hits", det_pixels: "Pixels", det_peak: "Peak irradiance",
    tab_fan: "Ray Fan", tab_field: "Field Curv. / Distortion", tab_starts: "Starts",
    gap: (name, b) => `This round's ${b || "backend"} export has no ${name}. The gap is shown as is; the window computes nothing itself.`,
    fan_t: "Tangential ey(py)", fan_s: "Sagittal ex(px)", pupil: "Normalised pupil",
    curv: "Field curvature (distance from image)", dist: "Distortion %", field_axis: "Field",
    starts_none: "No starts.generate record yet. After starts are generated, this lists sources, scaling, exceeded ranges, first- and third-order predictions and the screening.",
    st_form: "Form", st_source: "Source", st_scale: "Scale", st_tight: "Tightest", st_extra: "Outside the source example", st_efl: "EFL", st_bfl: "BFL", st_fno: "f/#",
    st_petz: "Petzval R/f", st_seidel: "Third-order totals (waves)", st_aoi: "Max incidence", st_excluded: "Forms not selected", st_noteval: "Not evaluated (material/computation)",
    st_screen: "Zero-optimisation screening", st_short: (s) => `Equal short budget · ${s} s`, st_running: "running", st_verdict: { valid: "valid", invalid: "invalid within budget", material: "not evaluated: material", computation: "not evaluated: computation" },
    st_g: "G start → end", st_warn: "Numeric warnings", st_page: "Comparison page", st_foot: "Reads existing files in explore/starts and scans only; predictions come from OpticStudio operands and native analyses, not from optimisation.",
    hover_row: "Point at a row to highlight its surface in the layout",
    tab_metrics: "Metrics", metrics_none: "The demand has no checked items yet.",
    metric_missing: "no longer in the demand", metric_bound: "Bound", metric_no_value: "no valid value", merit: "Merit",
    metric_stale: "from before the last lens change", metric_slow: "slower than the live time limit; value from saos check or round", metric_pending: "not measured yet",
    metric_src: { live: "live", check: "check", round: "round" },
    metrics_foot: "The current-lens row is measured by the daemon after every lens change (limited to SAOS_LIVE_S seconds, 3 by default; items that do not fit take the value from saos check or round); grey values are from before the last change. The other rows are each round's ledger values; dashed lines are the demand's bounds. saos ui open --metrics picks and orders the items.",
    lens: "Lens", judge: "Checks", tl_title: "Rounds", tl_rounds: (n) => `${n} ${n === 1 ? "round" : "rounds"} recorded`, tl_none: "no rounds yet", live_tick: "now",
    k_live: "Current lens", k_review: "reviewing", k_hint: "click a tick or drag to review a round", k_follow: "following the latest",
    need_none: "The demand has no checked items yet.", need_empty: "No measured value for this item yet.", need_live: "now",
    expand: "Expand", close: "Close", gate_wait: "your turn", gate_signed: "confirmed", gate_signed_at: (h) => `confirmed at ${h}`, gate_auto: "autonomous",
    fk_demand: "Demand", fk_weak: "Weak basis", fk_missing: "No source", fk_model: "Model", fk_rebuild: "Rebuild", fk_todo: "To add",
    need_curve: "Curve", need_table: "Table", need_trend: "Rounds", display_only: "view only", iteration: (n) => `step ${n}`,
    pick_title: "Metric catalog", pick_save: "Save", pick_catalog: "Catalog", pick_chosen: "Shown in addition (not judged)", pick_none: "No added metrics yet.",
    pick_hint: "Click an entry on the left to add it; key is the display name, params are JSON (hover to see the allowed ones). Metrics bound in the demand show up by themselves.",
    pick_saved: "Saved; the next frame computes it", pick_later: "The daemon is busy or not connected; saving again once it is idle", pick_unavailable: "Cannot read the metric catalog (the daemon must have a lens loaded): ",
    pick_stale: "Catalog not refreshed: ", pick_need_key: "Every entry needs a key", pick_bad_params: "params is not a JSON object", pick_dup: "Duplicate keys", pick_conflict: "same id as a demand item",
    avail_available: "computable", avail_unsupported: "not on this backend", avail_unknown: "unknown", live_error: "live computation failed",
    gate_ok: "Confirm", gate_note_short: "Note (required to ask for changes)", ld_objects: "Non-sequential objects", no_ld: "waiting for lens data",
  },
};
// 每轮 viz.json 里的分析图：一处登记，标签、取数和画法都从这里来（借鉴 Optiland GUI 的 registry.py）。
// key 是 viz.json 里的字段；某一轮没有这个字段时页面显示缺口，不在界面里补算
// one registry for the per-round plots in viz.json (after the Optiland GUI's registry.py); a missing key is shown as a gap
const ROUND_PLOTS = [
  { id: "spot", key: "spot", render: (v) => renderSpot(v) },
  { id: "fan", key: "ray_fan", render: (v) => renderFan(v) },
  { id: "field", key: "field_plots", render: (v) => renderField(v) },
  { id: "mtf", key: "mtf", render: (v) => renderMtf(v) },
];
const NSC_PLOTS = [{ id: "detectors", key: "detectors", gapless: true, render: (v) => renderDetectors(v) }];
const plotsFor = () => (isNsc() ? NSC_PLOTS : ROUND_PLOTS);
const plotIds = () => [...ROUND_PLOTS, ...NSC_PLOTS].map((p) => p.id);
// ayu Dark：视场依次黄、薄荷、橙，少用蓝紫 / ayu Dark: fields gold, mint, orange first; little blue or violet
const FIELD_COLORS = ["#e6b450", "#95e6cb", "#f29668", "#aad94c", "#f07178", "#d9c38c", "#7fd9b0", "#ffb38a"];
const MATERIAL_COLORS = ["#d5dae1", "#95e6cb", "#e6b450", "#f29668", "#aad94c", "#c9b38a"];

let lang = "zh";
let state = null;
let stateText = "";
let tab = Q.get("tab");
// 时间轴选中的对象："live"（当前镜头）、轮次号，null 为跟随最新 / the timeline selection: "live", a round number, or null to follow the latest
let pinnedRound = Q.get("round") === "live" ? "live" : Q.get("round") ? Number(Q.get("round")) : null;
// 渲染器读的全局：画哪一份导出、它属于哪一轮；镜头卡与分析卡各有一份，画之前换进来
// globals the renderers read: which export and which round; the lens card and the analysis card each keep theirs and swap it in
let roundShown = null;
let viz = null;
let lensViz = null;
let anaViz = null;
let lensSig = null;
let anaSig = null;
let metricsSeen = null;
let dim = Q.get("dim") === "2d" ? "2d" : "3d";
let needId = Q.get("need");
let ldHover = null;
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
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
  if (!msg.ok) { const err = new Error(msg.error?.message || "error"); err.code = msg.error?.code; throw err; }
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

// 图注与当前语言保持一致 / captions follow the current language
function foot(key, ...args) {
  return t(key, ...args);
}

// ---------------- 里程表：数字逐位滚动 ----------------

// 数字位换成一列 0–9 的竖条，按位平移；字形（位数、小数点位置）变了才重建 / digits roll; rebuilt only when the shape changes
function odo(el, text) {
  text = String(text);
  if (el.dataset.v === text) return;
  const old = el.dataset.v || "";
  el.dataset.v = text;
  if (reduceMotion) { el.textContent = text; return; }
  const shape = (s) => [...s].map((c) => (/\d/.test(c) ? "d" : c)).join("");
  if (shape(old) !== shape(text) || !el.querySelector(".strip")) {
    el.classList.add("odo");
    el.innerHTML = [...text].map((ch) => /\d/.test(ch)
      ? `<span class="c"><span class="strip">${"0123456789".split("").map((d) => `<span>${d}</span>`).join("")}</span></span>`
      : `<span class="c"><span>${esc(ch)}</span></span>`).join("");
    if (!old) el.querySelectorAll(".strip").forEach((s) => (s.style.transition = "none"));
  }
  [...text].forEach((ch, i) => { const s = el.children[i]?.querySelector(".strip"); if (s) s.style.transform = `translateY(${-Number(ch) * 1.2}em)`; });
  if (!old) requestAnimationFrame(() => el.querySelectorAll(".strip").forEach((s) => (s.style.transition = "")));
}

// ---------------- 顶部：流程弧线 ----------------

function nodeSub(step) {
  const d = step.detail;
  if (step.id === "interview") {
    if (d) return t(d);
    if (state.feasibility) return t("feasibility_written");
    return state.daemon.running && state.daemon.backend === "rtc" ? t("feasibility_rtc") : t("feasibility");
  }
  if (step.id === "demand_confirm") return d ? t(d) : "saos demand confirm";
  if (step.id === "modeling") {
    if (d && d.needs_description) return t("redescribe");
    if (step.status === "active" && state.progress) return `${state.progress.op}`;
    const x = d && d.exploration;
    if (x) return x.finished ? t("explore_done", x.candidates) : t("explore_run", x.candidates);
    if (d && d.starts) return t("starts_done", d.starts.count, d.starts.not_evaluated);
    return t("modeling_sub");
  }
  if (step.id === "model_approve") return typeof d === "string" ? d : t("approval_sub");
  if (step.id === "design") {
    if (!d) return t("design_sub");
    if (d.legacy) return t("legacy_job");
    const last = d.last;
    const unclosed = d.rtc_unclosed ? ` · ${t("rtc_unclosed")}` : "";
    if (!last) return t("no_rounds") + unclosed;
    if (d.confirm_failed != null) return t("confirm_failed", d.confirm_failed);
    const rtc = last.backend === "rtc" ? ` · ${t("tag_rtc")}` : "";
    return `${t("select_round", last.round)} · G ${fmtG(last.shortfall)} · ${last.tiers.join("+")}${rtc}${unclosed}`;
  }
  if (step.id === "deliver") {
    if (d && state.confirm_pending != null) return `${t("st_" + d)} · ${t("confirm_pending", state.confirm_pending)}`;
    return d ? `${t("st_" + d)} · ${step.status === "done" ? t("report_ready") : t("report_todo")}` : t("deliver_cmd");
  }
  return "";
}

const GATES = new Set(["demand_confirm", "model_approve"]);
let arcDone = 0;
// 六个节点排在一条上拱的弧线上；走过的一段描黄，当前节点有呼吸光圈；完整说明放在悬停提示里
// six nodes along an upward arc; the travelled part is gold, the current node breathes; full text in the hover title
function renderArc() {
  const svg = $("arc"), box = svg.getBoundingClientRect(), W = box.width, H = box.height;
  if (!W) return;
  const x0 = 8, x1 = W - 8, yTop = 22, yEnd = H - 44;
  const P = (s) => [(1 - s) ** 2 * x0 + 2 * (1 - s) * s * ((x0 + x1) / 2) + s * s * x1, (1 - s) ** 2 * yEnd + 2 * (1 - s) * s * (yTop - 20) + s * s * yEnd];
  const steps = state.steps, n = steps.length, ts = steps.map((_, i) => 0.03 + (0.94 * i) / (n - 1));
  const at = steps.findIndex((s) => s.status !== "done"), doneT = at < 0 ? 1 : ts[at];
  const running = !!state.progress;
  const room = Math.max(8, Math.floor((W / n) / (lang === "zh" ? 11 : 6.2)));
  const cut = (s) => ([...s].length > room ? [...s].slice(0, room - 1).join("") + "…" : s);
  const d = `M${x0},${yEnd} Q${(x0 + x1) / 2},${yTop - 20} ${x1},${yEnd}`;
  let s = `<path class="arc" d="${d}"/><path class="arc-done" id="arc-done" d="${d}" pathLength="1" stroke-dasharray="1" stroke-dashoffset="${1 - arcDone}"/>`;
  steps.forEach((st, i) => {
    const [x, y] = P(ts[i]);
    const shape = GATES.has(st.id) ? `<rect class="core" x="-5.5" y="-5.5" width="11" height="11" rx="2" transform="rotate(45)"/>` : `<circle class="core" r="6.5"/>`;
    const full = nodeSub(st), sub = ["waiting", "blocked"].includes(st.status) ? t(st.status) : full;
    s += `<g class="node ${st.status} ${st.status === "active" && running ? "running" : ""}" transform="translate(${x},${y})"><title>${esc(`${t(st.id)} · ${full}`)}</title>`
      + `<circle class="halo" r="8"/>${shape}<text y="24">${esc(t(st.id))}</text><text class="sub" y="37">${esc(cut(sub))}</text></g>`;
  });
  svg.innerHTML = s;
  const path = $("arc-done");
  if (doneT !== arcDone && !reduceMotion) requestAnimationFrame(() => path.setAttribute("stroke-dashoffset", 1 - doneT));
  else path.setAttribute("stroke-dashoffset", 1 - doneT);
  arcDone = doneT;
}

function renderTop() {
  // 标题带任务号：命令行在需要用户看的时候按标题找到这个窗口并置前 / the CLI finds this window by its title to raise it
  document.title = `saos · ${JOB}`;
  const notice = $("notice");
  notice.hidden = !state.notice;
  if (state.notice) { $("notice-text").textContent = state.notice.message; $("notice-close").textContent = t("notice_close"); }
  $("job-name").textContent = state.job;
  $("job-title").textContent = state.name || "";
  $("job-title").title = state.name || "";
  const chip = $("mode-chip");
  const running = state.daemon.running;
  const rtc = running && state.daemon.backend === "rtc";
  chip.textContent = rtc ? (state.daemon.dual ? t("rtc_dual", state.daemon.zos_started) : t("rtc_instance"))
    : running ? t("standalone") : t("no_daemon");
  chip.title = chip.textContent;
  chip.className = "pill" + (rtc ? " rtc" : "");
  const run = $("run-chip");
  if (state.progress) {
    run.hidden = false;
    const started = Date.parse(state.progress.started);
    const detail = state.progress.detail ? ` ${state.progress.detail}` : "";
    $("run-text").textContent = `${state.progress.op}${detail} · ${fmtDuration((Date.now() - started) / 1000)}`;
  } else run.hidden = true;
  // 当前镜头的评价函数和目标差距，后台进程自动更新 / the live merit and shortfall, updated by the daemon
  const m = state.metrics, mc = $("metric-chip");
  const g = m?.shortfall ? m.shortfall.value : null;
  const show = m && (m.merit != null || m.shortfall);
  mc.hidden = !show;
  if (show) {
    if (!mc.querySelector(".gv")) mc.innerHTML = `<span class="k">${esc(t("merit"))}</span><span class="mv"></span><span class="k">G</span><span class="gv"></span><span class="it"></span>`;
    mc.querySelector(".k").textContent = t("merit");
    mc.querySelector(".mv").textContent = m.merit != null ? fmtG(m.merit) : "–";
    odo(mc.querySelector(".gv"), m.shortfall ? fmtG(g) : "–");
    // rtc 实时帧（ADR-0022）：优化中带步数；出错时标红并把原因放进悬停提示 / live rtc frames carry the step; errors go to the title
    const lv = m.live || {};
    mc.querySelector(".it").textContent = lv.preview && m.iteration != null ? t("iteration", m.iteration) : "";
    mc.classList.toggle("err", lv.status === "error");
    mc.title = lv.status === "error" ? `${t("live_error")}: ${lv.error || ""}${lv.timeout_s ? ` (${lv.timeout_s} s)` : ""}` : "";
    mc.classList.toggle("ok", g === 0);
    // 新格式按 G 自己的视图版本判断；旧格式运行中一律算旧 / schema 2 compares G's own view; older files are stale while running
    mc.classList.toggle("stale", m.schema === 2 ? !!(m.shortfall && m.shortfall.view !== m.view) : !!state.progress || (m.shortfall && m.shortfall.view !== m.view));
  }
}

// 阻塞问题、模型记录提示、后端提示：有才显示 / blocking questions, model evidence and backend hints, shown only when present
function renderNotes() {
  const parts = [];
  if (state.blocking.length) parts.push(`<div class="q"><b>${esc(t("blocking"))}</b><br>${state.blocking.map((b) => esc(b.question || b)).join("<br>")}</div>`);
  if (state.model_evidence?.length) parts.push(`<div class="q"><b>${esc(t("model_evidence"))}</b><br>${state.model_evidence.map((p) => esc(`${p.path}: ${p.code}`)).join("<br>")}</div>`);
  // 只有 rtc 的后台进程（--backend rtc）：建模、确认和交付要换成带 OpticStudio 的；两个后端都有时它自己切换（ADR-0018）
  if (state.daemon.running && state.daemon.backend === "rtc" && !state.daemon.dual && ["modeling", "model_approve", "deliver"].includes(state.current)) {
    parts.push(`<div class="q">${esc(t("rtc_switch"))}</div>`);
  }
  const el = $("notes"), html = parts.join("");
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
  el.hidden = !parts.length;
}

// ---------------- 选中的轮次与导出数据 ----------------

const vizRounds = () => (state?.rounds || []).filter((r) => r.viz);
const latestRound = () => vizRounds().at(-1)?.round ?? null;
// 跟随时：有当前镜头就看当前镜头，否则看最新一轮 / following: the live lens when there is one, else the latest round
const selected = () => pinnedRound ?? (state?.live != null ? "live" : latestRound());
// 点列图、MTF 等只有轮次有：选中当前镜头时看最新一轮 / per-round plots: the latest round while the live lens is selected
const anaRound = () => (typeof selected() === "number" ? selected() : latestRound());
const isNscOf = (v) => v?.system?.mode === "non_sequential";
const isNsc = () => isNscOf(lensViz?.ok ? lensViz : anaViz);
const roundOf = (n) => (state?.rounds || []).find((r) => r.round === n);

const vizCache = new Map();
async function loadViz(key) {
  if (key === "live") {
    const k = `live:${state.live}`;
    if (!vizCache.has(k)) {
      [...vizCache.keys()].filter((x) => x.startsWith("live:")).forEach((x) => vizCache.delete(x));
      try { vizCache.set(k, await api("live")); } catch (e) { return { ok: false, error: e.message, live: true }; }
    }
    return vizCache.get(k);
  }
  const r = roundOf(key);
  if (!r?.viz) return { ok: false, error: "no viz.json" };
  const k = `r:${key}:${r.time}`;
  if (!vizCache.has(k)) {
    try { vizCache.set(k, await api("viz", { round: key })); } catch (e) { return { ok: false, error: e.message }; }
  }
  return vizCache.get(k);
}

function vizProblem(v, key) {
  if (key === "live") return t("live_failed") + (v.error || "");
  return v.error?.includes?.("no viz.json") ? t("viz_missing") : t("viz_failed") + (v.error || "");
}

// 镜头卡：布局常驻；选中对象或数据变了才重画，免得每次轮询都清掉视角和测距
// the lens card: the layout stays; redrawn only when the selection or its data change, keeping camera and ruler
async function renderLens(force = false) {
  const key = selected(), host = $("lens");
  $("lens-title").textContent = t("lens");
  const sig = JSON.stringify([key, key === "live" ? state.live : roundOf(key)?.time ?? null, dim, lang]);
  if (!force && sig === lensSig) return;
  lensSig = sig;
  if (key == null) {
    lensViz = null;
    host.innerHTML = `<div class="empty">${esc(t("no_live"))}</div>`;
    $("ld").innerHTML = `<div class="empty">${esc(t("no_ld"))}</div>`;
    $("lens-src").textContent = "";
    return;
  }
  const v = await loadViz(key);
  if (JSON.stringify([selected(), key === "live" ? state.live : roundOf(key)?.time ?? null, dim, lang]) !== sig) return;
  lensViz = v;
  if (!v.ok) {
    host.innerHTML = `<div class="empty">${esc(vizProblem(v, key))}</div>`;
    $("ld").innerHTML = `<div class="empty">${esc(t("no_ld"))}</div>`;
    return;
  }
  viz = v; roundShown = key;
  if (isNscOf(v)) renderNscLayout(host); else renderLayout(host);
}

// 分析卡（以及放大层）的标签：点列图等只看轮次；指标、轮次、起点另有来源
// tabs of the analysis card (and its enlarged copy): per-round plots, plus metrics, rounds and starts
function tabList() {
  const list = [];
  const hasStarts = !!state?.steps?.find((s) => s.id === "modeling")?.detail?.starts;
  const hasMetrics = !!state?.metric_items?.length;
  const applies = (k) => k === "rounds" || (k === "detectors" ? isNsc() : ["spot", "fan", "field", "mtf"].includes(k) ? !isNsc()
    : k === "starts" ? hasStarts : k === "metrics" ? hasMetrics : false);
  // agent 用 saos ui open --tabs 为这类系统选的标签页和顺序；布局已常驻在镜头卡里 / the agent's --tabs choice; the layout lives in the lens card
  const chosen = state?.ui?.tabs?.filter((k) => k !== "layout");
  const shown = (chosen?.length ? chosen : [...plotsFor().map((p) => p.id), "metrics", "rounds", "starts"]).filter(applies);
  list.push(...(shown.length ? shown : ["rounds"]));
  return list;
}

function renderTabs() {
  const list = tabList();
  if (!tab || !list.includes(tab)) tab = list[0];
  const html = list.map((k) => `<button class="tab ${k === tab ? "on" : ""}" data-tab="${k}" role="tab">${esc(t("tab_" + k))}</button>`).join("");
  if ($("tabs").dataset.html !== html) { $("tabs").innerHTML = html; $("tabs").dataset.html = html; }
  $("expand").title = t("expand");
}

function anaSignature() {
  const r = anaRound();
  const modeling = JSON.stringify(state?.steps?.find((x) => x.id === "modeling")?.detail ?? null);
  return JSON.stringify([tab, lang, r, roundOf(r)?.time ?? null, selected(), tab === "metrics" ? [state.metrics, state.metric_items, !!state.progress] : null,
    tab === "rounds" ? state.rounds : null, tab === "starts" ? modeling : null]);
}

async function renderAna(force = false) {
  const sig = anaSignature();
  if (!force && sig === anaSig) return;
  anaSig = sig;
  // 渲染器按 id 找子元素（mtf-plot、g-plot…），同一时刻只画在一处：放大层开着就只画放大层
  // renderers look children up by id, so draw in one place at a time: the enlarged layer while it is open
  const zoomed = !$("zoom").hidden;
  if (zoomed) $("zoom-title").textContent = t("tab_" + tab);
  await renderTabInto(zoomed ? $("zoom-view") : $("view"));
}

function setZoom(open) {
  $("zoom").hidden = !open;
  if (open) $("view").innerHTML = ""; else $("zoom-view").innerHTML = "";
  renderAna(true);
}

async function renderTabInto(view) {
  if (tab === "rounds") return renderRounds(view);
  if (tab === "starts") return renderStarts(view);
  if (tab === "metrics") return renderMetrics(view);
  const key = anaRound();
  if (key == null) { view.innerHTML = `<div class="empty">${esc(t("no_viz"))}</div>`; return; }
  const v = await loadViz(key);
  if (anaRound() !== key) return;
  anaViz = v;
  if (!v.ok) { view.innerHTML = `<div class="empty">${esc(vizProblem(v, key))}</div>`; return; }
  const plot = plotsFor().find((p) => p.id === tab);
  if (!plot) { view.innerHTML = ""; return; }
  if (!plot.gapless && v[plot.key] == null) { view.innerHTML = `<div class="empty">${esc(t("gap", t("tab_" + plot.id), v.system?.backend))}</div>`; return; }
  viz = v; roundShown = key;
  plot.render(view);
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
  const GAP = 10, CELL = view.classList.contains("wide") ? 220 : Math.max(110, Math.min(220, (view.clientWidth - 30 - (cols - 1) * GAP) / cols));
  view.innerHTML = `<div class="figure" style="min-width:${cols * CELL + (cols - 1) * GAP + 30}px">
    <div class="fig-head"><div class="legend">${sys.wavelengths.map((w, i) => `<span><i style="background:${colors[i]}"></i>${Math.round(w.um * 1000)} nm</span>`).join("")}</div></div>
    <div class="spot-grid" style="grid-template-columns:repeat(${cols},minmax(${CELL}px,1fr))"></div></div>
    <div class="foot">${esc(foot(viz.backend === "rtc" ? "spot_foot_rtc" : "spot_foot", spot.analysis_settings?.ReferTo || "—"))}</div>`;
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
    <div class="foot">${esc(foot(viz.backend === "rtc" ? "mtf_foot_rtc" : "mtf_foot"))} ${esc(settings)}${mtf.per_field ? `<br>${esc(t("mtf_partial") + (viz.errors?.FftMtf || ""))}` : ""}</div>`;
  const host = $("mtf-plot");
  const head = view.querySelector(".fig-head").offsetHeight;
  const W = host.clientWidth, H = Math.max(150, Math.min(520, view.clientHeight - head - 70));
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

// ---------------- 通用折线图 ----------------

// 一张带网格和刻度的折线图；曲线点为 null 时断开。扇形图、场曲和畸变共用
// one gridded line plot; a null point breaks the curve. Shared by the ray fan, field curvature and distortion
function linePlot(host, { W, H, xr, yr, curves, xLabel = "", yLabel = "", zeroX = false, zeroY = false }) {
  const [, g] = canvasIn(host, W, H);
  const pad = { l: 52, r: 12, t: yLabel ? 20 : 8, b: 36 };
  const [x0, x1] = xr[0] === xr[1] ? [xr[0] - 1, xr[1] + 1] : xr, [y0, y1] = yr[0] === yr[1] ? [yr[0] - 1, yr[1] + 1] : yr;
  const px = (x) => pad.l + ((x - x0) / (x1 - x0)) * (W - pad.l - pad.r), py = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  g.fillStyle = css("--plot-bg"); g.fillRect(pad.l, pad.t, W - pad.l - pad.r, H - pad.t - pad.b);
  g.strokeStyle = css("--grid"); g.lineWidth = 1;
  g.fillStyle = css("--text-2"); g.font = `11px ${css("--mono")}`;
  const xs = niceStep((x1 - x0) / 4), ys = niceStep((y1 - y0) / 4);
  ticks(x0, x1, xs).forEach((x) => {
    const X = px(x); g.beginPath(); g.moveTo(X + 0.5, pad.t); g.lineTo(X + 0.5, H - pad.b); g.stroke();
    g.textAlign = "center"; g.textBaseline = "top"; g.fillText(sig(x, 3), X, H - pad.b + 5);
  });
  ticks(y0, y1, ys).forEach((y) => {
    const Y = py(y); g.beginPath(); g.moveTo(pad.l, Y + 0.5); g.lineTo(W - pad.r, Y + 0.5); g.stroke();
    g.textAlign = "right"; g.textBaseline = "middle"; g.fillText(sig(y, 3), pad.l - 6, Y);
  });
  g.strokeStyle = css("--text-3");
  if (zeroX && x0 < 0 && x1 > 0) { g.beginPath(); g.moveTo(px(0) + 0.5, pad.t); g.lineTo(px(0) + 0.5, H - pad.b); g.stroke(); }
  if (zeroY && y0 < 0 && y1 > 0) { g.beginPath(); g.moveTo(pad.l, py(0) + 0.5); g.lineTo(W - pad.r, py(0) + 0.5); g.stroke(); }
  g.fillStyle = css("--text-3"); g.textAlign = "center"; g.textBaseline = "top";
  g.fillText(xLabel, pad.l + (W - pad.l - pad.r) / 2, H - pad.b + 20);
  if (yLabel) { g.textAlign = "left"; g.fillText(yLabel, pad.l, 2); }
  g.save(); g.beginPath(); g.rect(pad.l, pad.t, W - pad.l - pad.r, H - pad.t - pad.b); g.clip();
  curves.forEach((c) => {
    g.strokeStyle = c.color; g.lineWidth = 1.6; g.setLineDash(c.dash || []); g.beginPath();
    let pen = false;
    c.x.forEach((x, i) => {
      const y = c.y[i];
      if (x == null || y == null || !isFinite(x) || !isFinite(y)) { pen = false; return; }
      pen ? g.lineTo(px(x), py(y)) : g.moveTo(px(x), py(y));
      pen = true;
    });
    g.stroke();
  });
  g.restore(); g.setLineDash([]);
}

const unitOf = (sys) => (sys.lens_units === "Millimeters" ? "mm" : sys.lens_units);
const extent = (vals) => { const ok = vals.filter((v) => v != null && isFinite(v)); return ok.length ? [Math.min(...ok), Math.max(...ok)] : [-1, 1]; };

// ---------------- 光线扇图 ----------------

function renderFan(view) {
  const sys = viz.system, fan = viz.ray_fan;
  const colors = waveColors(sys.wavelengths);
  // 各视场共用纵轴，才能一眼比较视场之间的像差大小 / one vertical range for all fields so they compare at a glance
  let half = 0;
  fan.fields.forEach((f) => [f.tangential, f.sagittal].forEach((set) => (set || []).forEach((ys) => ys.forEach((v) => { if (v != null && isFinite(v)) half = Math.max(half, Math.abs(v)); }))));
  half = half > 0 ? Number((half * 1.08).toPrecision(2)) : 1e-3;
  view.innerHTML = `<div class="figure"><div class="fig-head"><div class="legend">${sys.wavelengths.map((w, i) => `<span><i style="background:${colors[i]}"></i>${Math.round(w.um * 1000)} nm</span>`).join("")}</div></div>
    <div class="fan-grid"></div></div><div class="foot">${esc(foot("fan_foot"))}</div>`;
  const grid = view.querySelector(".fan-grid");
  const W = Math.max(220, (grid.clientWidth - 12) / 2);
  fan.fields.forEach((f) => {
    const row = document.createElement("div");
    row.className = "fan-row";
    row.innerHTML = `<div class="cap"><b>${esc(fieldLabel(sys, f.field))}</b></div><div class="fan-pair"><div></div><div></div></div>`;
    grid.appendChild(row);
    const [ht, hs] = row.querySelectorAll(".fan-pair > div");
    if (!f.tangential) { ht.innerHTML = hs.innerHTML = `<div class="empty">${esc(t("gap", t("tab_fan"), sys.backend))}</div>`; return; }
    [[ht, f.tangential, "fan_t"], [hs, f.sagittal, "fan_s"]].forEach(([host, set, label]) => linePlot(host, {
      W, H: 190, xr: [-1, 1], yr: [-half, half], zeroX: true, zeroY: true, xLabel: `${t(label)} · ${t("pupil")}`, yLabel: unitOf(sys),
      curves: set.map((ys, w) => ({ x: fan.pupil, y: ys, color: colors[w] })),
    }));
  });
}

// ---------------- 场曲 / 畸变 ----------------

function renderField(view) {
  const sys = viz.system, fp = viz.field_plots;
  const colors = waveColors(fp.curvature || fp.distortion || []);
  const fieldUnit = /angle/i.test(fp.field_type || sys.fields.type) ? "°" : ` ${unitOf(sys)}`;
  const axis = (n, from) => Array.from({ length: n }, (_, k) => from + (fp.max_field - from) * k / (n - 1));
  view.innerHTML = `<div class="figure"><div class="fig-head"><div class="legend">${(fp.curvature || fp.distortion || []).map((w, i) => `<span><i style="background:${colors[i]}"></i>${Math.round(w.um * 1000)} nm</span>`).join("")}
    <span><i class="line"></i>${esc(t("tangential"))}</span><span><i class="line dash"></i>${esc(t("sagittal"))}</span></div></div>
    <div class="field-pair"><div id="curv-plot"></div><div id="dist-plot"></div></div></div>
    <div class="foot">${esc(foot("field_foot"))}</div>`;
  const W = Math.max(260, ($("curv-plot").clientWidth || 300));
  const H = Math.max(150, Math.min(460, view.clientHeight - 100));
  const yLabel = `${t("field_axis")} (${fieldUnit.trim()})`;
  if (fp.curvature) {
    const y = axis(fp.points, 0);
    const curves = fp.curvature.flatMap((w, i) => [{ x: w.tangential, y, color: colors[i] }, { x: w.sagittal, y, color: colors[i], dash: [6, 4] }]);
    const [lo, hi] = extent(curves.flatMap((c) => c.x)), m = Math.max(Math.abs(lo), Math.abs(hi)) * 1.1 || 1e-3;
    linePlot($("curv-plot"), { W, H, xr: [-m, m], yr: [0, fp.max_field], curves, zeroX: true, xLabel: `${t("curv")} (${unitOf(sys)})`, yLabel });
  } else $("curv-plot").innerHTML = `<div class="empty">${esc(t("gap", t("curv"), sys.backend) + (viz.errors?.field_curvature ? ` ${viz.errors.field_curvature}` : ""))}</div>`;
  if (fp.distortion) {
    const y = axis(fp.points, 0);
    const curves = fp.distortion.map((w, i) => ({ x: w.percent, y, color: colors[i] }));
    const [lo, hi] = extent(curves.flatMap((c) => c.x)), m = Math.max(Math.abs(lo), Math.abs(hi)) * 1.1 || 0.01;
    linePlot($("dist-plot"), { W, H, xr: [-m, m], yr: [0, fp.max_field], curves, zeroX: true, xLabel: t("dist"), yLabel });
  } else $("dist-plot").innerHTML = `<div class="empty">${esc(t("gap", t("dist"), sys.backend) + (viz.errors?.distortion ? ` ${viz.errors.distortion}` : ""))}</div>`;
}

// ---------------- 指标（按轮次）----------------

function renderMetrics(view) {
  const items = state.metric_items || [];
  const rounds = state.rounds || [];
  if (!items.length) { view.innerHTML = `<div class="empty">${esc(t("metrics_none"))}</div>`; return; }
  // 当前镜头的值：镜头改过（视图版本不同）或有操作在跑时标为过时 / live values: stale after a lens change or while an op runs
  metricsSeen = JSON.stringify([state.metrics, state.metric_items, !!state.progress]);
  const live = state.metrics || { items: {} };
  const slow = new Set(live.slow || []);
  const now = (id) => {
    const e = live.items?.[id];
    if (!e) return `<span class="stale" title="${esc(t("metric_pending"))}">–</span>`;
    const stale = staleEntry(e);
    const why = [stale ? t("metric_stale") : "", slow.has(id) ? t("metric_slow") : "", t("metric_src")[e.source] || e.source, e.error || ""].filter(Boolean).join(" · ");
    const text = e.value == null ? t("metric_no_value") : sig(e.value, 5);
    return `<span class="${stale ? "stale" : ""}" title="${esc(why)}">${esc(text)}${e.gap > 0 ? ` <span class="tag warn">${esc(fmtG(e.gap))}</span>` : e.gap === 0 ? ` <span class="tag ok">✓</span>` : ""}</span>`;
  };
  const bounds = (b) => (!b ? [] : "equals" in b ? [b.equals] : [b.min, b.max].filter((v) => v != null));
  const boundText = (b) => (!b ? "" : "equals" in b ? `= ${sig(b.equals, 4)}${b.tol ? ` ± ${sig(b.tol, 3)}` : ""}` : [b.min != null ? `≥ ${sig(b.min, 4)}` : "", b.max != null ? `≤ ${sig(b.max, 4)}` : ""].filter(Boolean).join(" · "));
  view.innerHTML = `<div class="metric-grid">${items.map((m, k) => `<div class="card"><h3>${esc(m.id)}${unitOfItem(m) ? ` <small>${esc(unitOfItem(m))}</small>` : ""}</h3>
      <p>${esc(m.missing ? t("metric_missing") : m.description || "")}${m.bound ? ` · ${esc(t("metric_bound"))} ${esc(boundText(m.bound))}` : ""}${m.direction ? ` · ${esc(m.direction)}` : ""}</p>
      <div class="metric-now">${now(m.id)}</div><div id="metric-plot-${k}"></div></div>`).join("")}</div>
    <div class="table-wrap"><table><tr><th>${esc(t("round"))}</th>${items.map((m) => `<th class="num">${esc(m.id)}</th>`).join("")}</tr>
      <tr><td>${esc(t("live"))}</td>${items.map((m) => `<td class="num">${now(m.id)}</td>`).join("")}</tr>
      ${rounds.slice().reverse().map((r) => `<tr><td>${t("select_round", r.round)}</td>${items.map((m) => `<td class="num">${r.values[m.id] == null ? esc(t("metric_no_value")) : sig(r.values[m.id], 5)}</td>`).join("")}</tr>`).join("")}</table></div>
    <div class="foot">${esc(t("metrics_foot"))}</div>`;
  items.forEach((m, k) => {
    const host = $(`metric-plot-${k}`);
    if (!rounds.some((r) => r.values[m.id] != null)) return;
    const xs = rounds.map((r) => r.round), ys = rounds.map((r) => r.values[m.id]);
    const lines = bounds(m.bound);
    const [lo, hi] = extent([...ys, ...lines]), padY = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1;
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    linePlot(host, { W: Math.max(240, host.clientWidth), H: 170, xr: [x0, x1 === x0 ? x0 + 1 : x1], yr: [lo - padY, hi + padY], xLabel: t("round"),
      curves: [...lines.map((v) => ({ x: [x0, x1 === x0 ? x0 + 1 : x1], y: [v, v], color: css("--wait"), dash: [6, 4] })), { x: xs, y: ys, color: css("--accent") }] });
  });
}

// ---------------- 起点与初筛 ----------------

async function renderStarts(view) {
  let data;
  try { data = await api("starts"); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (tab !== "starts") return;
  const gen = data.generated;
  if (!gen) { view.innerHTML = `<div class="empty">${esc(t("starts_none"))}</div>`; return; }
  const num = (v, n = 4) => (v == null || !isFinite(v) ? "–" : sig(v, n));
  const source = (p) => {
    const label = p.patent ? `${p.patent}${p.locator ? ` · ${p.locator}` : ""}` : p.book ? `${p.book}${p.page ? ` p.${p.page}` : ""}` : "–";
    return p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(label)}</a>` : esc(label);
  };
  const targets = gen.constraints?.values || {};
  const head = Object.entries(targets).map(([k, v]) => `${k} ${Array.isArray(v) ? v.map((x) => sig(x, 4)).join("/") : sig(v, 4)}`).join(" · ");
  const cands = gen.candidates.map((c) => {
    const fo = c.first_order || {}, extra = c.constraints?.extrapolation || [];
    const seidel = (c.seidel || []).map((v) => num(v, 3)).join(" / ") || "–";
    const bad = Object.values(c.errors || {}).join("; ");
    return `<tr><td><b>${esc(c.id)}</b><br><small>${esc(c.form || "")}</small></td><td>${source(c.provenance)}<br><small>${esc(c.provenance.validation || "")}</small></td>
      <td class="num">${num(c.provenance.scale)}</td><td>${esc(c.constraints?.tightest || "–")}${extra.length ? `<br><span class="tag warn">${esc(t("st_extra"))}</span> <small>${esc(extra.join("; "))}</small>` : ""}</td>
      <td class="num">${num(fo.efl)}</td><td class="num">${num(fo.bfl)}</td><td class="num">${num(fo.f_number, 3)}</td>
      <td class="num">${num(c.petzval?.radius_over_efl, 3)}</td><td class="num mono">${seidel}</td><td class="num">${c.incidence_max == null ? "–" : `${sig(c.incidence_max, 3)}°`}</td></tr>
      ${bad ? `<tr><td></td><td colspan="9" class="foot warn">${esc(bad)}</td></tr>` : ""}`;
  }).join("");
  const scans = data.scans.map((s) => {
    const title = s.optimised ? t("st_short", sig(s.budget_s, 3)) : t("st_screen");
    const rows = s.rows.map((r) => {
      const verdict = (t("st_verdict")[r.verdict] || r.verdict || (r.error ? t("st_verdict").computation : "–"));
      const why = (r.problems || []).join("; ") || r.error || "";
      return `<tr><td>${esc(r.id)}</td><td><span class="tag ${r.verdict === "valid" ? "ok" : r.verdict === "invalid" ? "" : "warn"}">${esc(verdict)}</span>${r.still_improving ? " ↘" : ""}<br><small>${esc(why)}</small></td>
        <td class="num">${fmtG(r.start_shortfall)} → ${fmtG(r.shortfall)}</td><td class="num">${r.numeric_warnings?.count ?? 0}</td></tr>`;
    }).join("");
    return `<h3 class="sub">${esc(title)} · ${esc(s.folder)}${s.finished ? "" : ` · ${esc(t("st_running"))}`}</h3>
      ${rows ? `<table><tr><th>#</th><th>${esc(t("state"))}</th><th class="num">${esc(t("st_g"))}</th><th class="num">${esc(t("st_warn"))}</th></tr>${rows}</table>` : ""}
      ${s.page ? `<div class="foot">${esc(t("st_page"))}: ${esc(s.page)}</div>` : ""}`;
  }).join("");
  const listOf = (items, key) => items.length ? `<h3 class="sub">${esc(t(key))}</h3><ul class="plain">${items.map((x) => `<li><b>${esc(x.id)}</b> · ${esc((x.reasons || [x.error]).join("; "))}</li>`).join("")}</ul>` : "";
  view.innerHTML = `<div class="table-wrap starts"><div class="foot">${esc(gen.folder)} · ${esc(head)}</div>
    <table><tr><th>${esc(t("st_form"))}</th><th>${esc(t("st_source"))}</th><th class="num">${esc(t("st_scale"))}</th><th>${esc(t("st_tight"))}</th>
      <th class="num">${esc(t("st_efl"))}</th><th class="num">${esc(t("st_bfl"))}</th><th class="num">${esc(t("st_fno"))}</th><th class="num">${esc(t("st_petz"))}</th>
      <th class="num">${esc(t("st_seidel"))}</th><th class="num">${esc(t("st_aoi"))}</th></tr>${cands}</table>
    ${listOf(gen.not_evaluated, "st_noteval")}${listOf(gen.excluded, "st_excluded")}${scans}
    <div class="foot">${esc(t("st_foot"))}</div></div>`;
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

// Lens Data 卡：与镜头卡共用一个悬停状态（ldHover 由布局设置）；数值相同不重画，换轮次时数字逐位滚动
// the Lens Data card shares one hover state with the layout (ldHover is set by it); numbers roll when the round changes
function renderLensData(rows) {
  const host = $("ld");
  $("ld-title").textContent = t("lens_data");
  $("ld-note").textContent = t("ld_solve");
  $("ld-note").title = t("hover_row");
  if (!rows?.length) { host.innerHTML = `<div class="empty">${esc(t("no_ld"))}</div>`; host.dataset.shape = ""; return; }
  const num = (v) => (v == null ? "" : !isFinite(v) ? "∞" : sig(v, 6));
  const mark = (s, k) => { const v = (s.solves || {})[k]; return !v || ["Fixed", "Automatic", "None"].includes(v) ? "" : v === "Variable" ? "V" : v[0]; };
  const cells = (s) => [num(s.radius), num(s.thickness), num(s.semi_diameter), num(s.conic)];
  // 表的结构（行、面型、材料、求解标记）不变时只滚动数字 / same structure: only roll the numbers
  const shape = JSON.stringify([lang, rows.map((s) => [s.index, s.is_stop, s.type, s.comment, s.material, ...["radius", "thickness", "semi_diameter", "conic", "material"].map((k) => mark(s, k))])]);
  if (host.dataset.shape !== shape) {
    host.dataset.shape = shape;
    const sv = (s, k) => (mark(s, k) ? `<span class="sv">${mark(s, k)}</span>` : "");
    host.innerHTML = `<table><tr><th>#</th><th>${esc(t("ld_type"))}</th><th>${esc(t("ld_comment"))}</th><th class="num">${esc(t("ld_radius"))}</th>
      <th class="num">${esc(t("ld_thickness"))}</th><th>${esc(t("ld_material"))}</th><th class="num">${esc(t("ld_sd"))}</th><th class="num">${esc(t("ld_conic"))}</th></tr>
      ${rows.map((s) => `<tr data-surf="${s.index}"><td>${s.index}${s.is_stop ? " STO" : ""}</td><td>${esc(s.type)}</td><td>${esc(s.comment)}</td>
        <td class="num"><span class="v"></span>${sv(s, "radius")}</td><td class="num"><span class="v"></span>${sv(s, "thickness")}</td>
        <td>${esc(s.material)}${sv(s, "material")}</td><td class="num"><span class="v"></span>${sv(s, "semi_diameter")}</td>
        <td class="num"><span class="v"></span>${sv(s, "conic")}</td></tr>`).join("")}</table>`;
    const table = host.querySelector("table");
    table.addEventListener("pointerover", (e) => { const tr = e.target.closest("tr[data-surf]"); if (tr) ldHover?.(Number(tr.dataset.surf)); });
    table.addEventListener("pointerleave", () => ldHover?.(null));
  }
  host.querySelectorAll("tr[data-surf]").forEach((tr, i) => tr.querySelectorAll(".v").forEach((el, k) => odo(el, cells(rows[i])[k])));
}

// 布局图的显示选项按浏览器记住；读写失败（隐私窗口等）就用默认值
const LAYOUT_OPTS_KEY = "saos.layout";
const layoutOpts = { grid: true, labels: false };
try { Object.assign(layoutOpts, JSON.parse(localStorage.getItem(LAYOUT_OPTS_KEY) || "{}")); } catch { /* 用默认值 */ }
const saveLayoutOpts = () => { try { localStorage.setItem(LAYOUT_OPTS_KEY, JSON.stringify(layoutOpts)); } catch { /* 不记住 */ } };
let ruler = null;

// 截线上纵坐标为 y 处的 z（线性插值）；截线不过 y 时返回 null
function zAtY(profile, y) {
  for (let i = 0; i < profile.length - 1; i++) {
    const a = profile[i], b = profile[i + 1];
    if (a[1] !== b[1] && (a[1] - y) * (b[1] - y) <= 1e-12) return a[2] + (b[2] - a[2]) * (y - a[1]) / (b[1] - a[1]);
  }
  return null;
}

// 轴对称系统的边缘厚：在两面子午截线共同覆盖的上下两端取厚度，取较小值（截线按面孔径取，可能不对称）
function edgeThickness(e) {
  const ys = (p) => p.map((q) => q[1]);
  const top = Math.min(Math.max(...ys(e.front.profile_y)), Math.max(...ys(e.back.profile_y)));
  const bottom = Math.max(Math.min(...ys(e.front.profile_y)), Math.min(...ys(e.back.profile_y)));
  const dir = Math.sign(e.back.vertex[2] - e.front.vertex[2]) || 1;
  const vals = [top, bottom].map((y) => { const zf = zAtY(e.front.profile_y, y), zb = zAtY(e.back.profile_y, y); return zf == null || zb == null ? null : (zb - zf) * dir; });
  const ok = vals.filter((v) => v != null);
  return ok.length ? Math.min(...ok) : null;
}

function segDist(px, py, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L = dx * dx + dy * dy;
  const k = L ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / L)) : 0;
  return Math.hypot(px - a[0] - k * dx, py - a[1] - k * dy);
}

// 区间内的整格位置；格号取整再乘步长，避免累加误差，-0 归零
const ticks = (a, b, step) => { const out = []; for (let k = Math.ceil(a / step); k <= Math.floor(b / step); k++) out.push(k * step || 0); return out; };
const fmtLen = (v, step) => v.toFixed(Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9))));

// 2D 剖面：把点换到剖面平面的坐标（离面距离, 纵轴, 横轴），之后照旧按 (z, y) 画子午截线和 px = 0 的光线扇。
// 轴对称系统的剖面就是全局 YZ 面，坐标不变；折转系统用服务端找到的对称平面（section.py）
function toSection(v, sec) {
  const dot = (a, p) => a[0] * p[0] + a[1] * p[1] + a[2] * p[2];
  const P = (p) => [dot(sec.normal, p) - sec.offset, dot(sec.v, p), dot(sec.u, p)];
  const D = (d) => [dot(sec.normal, d), dot(sec.v, d), dot(sec.u, d)];
  // 截线按纵坐标从下到上排，标签和挡光片按首尾取上下端
  const upward = (pts) => (pts && pts.length && pts[0][1] > pts[pts.length - 1][1] ? pts.slice().reverse() : pts);
  const surfaces = v.surfaces.map((s) => {
    if (!s.vertex) return s;
    const key = sec.profiles[s.index] || "profile_y", other = key === "profile_y" ? "profile_x" : "profile_y";
    return { ...s, vertex: P(s.vertex), profile_y: upward(s[key]?.map(P)), profile_x: s[other]?.map(P), rim: s.rim?.map(P) };
  });
  const swap = sec.fan !== "px";
  const rays = v.layout.rays.map((r) => ({ ...r, px: swap ? r.py : r.px, py: swap ? r.px : r.py, points: r.points.map(P), start_dir: r.start_dir && D(r.start_dir) }));
  return { ...v, surfaces, layout: { ...v.layout, rays } };
}

function renderLayout(view) {
  const sec = viz.section;
  // 找不到对称平面的系统（各面朝向不共面）画不出有意义的剖面，只给 3D / no plane of symmetry: 3D only
  const only3d = !!viz.system.non_axial && !sec;
  if (only3d) dim = "3d";
  const shown = dim === "2d" && sec ? toSection(viz, sec) : viz;
  const sys = shown.system;
  // 浅拷贝每个面：过渡动画会替换坐标数组，不能改到缓存里的导出 / shallow copies: the tween swaps coordinate arrays, never the cached export
  const surfs = shown.surfaces.map((s) => ({ ...s }));
  const shownKey = roundShown;
  const axisH = dim === "2d" && sec ? sec.u_name : "z", axisV = dim === "2d" && sec ? sec.v_name : "y";
  $("dim-seg").querySelectorAll("button").forEach((x) => { x.classList.toggle("on", x.dataset.dim === dim); x.disabled = only3d && x.dataset.dim === "2d"; x.title = x.disabled ? t("only_3d") : ""; });
  const clash = clashOf(viz);
  const title = roundShown === "live" ? `${t("live")} · ${viz.lens || ""}${viz.backend === "rtc" ? " · rtc" : ""}${viz.dirty ? ` · ${t("live_unsaved")}` : ""}${viz.layout?.rays_paused ? ` · ${t("live_optimising")}` : ""}` : t("select_round", roundShown);
  const els = elements(surfs);
  const mats = [...new Set(els.map((e) => e.material.toLowerCase()))];
  const matColor = (m) => MATERIAL_COLORS[mats.indexOf(m.toLowerCase()) % MATERIAL_COLORS.length];
  const image = surfs[surfs.length - 1];
  const stopSd = sys.stop_semi_diameter;
  const unit = sys.lens_units === "Millimeters" ? "mm" : sys.lens_units;
  const rows = new Map((viz.lens_data || []).map((r) => [r.index, r]));
  // 卡头写这是哪一轮；图例一行在上，材料图例并进同一行；来源说明一行在下，全文在悬停提示里
  // the card header names the round; one legend row above; the provenance line below with its full text on hover
  $("lens-src").textContent = title;
  $("lens-src").title = title;
  const footText = `${viz.errors?.sag ? t("sag_flat") + " " : ""}${foot(viz.backend === "rtc" ? "layout_foot_rtc" : "layout_foot")}${viz.unverified?.length ? ` ${t("unverified", viz.unverified)}` : ""}`;
  view.innerHTML = `<div class="layout-view"><div class="figure">
    <div class="fig-head"><div class="legend">${sys.fields.items.map((f, i) => `<span><i class="line" style="border-color:${FIELD_COLORS[i % FIELD_COLORS.length]}"></i>${esc(fieldLabel(sys, i))}</span>`).join("")}
      ${mats.map((m) => `<span><span class="swatch" style="background:${matColor(m)}55;border-color:${matColor(m)}"></span>${esc(m)}</span>`).join("")}</div>
      <span class="info">λ ${sig(sys.wavelengths[sys.primary_wave - 1].um, 4)} µm${stopSd ? ` · STOP Ø ${(2 * stopSd).toFixed(2)} ${unit}` : ""}${image?.vertex ? ` · ${esc(t("image_z"))} ${image.vertex[2].toFixed(2)} ${unit}` : ""}</span></div>
    ${clash.lines.length ? `<details class="clash"><summary><b>${esc(t("clash_title"))}</b> · ${clash.lines.length} · ${esc(clash.lines[0])}</summary>${clash.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</details>` : ""}
    <div class="stage"><canvas></canvas>
      <div class="stage-tools"><button data-opt="grid">${esc(t("opt_grid"))}</button><button data-opt="labels">${esc(t("opt_labels"))}</button><button data-act="png" title="${esc(t("png_title"))}">PNG</button></div>
      <span class="zoom-chip"></span><span class="readout"></span><div class="tip" hidden></div></div></div>
    <div class="foot ${viz.errors?.sag ? "warn" : ""}" title="${esc(footText)}"><span id="layout-hint"></span> · ${esc(footText)}</div></div>`;
  renderLensData(viz.lens_data);
  const stage = view.querySelector(".stage");
  const canvas = stage.querySelector("canvas");
  const zoomChip = stage.querySelector(".zoom-chip");
  const readout = stage.querySelector(".readout");
  const tip = stage.querySelector(".tip");
  const tools = stage.querySelector(".stage-tools");
  const syncTools = () => tools.querySelectorAll("[data-opt]").forEach((b) => {
    b.classList.toggle("on", !!layoutOpts[b.dataset.opt]);
    b.hidden = b.dataset.opt === "grid" && dim !== "2d";
  });

  // 所有点用于取景；起点按第一面方向往回延长一段
  const verts = surfs.filter((s) => s.vertex).map((s) => s.vertex);
  const track = verts.length > 1 ? Math.hypot(...verts[verts.length - 1].map((v, k) => v - verts[0][k])) : 10;
  const rays = shown.layout.rays.map((r) => {
    const pts = r.points.slice();
    if (r.start_dir) pts.unshift(pts[0].map((v, k) => v - r.start_dir[k] * track * 0.28));
    return { ...r, pts };
  });
  const all = [...surfs.flatMap((s) => s.rim || []), ...rays.flatMap((r) => r.pts)];
  const lo = [0, 1, 2].map((k) => Math.min(...all.map((p) => p[k]))), hi = [0, 1, 2].map((k) => Math.max(...all.map((p) => p[k])));
  const center = lo.map((v, k) => (v + hi[k]) / 2);
  let frame = null;
  let hover = null;

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
    // 填底色而不是清空，导出的 PNG 才有背景
    g.fillStyle = css("--plot-bg"); g.fillRect(0, 0, W, H);
    syncTools();
    const yaw = dim === "2d" ? 0 : camera.yaw, pitch = dim === "2d" ? 0 : camera.pitch;
    const P = all.map((p) => project(p, yaw, pitch));
    const spanU = Math.max(...P.map((p) => p[0])) - Math.min(...P.map((p) => p[0])) || 1;
    const spanV = Math.max(...P.map((p) => p[1])) - Math.min(...P.map((p) => p[1])) || 1;
    const scale = Math.min((W - 80) / spanU, (H - 60) / spanV) * camera.zoom;
    const midU = (Math.max(...P.map((p) => p[0])) + Math.min(...P.map((p) => p[0]))) / 2;
    const midV = (Math.max(...P.map((p) => p[1])) + Math.min(...P.map((p) => p[1]))) / 2;
    const S = (p) => { const [u, v] = project(p, yaw, pitch); return [W / 2 + camera.panX + (u - midU) * scale, H / 2 + camera.panY - (v - midV) * scale]; };
    // 2D 下屏幕点换回全局 (z, y)
    const inv = (x, y) => [(x - W / 2 - camera.panX) / scale + midU + center[2], -(y - H / 2 - camera.panY) / scale + midV + center[1]];
    const at = (z, y) => [center[0], y, z];
    frame = { S, inv, W, H, scale };
    $("layout-hint").textContent = t(dim === "2d" ? "layout_hint_2d" : "layout_hint_3d");
    zoomChip.textContent = camera.zoom === 1 ? "" : `${Math.round(camera.zoom * 100)} %`;
    const placed = [];
    const overlaps = (box) => placed.some((b) => b.box[0] < box[2] && box[0] < b.box[2] && b.box[1] < box[3] && box[1] < b.box[3]);
    const line = (pts, close = false) => { g.beginPath(); pts.forEach((p, i) => { const [x, y] = S(p); i ? g.lineTo(x, y) : g.moveTo(x, y); }); if (close) g.closePath(); };

    // 网格（2D）：主格约 90 px，次格 4 或 5 等分；刻度是全局 z、y
    if (dim === "2d" && layoutOpts.grid) {
      const major = niceStep(90 / scale);
      const mant = Math.round(major / Math.pow(10, Math.floor(Math.log10(major) + 1e-9)));
      const minor = major / (mant === 2 ? 4 : 5);
      const [z0, y1] = inv(0, 0), [z1, y0] = inv(W, H);
      const gridLines = (step, color, width) => {
        g.strokeStyle = color; g.lineWidth = width; g.beginPath();
        ticks(z0, z1, step).forEach((z) => { const x = Math.round(S(at(z, 0))[0]) + 0.5; g.moveTo(x, 0); g.lineTo(x, H); });
        ticks(y0, y1, step).forEach((y) => { const v = Math.round(S(at(0, y))[1]) + 0.5; g.moveTo(0, v); g.lineTo(W, v); });
        g.stroke();
      };
      g.globalAlpha = 0.45; gridLines(minor, css("--grid"), 1); g.globalAlpha = 1;
      gridLines(major, css("--grid"), 1);
      g.font = `10px ${css("--mono")}`; g.fillStyle = css("--text-3");
      g.textAlign = "center"; g.textBaseline = "bottom";
      ticks(z0, z1, major).forEach((z) => { const x = S(at(z, 0))[0]; if (x > 40 && x < W - 50) g.fillText(fmtLen(z, major), x, H - 3); });
      g.textAlign = "left"; g.textBaseline = "middle";
      ticks(y0, y1, major).forEach((y) => { const v = S(at(0, y))[1]; if (v > 18 && v < H - 18) g.fillText(fmtLen(y, major), 4, v); });
      g.textAlign = "right"; g.textBaseline = "bottom"; g.fillText(`${axisH} (${unit})`, W - 6, H - 3);
      g.textAlign = "left"; g.textBaseline = "top"; g.fillText(`${axisV} (${unit})`, 4, 4);
    }
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
      if (overlaps(box)) return;
      placed.push({ box, text: `L${k + 1}`, x: lx, y: ly, baseline: "bottom" });
    });
    // 面号：标在每面下端
    if (layoutOpts.labels) {
      surfs.forEach((s) => {
        if (!s.profile_y) return;
        // 下端出了画布就贴着画布底部（让开刻度和比例尺）
        const [x, y0] = S(s.profile_y[0]), y = Math.min(y0 + 4, H - 48);
        const text = `${s.index}`, w = 4 + 6.5 * text.length, box = [x - w / 2, y, x + w / 2, y + 13];
        if (x < 0 || x > W || y < 0 || overlaps(box)) return;
        placed.push({ box, text, x, y, baseline: "top", small: true });
      });
    }
    g.textAlign = "center"; g.lineWidth = 3; g.lineJoin = "round"; g.strokeStyle = css("--plot-bg");
    placed.forEach((p) => {
      g.font = p.small ? `11px ${css("--mono")}` : `600 12px ${css("--font")}`;
      g.fillStyle = p.small ? css("--text-3") : css("--text-2"); g.textBaseline = p.baseline;
      g.strokeText(p.text, p.x, p.y); g.fillText(p.text, p.x, p.y);
    });
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
    // 悬停的面
    const hs = hover != null && surfs.find((s) => s.index === hover);
    if (hs?.profile_y) {
      g.strokeStyle = css("--accent"); g.lineWidth = 2.6;
      line(hs.profile_y); g.stroke();
      if (dim === "3d" && hs.rim) { line(hs.rim, true); g.stroke(); }
    }
    // 测距（2D）
    if (dim === "2d" && ruler) {
      const [ax, ay] = S(at(...ruler.a)), [bx, by] = S(at(...ruler.b));
      g.strokeStyle = css("--accent"); g.fillStyle = css("--accent"); g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
      [[ax, ay, ruler.snapA], [bx, by, ruler.snapB]].forEach(([x, y, snapped]) => { g.beginPath(); g.arc(x, y, snapped ? 4 : 2.5, 0, 2 * Math.PI); snapped ? g.stroke() : g.fill(); });
      const dz = ruler.b[0] - ruler.a[0], dy = ruler.b[1] - ruler.a[1];
      const text = t("ruler", fmtLen(Math.hypot(dz, dy), 1e-3), fmtLen(dz, 1e-3), fmtLen(dy, 1e-3), unit);
      g.font = `12px ${css("--mono")}`; g.textAlign = "left"; g.textBaseline = "bottom";
      const tx = Math.min(Math.max((ax + bx) / 2 + 8, 4), W - g.measureText(text).width - 6), ty = Math.max((ay + by) / 2 - 6, 16);
      g.lineWidth = 3; g.strokeStyle = css("--plot-bg"); g.strokeText(text, tx, ty); g.fillText(text, tx, ty);
    }
    // 比例尺：正交投影，3D 下表示屏幕平面内的长度
    {
      const len = niceStep(110 / scale), px = len * scale, x0 = 46, y0 = H - (dim === "2d" && layoutOpts.grid ? 30 : 18);
      g.strokeStyle = css("--text-2"); g.fillStyle = css("--text-2"); g.lineWidth = 1.5;
      g.beginPath(); g.moveTo(x0, y0 - 5); g.lineTo(x0, y0); g.lineTo(x0 + px, y0); g.lineTo(x0 + px, y0 - 5); g.stroke();
      g.font = `11px ${css("--mono")}`; g.textAlign = "center"; g.textBaseline = "bottom";
      g.lineWidth = 3; g.strokeStyle = css("--plot-bg");
      const label = `${fmtLen(len, len)} ${unit}`;
      g.strokeText(label, x0 + px / 2, y0 - 3); g.fillText(label, x0 + px / 2, y0 - 3);
    }
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

  // 离光标 8 px 内最近的面（按子午截线，3D 加边缘一圈）
  function surfaceAt(mx, my) {
    let best = null, bestD = 8;
    surfs.forEach((s) => {
      if (!s.profile_y) return;
      const paths = dim === "3d" && s.rim ? [s.profile_y, s.rim] : [s.profile_y];
      paths.forEach((pts) => {
        const q = pts.map(frame.S);
        for (let i = 0; i < q.length - 1; i++) { const d = segDist(mx, my, q[i], q[i + 1]); if (d < bestD) { bestD = d; best = s.index; } }
      });
    });
    return best;
  }

  // 测距端点吸附到 8 px 内的面顶点或截线采样点
  function snapAt(mx, my) {
    let best = null, bestD = 8;
    surfs.forEach((s) => (s.profile_y || []).forEach((p) => {
      const [x, y] = frame.S(p), d = Math.hypot(x - mx, y - my);
      if (d < bestD) { bestD = d; best = [p[2], p[1]]; }
    }));
    return best ? { p: best, snapped: true } : { p: frame.inv(mx, my), snapped: false };
  }

  function tipText(index) {
    const s = surfs.find((x) => x.index === index), r = rows.get(index) || {};
    const num = (v) => (v == null ? "–" : !isFinite(v) ? "∞" : sig(v, 5));
    const lines = [`S${index}${s.is_stop ? " STO" : ""} · ${r.type || s.type}${(r.comment || s.comment) ? ` · ${r.comment || s.comment}` : ""}`,
      `R ${num(r.radius)} · T ${num(r.thickness)} · ${r.material || s.material || t("tip_air")} · SD ${num(r.semi_diameter ?? s.semi_diameter)}`];
    els.forEach((e, k) => {
      if (e.front !== s && e.back !== s) return;
      const ct = rows.get(e.front.index)?.thickness;
      const et = sys.non_axial && !(dim === "2d" && sec) ? null : edgeThickness(e);
      lines.push(t("tip_lens", k + 1, e.material, ct == null ? "–" : ct.toFixed(3), et == null ? "–" : et.toFixed(3), unit) + (et != null && et <= 0 ? " ⚠" : ""));
    });
    return lines;
  }

  function showTip(mx, my) {
    if (hover == null) { tip.hidden = true; return; }
    tip.innerHTML = tipText(hover).map((l) => `<div>${esc(l)}</div>`).join("");
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = `${Math.min(mx + 14, frame.W - w - 6)}px`;
    tip.style.top = `${my + 16 + h > frame.H ? my - h - 10 : my + 16}px`;
  }

  // 布局与 Lens Data 表共用一个悬停状态：指向布局里的面标出表格行，指向表格行高亮布局里的面（借鉴 Optiland GUI 的 SurfaceInteractionState）
  // the layout and the Lens Data table share one hover state, after the Optiland GUI's SurfaceInteractionState
  // Lens Data 表在另一张卡里，经 ldHover 接进来 / the table lives in its own card and calls in through ldHover
  const markRow = (index) => $("ld").querySelectorAll("tr[data-surf]").forEach((tr) => tr.classList.toggle("hl", Number(tr.dataset.surf) === index));
  const setHover = (index) => { if (index === hover) return; hover = index; markRow(index); draw(); };
  ldHover = (index) => { if (!drag) setHover(index); };

  // 结构不变（面、截线点数、光线条数都一样）时，镜片与光线从上次画的位置过渡到新位置；只是动画，终点就是导出的坐标
  // with the same structure, lenses and rays glide from where they were drawn to the new export; animation only, it ends on the export
  const GEO = ["vertex", "profile_y", "profile_x", "rim"];
  const geomKey = JSON.stringify([dim, surfs.map((s) => [s.index, ...GEO.map((k) => (!s[k] ? 0 : k === "vertex" ? 1 : s[k].length))]), rays.map((r) => [r.field, r.px, r.pts.length])]);
  const target = { surfs: surfs.map((s) => Object.fromEntries(GEO.map((k) => [k, s[k]]))), rays: rays.map((r) => r.pts) };
  const lerpPts = (a, b, k) => b.map((p, i) => p.map((v, j) => a[i][j] + (v - a[i][j]) * k));
  const snapshot = () => ({ key: geomKey, surfs: surfs.map((s) => Object.fromEntries(GEO.map((k) => [k, s[k]]))), rays: rays.map((r) => r.pts) });
  const blend = (from, k) => {
    surfs.forEach((s, i) => GEO.forEach((g) => {
      const a = from.surfs[i][g], b = target.surfs[i][g];
      if (a && b) s[g] = g === "vertex" ? b.map((v, j) => a[j] + (v - a[j]) * k) : lerpPts(a, b, k);
    }));
    rays.forEach((r, i) => { r.pts = lerpPts(from.rays[i], target.rays[i], k); });
  };
  cancelAnimationFrame(renderLayout.anim);
  const prev = renderLayout.geom;
  if (prev && prev.key === geomKey && !reduceMotion) {
    blend(prev, 0);
    const t0 = performance.now();
    // rAF 的时间戳可能早于 t0：进度夹到 [0, 1]，否则缓动会外推 / rAF stamps can precede t0: clamp the progress
    const step = (now) => {
      const k = Math.min(1, Math.max(0, (now - t0) / 700));
      blend(prev, 1 - Math.pow(1 - k, 4));
      renderLayout.geom = snapshot();
      draw();
      if (k < 1) renderLayout.anim = requestAnimationFrame(step);
    };
    renderLayout.anim = requestAnimationFrame(step);
  }
  renderLayout.geom = snapshot();

  draw();
  let drag = null;
  const pos = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.oncontextmenu = (e) => e.preventDefault();
  canvas.onpointerdown = (e) => {
    // 2D：左键平移，Shift 或右键测距；3D：左键旋转，Shift 或右键平移
    const alt = e.shiftKey || e.button === 2;
    const measure = dim === "2d" && alt;
    drag = { x: e.clientX, y: e.clientY, moved: false, measure, pan: !measure && (dim === "2d" || alt) };
    if (measure) { const [mx, my] = pos(e); const a = snapAt(mx, my); ruler = { a: a.p, b: a.p, snapA: a.snapped, snapB: a.snapped }; }
    canvas.classList.toggle("panning", drag.pan);
    canvas.classList.toggle("measuring", measure);
    hover = null; tip.hidden = true;
    canvas.setPointerCapture(e.pointerId);
  };
  const showReadout = (mx, my) => {
    if (dim === "2d" && frame) { const [z, y] = frame.inv(mx, my), step = 1 / frame.scale; readout.textContent = `${axisH} ${fmtLen(z, step)} · ${axisV} ${fmtLen(y, step)} ${unit}`; }
    else readout.textContent = "";
  };
  canvas.onpointermove = (e) => {
    const [mx, my] = pos(e);
    showReadout(mx, my);
    if (!drag) {
      setHover(surfaceAt(mx, my));
      showTip(mx, my);
      return;
    }
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 0) drag.moved = true;
    if (drag.measure) { const b = snapAt(mx, my); ruler.b = b.p; ruler.snapB = b.snapped; }
    else if (drag.pan) { camera.panX += dx; camera.panY += dy; }
    else {
      camera.yaw += dx * 0.008;
      camera.pitch = Math.max(-1.45, Math.min(1.45, camera.pitch + dy * 0.008));
    }
    drag.x = e.clientX; drag.y = e.clientY;
    draw();
  };
  canvas.onpointerup = () => {
    // 不拖动的单击清掉测距线
    if (drag && !drag.moved && !drag.measure && ruler) { ruler = null; draw(); }
    if (drag?.measure && !drag.moved) { ruler = null; draw(); }
    drag = null; canvas.classList.remove("panning", "measuring");
  };
  canvas.onpointerleave = () => { if (drag) return; readout.textContent = ""; tip.hidden = true; setHover(null); };
  canvas.onwheel = (e) => {
    e.preventDefault();
    // 以光标为中心缩放：光标下的点缩放前后不动
    const old = camera.zoom;
    camera.zoom = Math.max(0.3, Math.min(200, old * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const f = camera.zoom / old, [mx, my] = pos(e), cx = mx - frame.W / 2, cy = my - frame.H / 2;
    camera.panX = cx - (cx - camera.panX) * f; camera.panY = cy - (cy - camera.panY) * f;
    draw();
    showReadout(mx, my);
    tip.hidden = true;
  };
  canvas.ondblclick = () => { Object.assign(camera, CAMERA0); draw(); };
  tools.onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.opt) { layoutOpts[b.dataset.opt] = !layoutOpts[b.dataset.opt]; saveLayoutOpts(); draw(); return; }
    canvas.toBlob((blob) => {
      if (!blob) return;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${JOB}-${shownKey === "live" ? "live" : t("select_round", shownKey)}-${dim}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  };
  renderLayout.redraw = draw;
  renderLayout.ro?.disconnect();
  renderLayout.ro = new ResizeObserver(() => draw());
  renderLayout.ro.observe(stage);
}


// ---------------- 非序列 ----------------

function nscColor(o, mats) {
  if (o.kind === "source") return "#e5a00d";
  if (o.kind === "detector") return "#e0705f";
  return MATERIAL_COLORS[Math.max(0, mats.indexOf((o.material || "").toLowerCase())) % MATERIAL_COLORS.length];
}

function renderNscLayout(view) {
  const objs = viz.objects || [];
  const sys = viz.system;
  const unit = sys.lens_units === "Millimeters" ? "mm" : sys.lens_units;
  const mats = [...new Set(objs.filter((o) => o.kind === "object").map((o) => (o.material || "").toLowerCase()))];
  const clash = (viz.interference?.overlaps || []).map((o) => t("nsc_overlap", o.objects[0], o.objects[1], sig(o.depth, 2)));
  const undrawn = objs.filter((o) => o.undrawn).map((o) => o.index);
  const data = new Map((viz.object_data || []).map((o) => [o.index, o]));
  const vpText = (vp) => (!vp || vp.model === "None" ? "—" : [vp.dll || vp.model, vp.mean_path != null ? `mfp ${sig(vp.mean_path, 4)}` : "", ...Object.entries(vp.params || {}).map(([k, v]) => `${k} ${sig(v, 4)}`)].filter(Boolean).join(" · "));
  const title = roundShown === "live" ? `${t("live")} · ${viz.lens || ""}${viz.dirty ? ` · ${t("live_unsaved")}` : ""}` : t("select_round", roundShown);
  // 只列改过默认设置的面：反射/吸收、膜层、面散射
  const faceText = (d) => (d?.faces || []).filter((f) => f.is !== "default" || f.coating || f.profile || f.scatter?.model !== "none").map((f) => `${t("face_word")} ${f.face}: ${[
    f.is !== "default" ? t("face_is")[f.is] || f.is : "", f.coating || "", f.profile ? `profile ${f.profile}` : "",
    f.scatter && f.scatter.model !== "none" ? [f.scatter.model, f.scatter.fraction != null ? sig(f.scatter.fraction, 3) : "", f.scatter.reflect || f.scatter.transmit || f.scatter.dll || ""].filter(Boolean).join(" ") : ""].filter(Boolean).join(" · ")}`).join("; ") || "—";
  const [h0, h1, h2, h3, h4, h5] = t("th_obj");
  $("lens-src").textContent = title;
  const nscFoot = `${t("nsc_foot")}${undrawn.length ? ` ${t("nsc_undrawn")}${undrawn.join(", ")}` : ""}`;
  view.innerHTML = `<div class="layout-view"><div class="figure">
    <div class="fig-head"><div class="legend">${mats.map((m) => `<span><span class="swatch" style="background:${MATERIAL_COLORS[mats.indexOf(m) % MATERIAL_COLORS.length]}55;border-color:${MATERIAL_COLORS[mats.indexOf(m) % MATERIAL_COLORS.length]}"></span>${esc(m || "—")}</span>`).join("")}
      <span><i class="line" style="border-color:#e5a00d"></i>${esc(t("kind_source"))}</span><span><i class="line" style="border-color:#e0705f"></i>${esc(t("kind_detector"))}</span></div>
      <span class="info">${esc(t("nsc_title"))} · λ ${sys.wavelengths.map((w) => sig(w.um, 4)).join(", ")} µm</span></div>
    ${clash.length ? `<div class="clash"><b>${esc(t("clash_title"))}</b>${clash.map((l) => `<div>${esc(l)}</div>`).join("")}</div>` : ""}
    <div class="stage"><canvas></canvas><span class="zoom-chip"></span></div></div>
    <div class="foot" title="${esc(nscFoot)}">${esc(t(dim === "2d" ? "layout_hint_2d" : "layout_hint_3d"))} · ${esc(nscFoot)}</div></div>`;
  // 物体表放进 Lens Data 卡 / the object table goes into the Lens Data card
  $("ld-title").textContent = t("ld_objects"); $("ld-note").textContent = ""; $("ld").dataset.shape = "";
  $("ld").innerHTML = `<table><tr><th>${h0}</th><th>${esc(h1)}</th><th>${esc(h2)}</th><th>${esc(h3)}</th><th>${esc(h4)}</th><th>${esc(h5)}</th></tr>
      ${objs.map((o) => `<tr><td>${o.index}</td><td>${esc(o.type_label || o.type)}${data.get(o.index)?.file ? ` · ${esc(data.get(o.index).file)}` : ""}</td><td>${esc(o.comment)}</td><td>${esc(o.kind === "object" ? o.material || "—" : "—")}</td><td>${esc(vpText(data.get(o.index)?.volume_physics))}</td><td>${esc(faceText(data.get(o.index)))}</td></tr>`).join("")}
    </table>`;
  ldHover = null; renderLayout.geom = null;
  $("dim-seg").querySelectorAll("button").forEach((x) => { x.classList.toggle("on", x.dataset.dim === dim); x.disabled = false; x.title = ""; });
  const stage = view.querySelector(".stage"), canvas = stage.querySelector("canvas"), zoomChip = stage.querySelector(".zoom-chip");
  const lines = (o) => (dim === "2d" ? o.section || [] : o.edges || []);
  const all = objs.flatMap((o) => [...(o.edges || []).flat(), ...(o.point ? [o.point] : [])]);
  if (!all.length) { stage.innerHTML = `<div class="empty">${esc(t("nsc_undrawn"))}${undrawn.join(", ")}</div>`; return; }
  const center = [0, 1, 2].map((k) => (Math.min(...all.map((p) => p[k])) + Math.max(...all.map((p) => p[k]))) / 2);
  const project = (p, yaw, pitch) => {
    const u = p[2] - center[2], v = p[1] - center[1], w = p[0] - center[0];
    const u1 = u * Math.cos(yaw) + w * Math.sin(yaw), w1 = -u * Math.sin(yaw) + w * Math.cos(yaw);
    return [u1, v * Math.cos(pitch) - w1 * Math.sin(pitch)];
  };
  function draw() {
    const W = stage.clientWidth, H = stage.clientHeight, dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = css("--plot-bg"); g.fillRect(0, 0, W, H);
    const yaw = dim === "2d" ? 0 : camera.yaw, pitch = dim === "2d" ? 0 : camera.pitch;
    const P = all.map((p) => project(p, yaw, pitch));
    const lo = [0, 1].map((k) => Math.min(...P.map((q) => q[k]))), hi = [0, 1].map((k) => Math.max(...P.map((q) => q[k])));
    const scale = Math.min((W - 80) / (hi[0] - lo[0] || 1), (H - 60) / (hi[1] - lo[1] || 1)) * camera.zoom;
    const S = (p) => { const [u, v] = project(p, yaw, pitch); return [W / 2 + camera.panX + (u - (lo[0] + hi[0]) / 2) * scale, H / 2 + camera.panY - (v - (lo[1] + hi[1]) / 2) * scale]; };
    zoomChip.textContent = camera.zoom === 1 ? "" : `${Math.round(camera.zoom * 100)} %`;
    objs.forEach((o) => {
      const col = nscColor(o, mats);
      g.strokeStyle = col; g.fillStyle = col + "22"; g.lineWidth = o.kind === "object" ? 1.3 : 2;
      lines(o).forEach((pts) => {
        g.beginPath(); pts.forEach((p, i) => { const [x, y] = S(p); i ? g.lineTo(x, y) : g.moveTo(x, y); });
        if (dim === "2d" && o.closed) { g.closePath(); g.fill(); }
        g.stroke();
      });
      if (o.point) { const [x, y] = S(o.point); g.fillStyle = col; g.beginPath(); g.arc(x, y, 4, 0, 2 * Math.PI); g.fill(); }
    });
    // 物体注释标在轮廓右上角 / comments at each outline's top right
    g.font = `11px ${css("--mono")}`; g.fillStyle = css("--text-2"); g.textAlign = "left"; g.textBaseline = "bottom";
    objs.forEach((o) => {
      const pts = lines(o).flat().concat(o.point ? [o.point] : []);
      if (!pts.length) return;
      const q = pts.map(S), x = Math.max(...q.map((a) => a[0])), y = Math.min(...q.map((a) => a[1]));
      g.fillText(`${o.index}${o.comment ? " " + o.comment : ""}`, Math.min(x + 4, W - 80), Math.max(y, 12));
    });
    const len = niceStep(110 / scale), x0 = 46, y0 = H - 18;
    g.strokeStyle = css("--text-2"); g.lineWidth = 1.5; g.beginPath(); g.moveTo(x0, y0 - 5); g.lineTo(x0, y0); g.lineTo(x0 + len * scale, y0); g.lineTo(x0 + len * scale, y0 - 5); g.stroke();
    g.textAlign = "center"; g.fillText(`${fmtLen(len, len)} ${unit}`, x0 + len * scale / 2, y0 - 3);
  }
  draw();
  let drag = null;
  canvas.onpointerdown = (e) => { drag = { x: e.clientX, y: e.clientY, pan: dim === "2d" || e.shiftKey || e.button === 2 }; canvas.setPointerCapture(e.pointerId); };
  canvas.oncontextmenu = (e) => e.preventDefault();
  canvas.onpointermove = (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (drag.pan) { camera.panX += dx; camera.panY += dy; } else { camera.yaw += dx * 0.008; camera.pitch = Math.max(-1.45, Math.min(1.45, camera.pitch + dy * 0.008)); }
    drag.x = e.clientX; drag.y = e.clientY; draw();
  };
  canvas.onpointerup = () => { drag = null; };
  canvas.onwheel = (e) => { e.preventDefault(); camera.zoom = Math.max(0.3, Math.min(200, camera.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12))); draw(); };
  canvas.ondblclick = () => { Object.assign(camera, CAMERA0); draw(); };
  renderLayout.redraw = draw;
  renderLayout.ro?.disconnect();
  renderLayout.ro = new ResizeObserver(() => draw());
  renderLayout.ro.observe(stage);
}

function renderDetectors(view) {
  const dets = viz.detectors || [];
  if (!dets.length) { view.innerHTML = `<div class="empty">${esc(t("det_none"))}</div>`; return; }
  view.innerHTML = `<div class="cards">${dets.map((d, k) => `<div class="card"><h3>${d.object} · ${esc(d.comment || d.type)}</h3>
    <dl class="kv"><dt>${esc(t("det_power"))}</dt><dd>${sig(d.power, 4)} W</dd><dt>${esc(t("det_hits"))}</dt><dd>${d.hits}</dd>
    <dt>${esc(t("det_pixels"))}</dt><dd>${(d.pixels || []).join(" × ")}</dd><dt>${esc(t("det_peak"))}</dt><dd id="det-peak-${k}"></dd></dl>
    <div id="det-map-${k}"></div></div>`).join("")}</div><div class="foot">${esc(t("det_note"))}</div>`;
  dets.forEach((d, k) => {
    const grid = d.irradiance_grid;
    if (!grid?.length) return;
    const ny = grid.length, nx = grid[0].length, peak = Math.max(...grid.flat());
    $(`det-peak-${k}`).textContent = `${sig(peak, 4)} W/cm²`;
    const host = $(`det-map-${k}`), size = Math.min(320, host.clientWidth || 320);
    const [, g] = canvasIn(host, size, size);
    const cw = size / nx, ch = size / ny;
    for (let i = 0; i < ny; i++) for (let j = 0; j < nx; j++) {
      const v = peak > 0 ? grid[i][j] / peak : 0;
      g.fillStyle = `hsl(${240 - 240 * v}, 80%, ${12 + 50 * Math.sqrt(v)}%)`;
      g.fillRect(j * cw, (ny - 1 - i) * ch, Math.ceil(cw), Math.ceil(ch));
    }
  });
}

// ---------------- 轮次 ----------------

// 这一轮谁算的：rtc 轮次标 rtc，复测过的再标 OpticStudio 复测（数值已换成 OpticStudio 的）/ who computed a round
function roundSource(r) {
  if (r.backend !== "rtc") return "";
  if (r.confirmed_by == null) return `<span class="tag">${esc(t("tag_rtc"))}</span>`;
  return `<span class="tag ${r.agrees ? "ok" : ""}">${esc(t(r.agrees ? "tag_confirmed" : "tag_disagrees"))}</span>`;
}

function renderRounds(view) {
  const rounds = state.rounds;
  if (!rounds.length) { view.innerHTML = `<div class="empty">${esc(t("no_rounds"))}</div>`; return; }
  view.innerHTML = `<div class="table-wrap"><div class="figure"><div id="g-plot"></div></div><div class="foot">${esc(t("pick_round"))}</div>
    <table><tr><th>${esc(t("round"))}</th><th>${esc(t("tiers"))}</th><th class="num">${esc(t("gap"))}</th><th>${esc(t("state"))}</th><th>${esc(t("note"))}</th></tr>
    ${rounds.slice().reverse().map((r) => `<tr class="${r.viz ? "pick" : ""} ${r.round === selected() ? "sel" : ""}" data-round="${r.round}">
      <td class="num" style="text-align:left">${t("select_round", r.round)}</td><td>${r.tiers.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}${roundSource(r)}</td>
      <td class="num">${fmtG(r.shortfall)}</td><td>${r.achieved ? `<span class="tag ok">${esc(t("achieved"))}</span>` : ""}${r.deliverable ? `<span class="tag">${esc(t("deliverable"))}</span>` : ""}</td>
      <td>${esc(r.note)}</td></tr>`).join("")}</table></div>`;
  view.querySelectorAll("tr.pick").forEach((tr) => tr.addEventListener("click", () => {
    pinnedRound = Number(tr.dataset.round);
    renderSelection();
  }));
  const host = $("g-plot");
  const W = host.clientWidth, H = 180;
  const [, g] = canvasIn(host, W, H);
  const pad = { l: 62, r: 14, t: 10, b: 26 };
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
    g.beginPath(); g.arc(px(r.round), py(r.shortfall), r.round === selected() ? 4.5 : 3, 0, 2 * Math.PI); g.fill();
  });
  g.fillStyle = css("--text-2"); g.font = `11px ${css("--mono")}`; g.textAlign = "right"; g.textBaseline = "middle";
  g.fillText(fmtG(Math.max(...ys)), pad.l - 6, pad.t + 6);
  g.fillText(logScale ? fmtG(Math.min(...positive) / 2) : "0", pad.l - 6, H - pad.b - 4);
  g.textAlign = "center"; g.textBaseline = "top";
  g.fillText("r000", px(0), H - pad.b + 5); g.fillText(t("select_round", n), px(n), H - pad.b + 5);
}

// ---------------- 确认关卡 / gates ----------------

// 需求单与建模说明由用户在窗口里点确认（ADR-0020）；命令行的 --quote 只在窗口用不了时可用
// the user confirms the demand and approves a model here; the CLI's --quote only works when the window cannot be used
let gateKey = null;
let signedAt = 0;

function gateShell(title, sub, stateText, body) {
  return `<div class="gh"><span class="gicon"></span><div class="gt"><b>${esc(title)}</b><span>${sub}</span></div><span class="gstate">${esc(stateText)}</span></div>${body}`;
}

function renderGate() {
  const el = $("gate");
  // 自主任务：只说明谁在过关卡，并给用户一个改为参与的按钮（反过来只能在建任务时定，ADR-0021）
  if (state.mode === "autonomous") {
    if (gateKey === "autonomous" + lang) return;
    gateKey = "autonomous" + lang;
    el.className = "card gate auto";
    el.innerHTML = gateShell(t("gate_auto"), "", t("gate_auto"),
      `<p class="auto-text">${esc(t("mode_auto"))}</p><div class="act"><button class="gbtn" id="mode-switch">${esc(t("mode_to_interactive"))}</button></div>`);
    el.hidden = false;
    $("mode-switch").addEventListener("click", async () => {
      try { await api("mode", {}, { mode: "interactive" }); toast(t("mode_switched")); gateKey = null; stateText = ""; } catch (err) { toast(err.message, true); }
    });
    return;
  }
  const step = state.steps.find((s) => s.id === state.current);
  const open = GATES.has(state.current) && ["waiting", "blocked"].includes(step?.status);
  const g = state.gate || { demand: null, model: null };
  if (!open || (!g.demand && !g.model)) {
    // 刚确认过的卡收成两行、转绿，停留片刻再收起 / a just-confirmed card stays folded and green for a moment
    if (el.classList.contains("signed") && Date.now() - signedAt < 4000) return;
    el.hidden = true; el.innerHTML = ""; el.className = "card gate"; gateKey = null; return;
  }
  // 内容没变就不重画：重画会清掉用户正在打的备注 / redraw only on a change, or the note being typed is lost
  const key = JSON.stringify([g, state.revisions, lang]);
  if (key === gateKey) return;
  gateKey = key;
  const facts = [];
  let ready = true, sub = "";
  const fact = (k, v, cls = "") => facts.push(`<div class="fact ${cls}"><span class="fk">${esc(t(k))}</span><span class="fv">${v}</span></div>`);
  if (g.demand) {
    const d = g.demand;
    fact("fk_demand", esc(d.summary));
    if (d.weak_basis.length) fact("fk_weak", d.weak_basis.map((w) => `<i class="pend"></i><code>${esc(w.id)}</code> ${esc(JSON.stringify(w.bound))} · ${esc(w.source || "—")}`).join("<br>"));
    if (d.missing_source.length) { ready = false; fact("fk_missing", esc(t("gate_missing", d.missing_source.join(", "))), "bad"); }
  } else {
    const m = g.model;
    fact("fk_model", `<code>${esc(m.model)}</code>`);
    if (m.page) sub = `<button class="lnk" id="gate-open">${esc(t("gate_open"))} ↗</button>`;
    if (m.rebuild_reason) fact("fk_rebuild", esc(m.rebuild_reason));
    const todo = [];
    if (!m.rebuild_reason && m.rebuild_required) { ready = false; todo.push(t("gate_no_rebuild")); }
    if (m.exploration_missing) { ready = false; todo.push(t("gate_no_search")); }
    if (!m.shown_sha) { ready = false; todo.push(t("gate_old")); }
    if (todo.length) fact("fk_todo", todo.map(esc).join("<br>"), "bad");
  }
  const gate = g.demand ? "demand" : "model";
  const revs = (state.revisions || []).filter((r) => r.gate === gate).map((r) => esc(t("gate_revised", r.note))).join("<br>");
  el.className = "card gate";
  el.innerHTML = gateShell(t(g.demand ? "gate_demand" : "gate_model"), sub, t("gate_wait"),
    `<div class="body"><div class="facts">${facts.join("")}</div>${revs ? `<div class="rev">${revs}</div>` : ""}
      <div class="act"><input id="gate-note" placeholder="${esc(t("gate_note_short"))}"><button class="gbtn" id="gate-revise">${esc(t("gate_revise"))}</button>
      <button class="gbtn primary" id="gate-ok"${ready ? "" : " disabled"}><span>${esc(t("gate_ok"))}</span><kbd>Ctrl ↵</kbd></button></div></div>
     <div class="done" id="gate-done"></div>`);
  el.hidden = false;
  $("gate-open")?.addEventListener("click", () => api("open", {}, { page: g.model.page }).catch((e) => toast(e.message, true)));
  $("gate-note").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !$("gate-ok").disabled) $("gate-ok").click(); });
  $("gate-ok").addEventListener("click", async (e) => {
    const btn = e.currentTarget, note = $("gate-note").value;
    btn.disabled = true;
    try {
      await api("confirm", {}, g.demand ? { gate: "demand", sha: g.demand.demand_sha, note } : { gate: "model", sha: g.model.shown_sha, model: g.model.model, note });
      toast(t("gate_done"));
      signedAt = Date.now();
      el.classList.add("signed");
      el.querySelector(".gstate").textContent = t("gate_signed");
      $("gate-done").textContent = t("gate_signed_at", new Date().toTimeString().slice(0, 5)) + (note.trim() ? ` · ${note.trim()}` : "");
      gateKey = null; stateText = "";
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
  $("gate-revise").addEventListener("click", async (e) => {
    const btn = e.currentTarget, note = $("gate-note").value.trim();
    if (!note) { toast(t("gate_revise_need"), true); $("gate-note").focus(); return; }
    btn.disabled = true;
    try {
      await api("revise", {}, { gate, sha: g.demand ? g.demand.demand_sha : g.model.shown_sha, note });
      toast(t("gate_revise_done"));
      gateKey = null; stateText = "";
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  });
}

// ---------------- 判定圆环 ----------------

const boundText = (b) => (!b ? "" : "equals" in b ? `= ${sig(b.equals, 4)}${b.tol ? ` ± ${sig(b.tol, 3)}` : ""}`
  : [b.min != null ? `≥ ${sig(b.min, 4)}` : "", b.max != null ? `≤ ${sig(b.max, 4)}` : ""].filter(Boolean).join(" · "));
const fmtVal = (v) => (v == null ? "–" : Math.abs(v) >= 1e5 || (v !== 0 && Math.abs(v) < 1e-3) ? v.toExponential(2) : sig(v, 4));
const liveItems = () => state?.metrics?.items || {};
// 一项的单位：目录条目由后端写好 pick 与所选标量的单位（ADR-0022），其余用需求单写的单位
// a catalog entry carries its pick and that scalar's unit from the backend; other items use the demand's unit
const unitOfItem = (m) => {
  const e = liveItems()[m.id];
  if (!e?.metric_id) return m.unit;
  return e.pick ? e.unit : (e.scalar_units?.value ?? e.unit ?? m.unit);
};

// 圆环与需求卡的条目：需求里带判定的条目在前，用户追加的只看不判定的目录指标在后（ADR-0022）
// gauges and the need card: checked demand items first, then the display-only catalog metrics the user added
function judgeItems() {
  const items = (state?.metric_items || []).map((m) => ({ ...m, display: false }));
  const known = new Set(items.map((m) => m.id));
  Object.entries(liveItems()).forEach(([id, e]) => {
    if (!e.display_only || known.has(id)) return;
    items.push({ id, description: e.name || e.metric_id, unit: e.unit, bound: null, display: true, metric_id: e.metric_id });
  });
  return items;
}

// 实时帧里的条目是否过时：每项带自己的视图版本，与这一帧的版本不同就是旧值；旧格式在运行中一律算旧
// an entry is stale when its own view differs from the frame's; older files count everything stale while running
function staleEntry(e) {
  const live = state.metrics || {};
  if (!e) return true;
  if (live.schema === 2) return e.view !== live.view;
  return e.view !== live.view || !!state.progress;
}

// 一条在选中对象上的值与未达量：当前镜头取后台测的值（metrics.json），轮次取账本记录；未达量都是后台算好的
// an item's value and shortfall for the selection: live from metrics.json, a round from the ledger; shortfalls come from the daemon
function itemAt(id, key) {
  if (key === "live") {
    const e = liveItems()[id];
    if (!e) return { value: null, gap: null, stale: true };
    return { value: e.value, gap: e.display_only ? null : (e.gap ?? null), stale: staleEntry(e), error: e.error, display: !!e.display_only };
  }
  const r = roundOf(key);
  if (!r) return { value: null, gap: null };
  const value = r.values?.[id] ?? null;
  const gap = r.failing ? (r.failing[id] ?? (value == null ? null : 0)) : null;
  return { value, gap };
}

function renderJudge() {
  const items = judgeItems(), key = selected(), host = $("gauges");
  $("judge-title").textContent = t("judge");
  $("judge-src").textContent = key === "live" ? t("k_live") : key != null ? t("select_round", key) : "";
  $("pick-open").title = t("pick_title");
  if (!items.length) { host.innerHTML = `<div class="empty">${esc(t("need_none"))}</div>`; host.dataset.ids = ""; return; }
  const ids = JSON.stringify(items.map((m) => [m.id, unitOfItem(m), m.bound, m.description, m.display])) + lang;
  if (host.dataset.ids !== ids) {
    host.dataset.ids = ids;
    host.innerHTML = items.map((m) => `<div class="gauge ${m.display ? "display" : ""}" data-id="${esc(m.id)}" title="${esc(m.description || m.id)}"><div class="ring"><svg viewBox="0 0 80 80"><circle class="trk" cx="40" cy="40" r="34"/>`
      + `<circle class="fill" cx="40" cy="40" r="34" stroke-dasharray="213.6" stroke-dashoffset="213.6"/></svg><div class="val"><div><span class="num"></span><small>${esc(unitOfItem(m) || "")}</small></div></div></div>`
      + `<div class="n">${esc(m.id)}</div><div class="b">${esc(m.display ? t("display_only") : boundText(m.bound))}</div></div>`).join("");
  }
  host.querySelectorAll(".gauge").forEach((el) => {
    const it = itemAt(el.dataset.id, key), display = el.classList.contains("display");
    // 圆环填充 1/(1+3·未达量)：达成为满圈；只看的条目和没有未达量（旧账本、未测）时画一半、用主色
    // fill 1/(1+3·shortfall); half and gold for display-only items and when the shortfall is unknown
    const frac = display || it.gap == null ? 0.5 : 1 / (1 + 3 * it.gap);
    const fill = el.querySelector(".fill");
    fill.setAttribute("stroke-dashoffset", 213.6 * (1 - frac));
    fill.setAttribute("stroke", display ? "#95e6cb66" : it.gap == null ? "#e6b45066" : it.gap === 0 ? "#aad94c" : it.gap < 0.2 ? "#e6b450" : "#f07178");
    odo(el.querySelector(".num"), fmtVal(it.value));
    el.classList.toggle("ok", it.gap === 0);
    el.classList.toggle("stale", !!it.stale);
    el.classList.toggle("err", !!it.error);
    el.title = [el.title.split("\n")[0], it.error || ""].filter(Boolean).join("\n");
    el.classList.toggle("sel", el.dataset.id === needItem()?.id);
  });
}

// ---------------- 需求卡：曲线、表格或各轮趋势 ----------------

// 没点选时取当前未达最多的一条 / without a pick, the item furthest from its bound now
function needItem() {
  const items = judgeItems();
  if (!items.length) return null;
  const picked = items.find((m) => m.id === needId);
  if (picked) return picked;
  const key = selected();
  return items.slice().sort((a, b) => (itemAt(b.id, key).gap ?? -1) - (itemAt(a.id, key).gap ?? -1))[0];
}

// 曲线与表格只有当前镜头的目录结果里有；轮次的完整结果在证据目录，窗口只画各轮的标量 / curves exist for the live lens only
let needMode = null;
function needViews(m) {
  const e = selected() === "live" ? liveItems()[m.id] : null;
  const views = [];
  if (e?.series?.length) views.push("curve");
  if (e?.table?.rows?.length) views.push("table");
  if (!m.display) views.push("trend");
  return { e, views };
}

function renderNeed() {
  const m = needItem(), host = $("need");
  const seg = $("need-mode");
  if (!m) {
    $("need-name").textContent = t("judge"); $("need-rule").textContent = ""; seg.innerHTML = "";
    host.innerHTML = `<div class="empty">${esc(t("need_none"))}</div>`; host.dataset.mode = "";
    return;
  }
  const { e, views } = needViews(m);
  const mode = views.includes(needMode) ? needMode : views[0] || "trend";
  const segHtml = views.length > 1 ? views.map((v) => `<button data-mode="${v}" class="${v === mode ? "on" : ""}">${esc(t("need_" + v))}</button>`).join("") : "";
  if (seg.dataset.html !== segHtml) { seg.innerHTML = segHtml; seg.dataset.html = segHtml; }
  seg.hidden = !segHtml;
  $("need-name").textContent = m.id;
  const unit = unitOfItem(m);
  $("need-rule").textContent = [m.description, boundText(m.bound) && `${t("metric_bound")} ${boundText(m.bound)}${unit ? ` ${unit}` : ""}`, m.direction,
    m.display ? t("display_only") : ""].filter(Boolean).join(" · ");
  $("need-rule").title = [$("need-rule").textContent, e?.description].filter(Boolean).join("\n");
  if (host.dataset.mode !== mode) {
    host.dataset.mode = mode;
    host.innerHTML = mode === "table" ? `<div class="need-table"></div>`
      : `<canvas></canvas><div class="verdict"><b><span class="num"></span><small></small></b><span></span></div>`;
  }
  const key = selected(), now = itemAt(m.id, key);
  const verdict = host.querySelector(".verdict");
  if (verdict) {
    odo(verdict.querySelector(".num"), fmtVal(now.value));
    verdict.querySelector("small").textContent = unit || "";
    const at = key === "live" ? t("k_live") : key != null ? t("select_round", key) : "";
    const frame = key === "live" && state.metrics?.live?.preview && state.metrics.iteration != null ? ` · ${t("iteration", state.metrics.iteration)}` : "";
    verdict.querySelector(":scope > span").textContent = now.error ? `${at} · ${t("failed")}` : at + frame;
    verdict.title = now.error || "";
    verdict.className = "verdict" + (now.gap === 0 ? " ok" : now.gap > 0 || now.error ? " bad" : "") + (now.stale ? " stale" : "");
  }
  if (mode === "curve") drawCurve(m, e);
  else if (mode === "table") drawTable(e);
  else drawNeed(m);
}

function needCanvas() {
  const cv = $("need").querySelector("canvas");
  if (!cv) return [null];
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return [null];
  const dpr = devicePixelRatio || 1;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  return [g, W, H];
}

// 界限线：需求的界限（equals 带容差时画两条）/ the demand's bound lines
const boundLines = (b) => (!b ? [] : "equals" in b ? [b.equals - (b.tol || 0), b.equals + (b.tol || 0)].filter((v, k, a) => a.indexOf(v) === k) : [b.min, b.max].filter((v) => v != null));

// 目录指标的曲线组（ADR-0022 的 curve 形态）：每组 x 与若干条 y；scatter 画点；判定标量与 y 同单位时画界限线，
// MTF 判定频率画竖线。窗口只画导出的数据
// a catalog curve result: series of x with named y arrays; scatter as dots; bound lines when the judged scalar shares
// the y unit; a vertical line at the MTF judging frequency. The window draws exported data only
function drawCurve(m, e) {
  const [g, W, H] = needCanvas();
  if (!g) return;
  const series = e.series || [];
  const curves = [];
  // 衍射极限不占颜色序号，第一条视场曲线用主色 / the diffraction limit takes no colour slot
  let slot = -1;
  series.forEach((s) => { const isDiff = /diff/i.test(s.description || s.name || ""); if (!isDiff) slot += 1; const si = slot; Object.entries(s.y || {}).forEach(([name, ys]) => {
    curves.push({ x: s.x || [], y: ys || [], scatter: s.render === "scatter", label: [s.name || s.description, Object.keys(s.y).length > 1 ? name : ""].filter(Boolean).join(" · "),
      color: isDiff ? css("--text-3") : FIELD_COLORS[si % FIELD_COLORS.length], dash: isDiff ? [4, 4] : /sag/i.test(name) ? [6, 4] : [] });
  }); });
  const finite = (v) => v != null && isFinite(v);
  const xs = curves.flatMap((c) => c.x.filter(finite)), ys = curves.flatMap((c) => c.y.filter(finite));
  if (!xs.length || !ys.length) { g.fillStyle = css("--text-3"); g.font = `12px ${css("--font")}`; g.textAlign = "center"; g.fillText(t("need_empty"), W / 2, H / 2); return; }
  const yUnit = e.y_axis?.unit ?? "", judged = !m.display && unitOfItem(m) === yUnit;
  const lines = judged ? boundLines(m.bound) : [];
  let [x0, x1] = extent(xs), [y0, y1] = extent([...ys, ...lines]);
  const square = curves.some((c) => c.scatter);
  if (square) { const h = Math.max(Math.abs(x0), Math.abs(x1), Math.abs(y0), Math.abs(y1)) * 1.1 || 1e-3; x0 = y0 = -h; x1 = y1 = h; }
  else { const p = (y1 - y0) * 0.08 || Math.abs(y1) * 0.1 || 1; y0 -= p; y1 += p; if (x0 === x1) { x0 -= 1; x1 += 1; } }
  const L = 54, R = 14, T = Math.min(44, H * 0.22), B = 34;
  let pw = W - L - R, ph = H - T - B, ox = L, oy = T;
  if (square) { const s = Math.min(pw, ph); ox = L + (pw - s) / 2; oy = T + (ph - s) / 2; pw = ph = s; }
  const X = (x) => ox + ((x - x0) / (x1 - x0)) * pw, Y = (y) => oy + (1 - (y - y0) / (y1 - y0)) * ph;
  g.font = `10px ${css("--mono")}`; g.strokeStyle = css("--grid"); g.lineWidth = 1; g.fillStyle = css("--text-3");
  ticks(y0, y1, niceStep((y1 - y0) / 4)).forEach((v) => { const y = Math.round(Y(v)) + 0.5; g.beginPath(); g.moveTo(ox, y); g.lineTo(ox + pw, y); g.stroke(); g.textAlign = "right"; g.fillText(sig(v, 3), ox - 6, y + 3); });
  ticks(x0, x1, niceStep((x1 - x0) / 5)).forEach((v) => { const x = Math.round(X(v)) + 0.5; g.beginPath(); g.moveTo(x, oy); g.lineTo(x, oy + ph); g.stroke(); g.textAlign = "center"; g.fillText(sig(v, 3), x, oy + ph + 13); });
  const axis = (a, fallback) => (a ? `${a.name}${a.unit ? ` (${a.unit})` : ""}` : fallback || "");
  g.textAlign = "center"; g.fillText(axis(e.x_axis, series[0]?.x_label), ox + pw / 2, oy + ph + 27);
  g.textAlign = "left"; g.fillText(axis(e.y_axis), 4, Math.max(10, oy - 8));
  g.strokeStyle = "#acb6bf88"; g.setLineDash([5, 4]);
  lines.forEach((v) => { const y = Y(v); g.beginPath(); g.moveTo(ox, y); g.lineTo(ox + pw, y); g.stroke(); });
  const fq = e.params?.frequency;
  if (judged && finite(fq) && fq >= x0 && fq <= x1) { g.beginPath(); g.moveTo(X(fq), oy); g.lineTo(X(fq), oy + ph); g.stroke(); }
  g.setLineDash([]);
  g.save(); g.beginPath(); g.rect(ox, oy, pw, ph); g.clip();
  curves.forEach((c) => {
    g.strokeStyle = g.fillStyle = c.color;
    if (c.scatter) { c.x.forEach((x, i) => { const y = c.y[i]; if (finite(x) && finite(y)) g.fillRect(X(x) - 0.9, Y(y) - 0.9, 1.8, 1.8); }); return; }
    g.lineWidth = 1.6; g.setLineDash(c.dash); g.beginPath();
    let pen = false;
    c.x.forEach((x, i) => { const y = c.y[i]; if (!finite(x) || !finite(y)) { pen = false; return; } pen ? g.lineTo(X(x), Y(y)) : g.moveTo(X(x), Y(y)); pen = true; });
    g.stroke(); g.setLineDash([]);
  });
  g.restore();
  // 图例：曲线不多时写在左上 / a legend at the top left when there are few curves
  if (curves.length > 1 && curves.length <= 8) {
    g.font = `10px ${css("--font")}`; g.textAlign = "left"; let lx = ox + 6;
    curves.forEach((c) => {
      const w = g.measureText(c.label).width;
      if (lx + w + 20 > ox + pw - 90) return;
      g.fillStyle = c.color; g.fillRect(lx, oy + 6, 10, 2); g.fillStyle = css("--text-3"); g.fillText(c.label, lx + 14, oy + 10); lx += w + 26;
    });
  }
}

// 目录指标的表格（ADR-0022 的 table 形态）：列取各行共有的简单值 / a catalog table: columns are the rows' simple values
function drawTable(e) {
  const host = $("need").querySelector(".need-table");
  const rows = e.table?.rows || [];
  const cell = (v) => (v == null ? "–" : typeof v === "number" ? fmtVal(v) : Array.isArray(v) ? v.map((x) => (typeof x === "number" ? sig(x, 3) : x)).join(", ") : typeof v === "boolean" ? (v ? "✓" : "✗") : String(v));
  const cols = [...new Set(rows.flatMap((r) => Object.keys(r)))].filter((k) => rows.some((r) => r[k] != null && (typeof r[k] !== "object" || (Array.isArray(r[k]) && r[k].length <= 3 && r[k].every((x) => typeof x !== "object")))));
  host.innerHTML = `<table><tr>${cols.map((c) => `<th class="num">${esc(c)}</th>`).join("")}</tr>${rows.map((r) => `<tr>${cols.map((c) => `<td class="num">${esc(cell(r[c]))}</td>`).join("")}</tr>`).join("")}</table>`;
}

function drawNeed(m) {
  const [g, W, H] = needCanvas();
  if (!g) return;
  const rounds = (state.rounds || []).filter((r) => r.values?.[m.id] != null);
  const live = state.live != null ? itemAt(m.id, "live") : null;
  const pts = rounds.map((r, i) => ({ i, v: r.values[m.id], r }));
  const liveAt = pts.length;
  const all = [...pts.map((p) => p.v), ...(live?.value != null ? [live.value] : [])];
  if (!all.length) { g.fillStyle = css("--text-3"); g.font = `12px ${css("--font")}`; g.textAlign = "center"; g.fillText(t("need_empty"), W / 2, H / 2); return; }
  const lines = boundLines(m.bound);
  let [lo, hi] = extent([...all, ...lines]);
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.1 || 1; lo -= pad; hi += pad;
  const L = 50, R = 16, T = Math.min(44, H * 0.22), B = 22, n = liveAt + (live?.value != null ? 1 : 0);
  const X = (i) => L + (n <= 1 ? 0.5 : i / (n - 1)) * (W - L - R), Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  g.font = `10px ${css("--mono")}`; g.strokeStyle = css("--grid"); g.lineWidth = 1; g.fillStyle = css("--text-3");
  ticks(lo, hi, niceStep((hi - lo) / 4)).forEach((v) => { const y = Math.round(Y(v)) + 0.5; g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.textAlign = "right"; g.fillText(sig(v, 3), L - 6, y + 3); });
  g.strokeStyle = "#acb6bf88"; g.setLineDash([5, 4]);
  lines.forEach((v) => { const y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); });
  g.setLineDash([]);
  const key = selected();
  const sx = typeof key === "number" ? pts.find((p) => p.r.round === key) : key === "live" && live?.value != null ? { i: liveAt } : null;
  if (sx) { g.strokeStyle = "#ffffff30"; g.beginPath(); g.moveTo(X(sx.i), T - 6); g.lineTo(X(sx.i), H - B); g.stroke(); }
  if (pts.length) {
    g.strokeStyle = css("--gold"); g.lineWidth = 1.8; g.beginPath();
    pts.forEach((p, k) => (k ? g.lineTo(X(p.i), Y(p.v)) : g.moveTo(X(p.i), Y(p.v)))); g.stroke();
    pts.forEach((p) => {
      const ok = p.r.failing ? !(m.id in p.r.failing) : null;
      g.fillStyle = ok === true ? css("--sage") : ok === false ? css("--card") : css("--gold"); g.strokeStyle = ok === true ? css("--sage") : css("--gold");
      g.lineWidth = 1.4; g.beginPath(); g.arc(X(p.i), Y(p.v), sx && sx.i === p.i ? 4.5 : 3.2, 0, 7); g.fill(); g.stroke();
    });
  }
  if (live?.value != null) {
    const x = X(liveAt), y = Y(live.value);
    if (pts.length) { g.strokeStyle = "#95e6cb66"; g.setLineDash([3, 3]); g.lineWidth = 1.2; g.beginPath(); g.moveTo(X(liveAt - 1), Y(pts.at(-1).v)); g.lineTo(x, y); g.stroke(); g.setLineDash([]); }
    g.globalAlpha = live.stale ? 0.45 : 1; g.fillStyle = css("--mint");
    g.beginPath(); g.moveTo(x, y - 5); g.lineTo(x + 5, y); g.lineTo(x, y + 5); g.lineTo(x - 5, y); g.closePath(); g.fill(); g.globalAlpha = 1;
  }
  g.fillStyle = css("--text-3"); g.textAlign = "center";
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((W - L - R) / 46))));
  pts.forEach((p, k) => { if (k % every === 0 || k === pts.length - 1) g.fillText(t("select_round", p.r.round), X(p.i), H - 6); });
  if (live?.value != null) g.fillText(t("need_live"), X(liveAt), H - 6);
}

$("need-mode").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mode]");
  if (!b) return;
  needMode = b.dataset.mode;
  renderNeed();
});

// ---------------- 指标目录与追加选择 ----------------

// 目录与保存都经后台进程；优化占着它时会失败（BUSY 等），未保存的选择留在窗口里，空闲后自动再存（ADR-0022）
// listing and saving go through the daemon; while it is busy the unsaved selection stays here and is saved once idle
let catalog = null;
let draft = null;
let draftPending = false;
let saveTries = 0;
const SAVE_TRIES = 20;

async function openPicker() {
  $("picker").hidden = false;
  $("picker-title").textContent = t("pick_title");
  $("picker-save").textContent = t("pick_save");
  $("picker-body").innerHTML = `<div class="empty">${esc(t("loading"))}</div>`;
  try {
    const r = await api("metrics");
    catalog = r;
    if (!draft) draft = (r.selection || []).map((x) => ({ ...x }));
  } catch (err) {
    if (!catalog) { $("picker-body").innerHTML = `<div class="empty">${esc(t("pick_unavailable"))} ${esc(err.message)}</div>`; return; }
    toast(`${t("pick_stale")} ${err.message}`, true);
  }
  if (!draft) draft = [];
  renderPicker();
}

function renderPicker() {
  const reserved = new Set((state?.metric_items || []).map((m) => m.id));
  const avail = { available: "ok", unsupported: "warn", unknown: "" };
  const list = (catalog?.metrics || []).map((m) => `<button class="cat ${m.availability === "unsupported" ? "off" : ""}" data-add="${esc(m.id)}" title="${esc(m.description)}">
      <b>${esc(m.name)}</b><small>${esc(m.id)} · ${esc(m.shape)}${m.unit ? ` · ${esc(m.unit)}` : ""}</small>
      <span class="tag ${avail[m.availability] ?? ""}">${esc(t("avail_" + (m.availability || "unknown")))}</span></button>`).join("");
  const chosen = draft.map((x, i) => {
    const m = (catalog?.metrics || []).find((c) => c.id === x.id);
    const picks = m?.scalars?.length ? `<select data-pick="${i}">${m.scalars.map((s) => `<option ${s === (x.pick || "value") ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>` : "";
    const bad = reserved.has(x.key) ? ` <span class="tag warn">${esc(t("pick_conflict"))}</span>` : "";
    return `<div class="chosen"><input data-key="${i}" value="${esc(x.key)}" aria-label="key"><span class="mid">${esc(x.id)}</span>${picks}
      <input class="params" data-params="${i}" value="${esc(JSON.stringify(x.params || {}))}" title="${esc(Object.keys(m?.params || {}).join(", "))}">${bad}
      <button class="icon" data-del="${i}" aria-label="remove">✕</button></div>`;
  }).join("");
  $("picker-body").innerHTML = `<div class="pick-cols"><div><h4>${esc(t("pick_catalog"))}${catalog?.backend ? ` · ${esc(catalog.backend)}` : ""}</h4><div class="cats">${list}</div></div>
    <div><h4>${esc(t("pick_chosen"))}</h4>${chosen || `<p class="muted">${esc(t("pick_none"))}</p>`}<p class="muted">${esc(t("pick_hint"))}</p></div></div>`;
}

function readDraft() {
  const rows = draft.map((x) => ({ ...x }));
  for (const [i, x] of rows.entries()) {
    const key = $("picker-body").querySelector(`[data-key="${i}"]`)?.value.trim();
    const params = $("picker-body").querySelector(`[data-params="${i}"]`)?.value.trim();
    const pick = $("picker-body").querySelector(`[data-pick="${i}"]`)?.value;
    if (!key) throw new Error(t("pick_need_key"));
    let parsed;
    try { parsed = params ? JSON.parse(params) : {}; } catch { throw new Error(`${key}: ${t("pick_bad_params")}`); }
    if (typeof parsed !== "object" || Array.isArray(parsed) || parsed === null) throw new Error(`${key}: ${t("pick_bad_params")}`);
    Object.assign(x, { key, params: parsed });
    if (pick && pick !== "value") x.pick = pick; else delete x.pick;
    if (!Object.keys(x.params).length) delete x.params;
  }
  if (new Set(rows.map((x) => x.key)).size !== rows.length) throw new Error(t("pick_dup"));
  return rows;
}

async function saveDraft(quiet = false) {
  try {
    const r = await api("metrics", {}, { items: draft });
    draft = (r.selection || draft).map((x) => ({ ...x }));
    draftPending = false; saveTries = 0;
    if (!quiet) toast(t("pick_saved"));
    if (!$("picker").hidden) renderPicker();
  } catch (err) {
    // 参数错是用户要改的，忙或断开才留到之后再存 / a parameter error is for the user to fix; busy or disconnected waits
    const retry = !err.code || ["BUSY", "DAEMON_NOT_RUNNING", "TIMEOUT", "DAEMON_TIMEOUT"].includes(err.code);
    draftPending = retry && saveTries < SAVE_TRIES;
    toast(`${retry ? t("pick_later") : t("failed")}: ${err.message}`, true);
  }
}

$("pick-open").addEventListener("click", openPicker);
$("picker-close").addEventListener("click", () => { $("picker").hidden = true; });
$("picker").addEventListener("click", (e) => { if (e.target === $("picker")) $("picker").hidden = true; });
$("picker-body").addEventListener("click", (e) => {
  const add = e.target.closest("[data-add]"), del = e.target.closest("[data-del]");
  if (!add && !del) return;
  try { draft = readDraft(); } catch { /* 先保留输入框里的内容，保存时再报 */ }
  if (add) {
    const id = add.dataset.add, taken = new Set([...draft.map((x) => x.key), ...(state?.metric_items || []).map((m) => m.id)]);
    let key = id, k = 2;
    while (taken.has(key)) key = `${id}_${k++}`;
    draft.push({ key, id });
  } else draft.splice(Number(del.dataset.del), 1);
  renderPicker();
});
$("picker-save").addEventListener("click", async () => {
  try { draft = readDraft(); } catch (err) { toast(err.message, true); return; }
  saveTries = 0;
  await saveDraft();
});

// 每次轮询：后台空闲且有没存上的选择就再试一次，至多 SAVE_TRIES 次 / each poll: retry an unsaved selection once the daemon is idle
function retryDraft() {
  if (!draftPending || !state?.daemon.running || state.progress) return;
  saveTries += 1;
  draftPending = false;
  saveDraft(true);
}

// ---------------- 时间轴 ----------------

// 刻度：每一轮一个，有当前镜头时末尾加一个菱形；点击或拖动吸附到最近的刻度 / one tick per round plus a diamond for the live lens
function timelineItems() {
  const items = (state?.rounds || []).map((r) => ({ key: r.round, r }));
  if (state?.live != null) items.push({ key: "live" });
  return items;
}
let tlKnown = null;

function renderTimeline() {
  const items = timelineItems(), n = items.length, key = selected();
  const pos = (i) => (n <= 1 ? 50 : (i / (n - 1)) * 100);
  $("tl-title").textContent = t("tl_title");
  const rounds = (state.rounds || []).length;
  $("tl-hint").textContent = rounds ? t("tl_rounds", rounds) : t("tl_none");
  const known = new Set(tlKnown || []);
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(($("track").clientWidth || 600) / 52))));
  const html = items.map((it, i) => {
    const label = it.key === "live" ? t("live_tick") : t("select_round", it.key);
    const tip = it.key === "live" ? t("k_live") : `${label} · G ${fmtG(it.r.shortfall)} · ${it.r.tiers.join("+")}${it.r.note ? ` · ${it.r.note}` : ""}`;
    const fresh = tlKnown && !known.has(String(it.key)) ? " new" : "";
    const cls = it.key === "live" ? "live" : it.r.achieved ? "ok" : "";
    return `<div class="tick ${cls}${fresh}" style="left:${pos(i)}%" data-i="${i}" title="${esc(tip)}">${i % every === 0 || i === n - 1 ? `<span>${esc(label)}</span>` : ""}</div>`;
  }).join("");
  tlKnown = items.map((it) => String(it.key));
  if ($("ticks").dataset.html !== html) { $("ticks").innerHTML = html; $("ticks").dataset.html = html; }
  const at = items.findIndex((it) => it.key === key);
  $("knob").hidden = at < 0;
  if (at >= 0) { $("knob").style.left = `${pos(at)}%`; $("rail-fill").style.width = `${pos(at)}%`; } else $("rail-fill").style.width = "0";
  $("ticks").querySelectorAll(".tick").forEach((el) => el.classList.toggle("on", Number(el.dataset.i) <= at && !el.classList.contains("ok")));
  const r = typeof key === "number" ? roundOf(key) : null;
  $("k-pos").textContent = key == null ? "—" : key === "live" ? t("k_live") : t("select_round", key);
  $("k-state").textContent = key == null ? t("k_hint")
    : [r ? `G ${fmtG(r.shortfall)}` : "", r?.achieved ? t("achieved") : "", pinnedRound == null ? t("k_follow") : t("k_review")].filter(Boolean).join(" · ");
}

function selectAt(i) {
  const items = timelineItems();
  if (!items[i]) return;
  // 选最后一个刻度就回到跟随最新 / picking the last tick follows the latest again
  const next = i === items.length - 1 ? null : items[i].key;
  if (next === pinnedRound) return;
  pinnedRound = next;
  renderSelection();
}

function renderSelection() {
  renderTimeline(); renderJudge(); renderNeed(); renderLens(); renderAna();
}

const track = $("track");
let tdrag = false;
const indexFrom = (e) => { const r = track.getBoundingClientRect(), n = timelineItems().length; return Math.round(clampN((e.clientX - r.x) / r.width, 0, 1) * Math.max(0, n - 1)); };
const clampN = (v, a, b) => Math.max(a, Math.min(b, v));
track.addEventListener("pointerdown", (e) => { if (!timelineItems().length) return; tdrag = true; track.classList.add("drag"); track.setPointerCapture(e.pointerId); selectAt(indexFrom(e)); });
track.addEventListener("pointermove", (e) => { if (tdrag) selectAt(indexFrom(e)); });
track.addEventListener("pointerup", () => { tdrag = false; track.classList.remove("drag"); });

$("gauges").addEventListener("click", (e) => {
  const el = e.target.closest(".gauge");
  if (!el) return;
  needId = el.dataset.id;
  renderJudge(); renderNeed();
});

// ---------------- 轮询与事件 ----------------

async function poll() {
  try {
    // 最小化时浏览器会放慢定时器，告诉服务端窗口只是隐藏了，命令行不必再开一个 / hidden, not closed
    const next = await api("state", document.hidden ? { hidden: "1" } : {});
    const text = JSON.stringify(next);
    if (text !== stateText) {
      state = next; stateText = text;
      lang = next.lang === "en" ? "en" : "zh";
      document.documentElement.lang = lang;
      // 选中的轮次已不在账本里（重建任务等）：回到跟随 / a pinned round gone from the ledger: follow again
      if (typeof pinnedRound === "number" && !roundOf(pinnedRound)) pinnedRound = null;
      if (pinnedRound === "live" && next.live == null) pinnedRound = null;
      renderArc(); renderNotes(); renderTabs(); renderTimeline(); renderJudge(); renderNeed();
      renderLens(); renderAna();
    }
    renderTop();
    renderGate();
    retryDraft();
  } catch (e) {
    // 服务重启后沿用同一端口和令牌，继续轮询就能重连 / a restarted service keeps the port and token: keep polling
    $("mode-chip").textContent = e instanceof TypeError ? t("disconnected") : e.message;
  }
  setTimeout(poll, POLL_MS);
}

// 用户关掉窗口：告诉服务，之后的命令不再自动重开，直到需要用户看的节点；刷新时页面马上又会轮询，标记随即去掉
// the user closed the window: later commands leave it closed until the user has to look; a reload polls again at once
window.addEventListener("pagehide", (e) => {
  if (e.persisted || !JOB) return;
  try {
    fetch(`/api/closed?job=${encodeURIComponent(JOB)}`, { method: "POST", keepalive: true, headers: { "X-Saos-Token": TOKEN } });
  } catch { /* 服务不在时什么也做不了 */ }
});

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b || b.dataset.tab === tab) return;
  tab = b.dataset.tab;
  renderTabs(); renderAna(true);
  if (!reduceMotion) { const p = $("view"); p.classList.remove("enter"); void p.offsetWidth; p.classList.add("enter"); }
});
$("expand").addEventListener("click", () => setZoom(true));
$("zoom-close").addEventListener("click", () => setZoom(false));
$("zoom").addEventListener("click", (e) => { if (e.target === $("zoom")) setZoom(false); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (ruler) { ruler = null; if (renderLayout.redraw) renderLayout.redraw(); return; }
  if (!$("picker").hidden) { $("picker").hidden = true; return; }
  if (!$("zoom").hidden) setZoom(false);
});
$("notice-close").addEventListener("click", async () => {
  $("notice").hidden = true;
  try { await api("notice", {}, { message: "" }); } catch { /* 下次轮询再显示 */ }
});
$("dim-seg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-dim]");
  if (!b || b.disabled) return;
  dim = b.dataset.dim;
  $("dim-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  // 2D 与 3D 用的坐标可能不同（折转系统的 2D 换到剖面平面），整体重画 / 2D may use the section frame: redraw from scratch
  renderLens(true);
});
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!state) return;
    renderArc(); renderTimeline(); renderNeed();
    if (renderLayout.redraw) renderLayout.redraw();
    renderAna(true);
  }, 150);
});

// 卡片尺寸变了就重画画布（首次绘制时布局可能还没定）/ redraw canvases when their cards resize (the first draw may precede layout)
const sized = new ResizeObserver((entries) => {
  if (!state) return;
  entries.forEach((e) => {
    if (e.target.id === "need") renderNeed();
    else if (e.target.id === "arc") renderArc();
    else if (e.target.id === "track") renderTimeline();
  });
});
["need", "arc", "track"].forEach((id) => sized.observe($(id)));

$("dim-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.dim === dim));
if (!JOB) document.body.innerHTML = `<div class="empty">?job= missing</div>`;
else poll();
