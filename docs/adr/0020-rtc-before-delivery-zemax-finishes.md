---
status: accepted; 第 2、3 条已落地（2026-10-30），其余待做，见“落地顺序”
---

# 交付之前都在 rtc 上计算，zemax 只做交付前的最后优化

2026-10-30 复盘 fiber-var-zoom 任务的会话：agent 在建模时用 `saos daemon use zos` 固定了后端，之后一直没有放开，设计阶段的 11 轮（r13–r23）全在 OpticStudio 上；即使放开，任务的 6 个检查直接读 `ctx.system.LDE` 或开 Huygens PSF 分析，在 rtc 上一项都算不了。用户随后决定扩大 rtc 的范围。本决定修订 ADR-0017（设计轮次在 rtc，交付前复测）和 ADR-0018 的阶段分工：建模与模型确认也归 rtc，OpticStudio 只留给交付。

用户拍板的事项：

1. **分工**：需求、建模、模型确认、设计轮次都在 rtc 上计算；zemax 只做交付前的最后优化。zemax 收尾后在同一状态下记一轮，报告的数值与公差分析都来自这一轮的 OpticStudio 测量。每轮都交给 zemax 复测是可选项，在需求访谈时和用户确认，默认不复测。
2. **固定后端有期限**：`saos daemon use rtc|zos` 只在当时的流程阶段内有效，阶段一变自动回到 `auto`；固定期间每个结果带 `backend_pin`（固定的后端、固定时的阶段、按阶段规则本该用的后端），放开的那次带 `released`。
3. **检查只经 ctx**：rtc 上没有 `ctx.system`，访问时报 `BACKEND_UNSUPPORTED` 并指向 `ctx.get`（面型参数用 `<面>.par<N>`）、`ctx.op`、`ctx.configuration`、`ctx.analysis`；判定结果的 `backend_unsupported` 列出当前后端算不了的条目，它们算作执行出错，不算设计不达标。
4. **rtc 补 Huygens PSF**：检查经后端中立的 `ctx.huygens` 会话取 PSF，宽度由 `kit.psf_widths`（横向 FWHM、包围能量直径）和 `kit.psf_axial`（离焦曲线 FWHM 与峰值位置）计算，`n_photon` 给多光子阶数；波前 RMS 经 `ctx.wavefront_rms`。rtc 已有 `psf.psf_huygens` / `strehl_huygens`，对账通过前 rtc 后端对这两项报 `BACKEND_UNSUPPORTED`。对账与 OpticStudio 同设置（光瞳采样、像面采样、参考相位）：横向 FWHM 与离焦扫描得到的轴向 FWHM 的相对差不超过 1% 与需求 `rel` 中较严者。
5. **算法来源**：按公开资料（OpticStudio 手册、Goodman 等教材）净室实现，OpticStudio 只作黑盒对账；`ref/mimic` 的反编译还原体只作线索（有哪些设置项与约定），不照搬逻辑。开源实现可作参照，但它们没有与 Zemax 对账的证据，参照不能代替对账：optiland（MIT，`optiland/psf/huygens_fresnel.py` 标量与矢量 Huygens）、ray-optics（BSD-3，布局与近轴）、prysm（MIT，FFT/MFT 传播，以解析解验证）。
6. **模型页由 rtc 出图**：rtc 用光线与面矢高画 XZ/YZ 截面，3D 斜视图暂不做；上线前与 OpticStudio 的布局图对照一次。在此之前 `lens describe` 仍需 OpticStudio。
7. **关卡由用户在窗口里点**：需求确认与模型确认改为工作流窗口按钮，记录用户看到的那一页的 sha；CLI `--quote` 保留为降级方式，记录与报告标明“非窗口确认”，quote 须是用户单条消息的原文。

## 落地顺序

- 已落地：第 2、3 条；`STAGE_BACKENDS` 中 modeling 与 model_approve 改为 rtc（`lens describe` 仍在 `OPTICSTUDIO_ONLY`）；第 4 条的 ctx 接口与 kit（OpticStudio 实现），fiber 配方的测量库改经 ctx 与 kit（与旧库的 OpticStudio 逐项对比待跑）；第 7 条（窗口 `/api/gate`、`/api/confirm`，记录带 `via`；结构改动的重建原因在窗口确认时取自 notes 的 `rebuild_reason`）。
- 依赖对账：第 4、6 条；交付阶段“zemax 最后优化后记一轮”的流程（`round --confirm` 与报告的关系）。

## 后果

- 交付前不占 OpticStudio 许可；需要 OpticStudio 的检查会以 `backend_unsupported` 显式暴露，而不是让 agent 悄悄整段改用 zos。
- 风险：zemax 的最后优化可能改变 rtc 上的结论；交付轮的数值来自 OpticStudio，结论不同时回到设计。默认不逐轮复测，rtc 与 OpticStudio 的差异要到交付才暴露，所以每准入一项计算都要留对账用例（ADR-0017 的要求不变）。
- 第 4、6 条完成前，Huygens 类检查和模型页仍要 OpticStudio；这段时间固定 zos 要按第 2 条的期限执行。
