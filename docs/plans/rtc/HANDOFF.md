# 交接文档（2026-10-26）

写给接手的人：这份文档只讲**现在处于什么状态、动手前必须知道什么、下一步做什么**。
需求范围与逐项差距见 `docs/SCOPE.md`、`docs/GAP-ANALYSIS.md`；参数对照见
`docs/SURFACE-PARAMS.md`；证据落点见 `docs/EVIDENCE.md`；决定见
`docs/adr/0001-own-sequential-ray-tracer.md`。

---

## 1. 一句话现状

序列追迹内核**已经跑通并逐面验证到 1e-10 mm 量级**（三个真实镜头、六种面型、三种玻璃），
可以当 Python 包用；**代码层面已经没有 NotImplementedError 的主路径**，剩下的都是
"写完了但没和 OpticStudio 对过账"：pi-B 的实际光线瞄准、Binary 2/Fresnel 的衍射、
圆形以外的孔径与渐晕码、MCE pickup、DLS 的 AD 雅可比、Huygens PSF/STRH。
逐项状态见 §8。

## 2. 交付物位置与仓库状态

- 仓库：`E:\gitProjects\rtc`（git，单个提交，见 §7）。
- 本机参考数据（手册文本、oracle 历史输出）不在仓库里，也不参与构建（只读，不复制，避免两份漂移）。
  该目录不属于本仓库，也不参与构建。
- 三个任务的运行记录（**只读来源**）：`E:\gitProjects\saos\work\{fiber-var-simplified,fiber-var-b,needle-zoom-interview}`。
  **saos 仓库一个字都不能改**（用户明确要求）。本次只从它的 `lenses/`、`checks/`、`scripts/` 里
  复制了只读副本到 `fixtures/`。

### 目录

```
src/rtc/zmx.py         .zmx 读取（面/参数/XDAT/MCE/MFE/视场/波长/孔径/瞄准标志）
src/rtc/textio.py      BOM 优先的编码判定（UTF-16LE / UTF-8 / ASCII）
src/rtc/materials.py   AGF 玻璃库（公式 1 Schott-Laurent、公式 2 Sellmeier1）
src/rtc/material/     材料公式、目录、模型与环境（本轮集成尚未运行回归，见 MATERIAL.md）
fixtures/material/   原指令观测及原 21 组模型玻璃插值系数归档
src/rtc/system.py      OpticalSystem：材料/厚度/光阑/近轴（含按方向区分的曲率）/瞄准目标
src/rtc/geometry.py    面形矢高与梯度 + 求交（安全括号 + Newton 混合）
src/rtc/trace.py       发射、瞄准、折射/衍射、渐晕、光程、批量追迹
src/rtc/operands.py    操作数后处理（部分已验证，见 §5）
src/rtc/psf.py         出瞳、OPD、FFT PSF、Huygens PSF、Strehl、STRH、RMS 波前
src/rtc/adtrace.py     前向 AD 追迹（DLS 的雅可比，隐函数定理处理求交）
src/rtc/optimize.py    LM/DLS（``Design`` 走 AD，普通残差函数才回退中心差分）
src/rtc/cli.py         ``rtc sag|trace|psf`` 命令行
src/rtc/scope.py       fixtures 扫描器 → manifest.json
oracle/Harness3.cs     v3 基准工具：LoadFile + 批量追迹 + GetSag + 操作数 + MODEL/WAVE/CONFIG
oracle/Harness3.exe    已编译（.NET Framework 4.x csc）
tools/gen_golden.py    生成 golden 作业文件并调用 Harness3
tools/compare_golden.py 逐面比对 rtc 与 OpticStudio
fixtures/lenses/       三件最终镜头（piA-r012 / piB-r007 / codex-r017）
fixtures/rounds/       三个任务全部 30 个轮次
fixtures/golden/       **OpticStudio 真值**（每个面一批、矢高网格、操作数、模型玻璃标定）
fixtures/checks|scripts/ 判定脚本与任务脚本（只读副本）
```

## 3. 快速上手

```bash
cd E:\gitProjects\rtc
uv sync
uv run pytest -q                     # 21 个测试（面形/材料/解析）
uv run python tools/compare_golden.py piA piB codex   # 逐面对照 golden
uv run python tools/compare_golden.py --aim piB       # piB 走真正的瞄准（不用旁路）
uv run rtc trace fixtures/lenses/piA-r012.zmx --px 0,0.5 --py 0 --surface 12
```

```python
from rtc import zmx
from rtc.system import OpticalSystem
from rtc.trace import trace
from rtc.psf import opd_map, strehl, rms_wavefront
from rtc.operands import Operands

lens = zmx.parse(r"fixtures/lenses/piA-r012.zmx")
system = OpticalSystem(lens, wavelength_um=0.92, config=1)
block = trace(system, px=[0, 0.5], py=[0, 0])   # 归一化光瞳坐标
print(block.at(5)["x"], block.opl[:, -1])
print(rms_wavefront(system), strehl(system))
```

oracle（需要本机许可，**路径必须给绝对路径**，相对路径 `LoadFile` 会失败）：

```bash
cd oracle && /c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe -nologo \
  -out:Harness3.exe Harness3.cs -r:ZOSAPI.dll -r:ZOSAPI_Interfaces.dll -r:ZOSAPI_NetHelper.dll
./Harness3.exe "E:/…/lens.zmx" "E:/…/jobs.txt" "E:/…/outdir"
```

## 4. 已实现的功能

| 功能 | 状态 |
|---|---|
| `.zmx`：SURF/TYPE/CURV/CONI/DISZ/GLAS/DIAM/MEMA/PARM/XDAT/VPAR/STOP、ENPD/OBNA、FTYP、RAIM、PUSH、MNUM/MCE、MFE | 可用；30 个轮次全部解析通过 |
| 材料：N-BK7/BK7/SAOS_*/WATER2（AGF 公式 1、2） | 可用 |
| 材料：模型玻璃（21 组 nd/Vd） | 可用；表外组合**明确报错**（公式未公开、不能外推） |
| 面形：STANDARD、EVENASPH、BICONICX、BICONICZ（基底+x/y 多项式）、TOROIDAL（圆柱）、PARAXIAL（平面） | 可用，矢高对 `GetSag` **优于 1e-15** |
| 衍射面型：BINARY_2（相位 φ = M·Σ Aᵢρ^2i）、FRESNELS（平面求交 + 基底折射） | 按手册实现，**未对账**（见 §8.5） |
| 求交：安全括号 + Newton/二分混合，返回 `nan` 表示光线丢失 | 可用（含 `kx≈−1.9e5` 的极端负圆锥面） |
| 折射：向量 Snell + 全反射码 2；衍射面用切向波矢式 `n₂d′ = n₁d + (λ/2π)∇φ_t + s·N` 解 `s` | 可用（衍射项未对账） |
| 光程：逐段 `n·t`，第 1 面处为 0，与批量追迹的 OPL 列同口径；PARAXIAL 面的理想透镜相位单独放在 `RayBlock.phase`（以同视场主光线为零），波前用 `opl + phase`；衍射面加 `λφ/2π` | 可用（见 §6.7） |
| 发射：EPD（入瞳平面 + 近轴求入瞳位置）、OBNA（锚在第 1 面顶点的方向锥） | EPD 可用；OBNA 的方向由瞄准修正 |
| 多重结构：THIC 覆盖（`Lens.thickness(i,cfg)`）；`MNUM` 第 2 个数 = 文件当前结构 | 可用；pickup 按 `scale·源+offset` 递归求解（列语义未标定，§8.7） |
| 渐晕：面孔径 1–10 类（圆/圆遮挡/蜘蛛/矩形/矩形遮挡/椭圆/椭圆遮挡/浮动）按类型码逐类实现 | 码值已对齐手册 §14.46；**未对账**（§8.6） |
| 批量追迹：`trace(system, px, py)` → 逐面 `x,y,z,l,m,n,opl,error,vig` | 可用 |
| 操作数：REAX/REAY/REAC/RAID/TRAC/INDX/SSAG/EXPP/EXPD/CTGT/CTLT/ETGT/MNET/MNCT/MNCA/STRH | 前 7 + STRH 已实现；厚度类按手册写、未对照 |
| 出瞳 + OPD + FFT PSF + Strehl + RMS 波前 | 可用（OPD 见 §6.7） |
| Huygens PSF + STRH | 已实现（`psf.psf_huygens` / `psf.strehl_huygens`），**未与 golden 的 STRH 对账**（§8.4） |
| DLS（`optimize.lm` + `Design`） | 可用，雅可比走 AD（`adtrace`）；`lm(residual, x0)` 仍能用中心差分 |
| 光线瞄准（RAIM 0 2） | 方向模式改成"沿光瞳坐标延拓 + 步长限幅 Newton"，**pi-B 仍未对账**（§6.5） |
| 命令行 | `rtc sag|trace|psf` |

## 5. 已验证的数字（OpticStudio 24.1 实测对照）

对照方式：`oracle/Harness3.exe` 加载真实 `.zmx`，`CreateNormUnpol` 批量实际光线追迹，
逐面读 `x,y,z,l,m,n,法线,OPL`；10 条光瞳光线 × 全部面。

| 镜头 | 结构 | max\|Δx\| (mm) | max\|Δl\| | max\|ΔOPL\| (mm) |
|---|---|---|---|---|
| piA r012 | 1 | 2.9e-11 | 1.1e-15 | 7.1e-10 |
| piB r007 | 1 | 3.4e-10 | 1.1e-10 | 9.0e-9 |
| codex r017 | **5** | 1.3e-10 | 2.8e-10 | 6.5e-10 |

- piB 那次是从 **OpticStudio 自己给出的面 1 出射方向**起追的（即把"瞄准"这一环隔离掉），
  所以它验证的是"求交+折射+光程"，不是瞄准。
- 面形：六种面型在各面的矢高网格上与 `LDE.GetSag` 差 < 1e-15；法线用 golden 的
  `Nx,Ny,Nz` 列间接验证（BICONICX/TOROIDAL 的解析梯度必须用精确式，见 §6.1）。
- 模型玻璃：`n² = A0 + A1λ² + A2λ⁻² + A3λ⁻⁴ + A4λ⁻⁶ + A5λ⁻⁸` 精确解，残差 < 2e-14（n²）。
- WATER2：用公式 1 回代 λ=0.5875618 得 1.33304403，与目录 nd=1.333044 一致到 1e-8。

## 6. 本次会话挖出来的、必须记住的事实

### 6.1 圆锥面梯度不能用 `c·x/S`
`dz/dx = c·x/S` 只在 k=0 时成立。精确式：

```
dz/dx = x/(1+S) · [2c + z·(1+k)c²/S],  S = √(1−(1+k)c²r²)
```
双锥面则分别用 `(cx,kx,U)`、`(cy,ky,U)`，`U = cx x² + cy y²`。
用近似式时法线差 1e-3 量级，折射角差到 5e-2，**这是最初 codex/piB 对不上的直接原因之一**。

### 6.2 `XDAT` 就是"参数 12 之后"
`XDAT_i ≡ 参数(12+i)`。BICONICZ：`XDAT 1..16 = x¹..x¹⁶`，`XDAT 17..32 = y¹..y¹⁶`；
TOROIDAL：`XDAT 1 = Zernike 项数(参数 13)`、`XDAT 2 = 归一化半径(参数 14)`。
`zmx.parse` 现在把 XDAT 同时写进 `params`（`extra` 保留原样）。
证据：piA 面 9 的 `XDAT 2/4/6` 与用矢高数据回归出来的 x²/x⁴/x⁶ 系数逐位相同。

### 6.3 三件镜头里两条"用不到"的分支（可以合法推迟）
- 30 个轮次里 **BICONICZ 的 Zernike 项数恒为 0**（参数 3 = 0）；
- 30 个轮次里 **TOROIDAL 的旋转半径恒为 0**（参数 1 = 0）→ 圆柱面。
所以"Zernike 项"和"有限旋转半径"两条路径没标定也没关系，代码里直接 `NotImplementedError`。

### 6.4 golden 是"文件当前结构"，不是结构 1
`codex-r017.zmx` 保存时停在第 **5** 个结构，`LoadFile` 之后 `MCE.CurrentConfiguration == 5`。
最初拿结构 1 去比，差值 2e-2 mm，排查了很久。`Harness3` 现在会在 `info.csv` 里写
`current_config`。**以后加镜头先看这个字段。**

### 6.5 pi-B 的瞄准尚未对账
现象：`OBNA 0.12` + `RAIM 0 2`。OpticStudio 的射线在**第 1 面交点恒为 (0,0)**，
方向随光瞳坐标变化；光阑（面 9）上的交点是 `(−0.037527·px, −0.024444·py)`，
即**各向异性**（系统是双锥面+圆柱面，近轴光瞳本身就是各向异性的）。
Zemax 手册只说"光阑半径由主波长边缘光线追迹确定"，没有给到能复现的程度。

已试过并失败：`(-y/f)` 式位置瞄准、直接二维 Newton（在圆柱面焦散处跳到反射分支）、
逐坐标轮换二分（会落到"沿轴穿过光阑"的伪根）。

现在的实现（`trace._aim_one_direction` / `_aim_newton`）用**沿光瞳坐标的连续性延拓**：
目标点从 0 按 s∈(0,1] 逐步加，每段用上一段的斜率作初值，Newton 单步限幅 0.02
（|Δu| ≤ 0.02，这是不跳到另一个根的关键），不收敛就把步长减半重试而不是换根。
光阑面上的目标点仍由 `system.aim_target()` 给出：近轴传递 × 物方 NA，各方向分别算，
可以用 `OpticalSystem(aim_scale=(rx,ry))` 用实测值覆盖。

**仍未对账**：跑 `uv run python tools/compare_golden.py --aim piB` 才知道瞄准是否真的复现了
OpticStudio；不加 `--aim` 时仍然走"用 Zemax 出射方向起追"的旁路（把瞄准隔离掉）。

### 6.6 模型玻璃：形式已知，系数不能外推
模型玻璃确实是 6 项 Laurent（残差 2e-14）。但把系数对 `(nd, 1/Vd)` 做仿射回归，
残差 3.9e-2 —— **不是**简单线性关系。此前据此采用查表；当前已接入原指令恢复的模型公式，
不再把 6 点精确插值当作 Laurent 形式的证明。新材料层的回归仍待用户安排，见 MATERIAL.md。
标定方法（已固化成 `fixtures/golden/modelglass/`）：系统里把某面设成 `MaterialModel`
求解 → `INDX` 读 6 个波长 → 解 6×6 线性方程。一次 oracle 运行可标定 20 组。

### 6.7 PARAXIAL 面：出射规则与理想透镜相位
- 波前要加理想透镜相位：只按几何路程累加，OPD 会多出一个 ~9 波的**假离焦**——三件镜头都用 OPD Mode 1（积分实际相位）。
- 但批量追迹的 OPL 列**不含**这一项：piA、codex 的 golden OPL 与 rtc 几何光程逐条差 ≤ 2e-12 mm。所以 `opl` 只记
  几何路程，相位放在 `RayBlock.phase`（AD 追迹同名列），`psf`、`Design(mode="wavefront")` 用 `opl + phase`。
- 出射规则是 `n'·tan u' = n·tan u − y/f`（2026-10-28 OpticStudio 24.1 实测：面后 N-BK7、N-SF6 差 ≤ 7e-18），之后的
  当前折射率换成 n'。此前写成 `tan u' = tan u − y/f` 且不更新折射率，面后不是空气时像面位置差 2.6 mm。
  对照数据在 `fixtures/golden/paraxial-glass`（`tests/test_paraxial_glass.py`）。
- 相位是沿面积分 n'·(L', M') − n·(L, M)（相位梯度），以同视场主光线为零：`trace._paraxial_lens_phase`，AD 追迹同式。
  入射光束无像差时积分有闭式，不需要知道光束会聚度；近轴近似就是 `−r²/(2f)`，f = 50、r = 4 时边缘差 0.46 waves。
  入射光束有像差时 OpticStudio 的积分路径未公开，仍用同式，**未对照**。
- `psf.opd_map` 与 OPDC 对照（2026-10-28，f = 50 的 PARAXIAL 面、像面在焦前 5 mm，轴上视场）：面后空气、面后 N-BK7 两种，
  边缘 −32.16 / 184.87 waves，逐点差 ≤ 1e-5 waves（N-BK7 的数据在 golden `trace.json` 的 `opdc`）。此前差 52 waves，三处原因：
  参考球面交点取错根（`−bq − √…` 应为 `bq − √…`）；`OpticalSystem._paraxial` 在 PARAXIAL 面后不换折射率，出瞳位置错
  （面后 N-BK7 时 −45 → −43.491）；相位用近轴近似。OPD 符号也改成与 OPDC 一致（光程比参考球面短为正）。
  改后 piA 的 OPD RMS 是 0.046 waves（PV 0.146；此前记的 0.117 含上面的错误），这个值没有 OpticStudio 对照。

### 6.8 其它小坑
- AGF 有两种编码（`MISC2.AGF` 是 UTF-16LE，`SCHOTT.AGF` 是 ASCII），**必须先看 BOM**；
  按 utf-16 解 ASCII 文件会"成功"但解出乱码（`0x4343 → '\u4343'`）。
- `WATER2` 在 `%USERPROFILE%\Documents\Zemax\Glasscat\MISC2.AGF:457`，不在 `MISC.AGF`。
- `MNUM` 第二个数与 MCE 行数不成比例（r012: 1 但有 6 行；r017: 5 但有 27 行），
  解析器已改为**按行形状**识别 MCE 行。
- `GetOperandValue`：`SSAG` 与 `INDX` **固定评估第 1 个光学面**（传面号无效）；
  `CONF` 返回 0（它只是后续操作数的开关）；`REAY` 的 6 个参数据 .zmx 的 MFE 行是
  `(Surf, Wave, Hx, Hy, Px, Py)`，但全零参数时返回 0，布局能否真正生效**未确认**。
- 批量追迹 `ReadNextResult` 的 C# 签名是 `out int, out int, out int, 11×out double`：
  `i1 = 光线序号`、`i2 = 错误码`、`i3 = 渐晕码`、`d0..d5 = x,y,z,l,m,n`、
  `d6..d8 = 法线`、`d9 = OPL`、`d10 = ?（恒 1）`。注意 `saos` 里那段 Python 的列映射
  与本签名不一致（它把 `r[3]` 当渐晕），只影响 saos，不影响本仓库。
- `HYPR`（codex = 1，另两件 = 0）语义仍未确认，官方文档与本机 CHM 都没有；要做 A/B 才敢定。
- oracle 需要**绝对路径**；`FRU__delta_init(): Attempt to start when running!` 是残留
  `OpticStudio.exe` 进程的提示，不影响结果。

## 7. 仓库状态

历史已压成**一个提交**（内容 = 骨架 + 面形/求交/折射/批量追迹 + oracle v3 + golden +
本轮的 AD/CLI/Huygens/孔径/Binary 2 等），工作区应当是干净的。

本轮改动**没有跑过任何测试**（用户要求统一测试），只做过语法编译（`python -m py_compile`），
语义均未验证。下次开工第一件事是跑完整的回归：

```bash
uv run pytest -q
uv run python tools/compare_golden.py piA codex
uv run python tools/compare_golden.py --aim piB
uv run rtc trace fixtures/lenses/piA-r012.zmx --px 0,1 --py 0
```

## 8. 清单（做完的 / 没对账的）

状态标记：**[已实现·未对账]** = 代码写完、按手册或样本实现，但还没有 oracle 数值证据；
**[已实现·已验证]** = 有 golden 对照。

1. **提交上一轮的改动** —— 已完成（历史已压成单个提交）。
2. **DLS 的自动微分雅可比** —— **[已实现·未对账]**。
   `rtc/adtrace.py`：`Variable` + `trace_ad` + `stack`；求交用隐函数定理
   `dt/dθ = −F_θ/F_t`，折射/归一化用 `Dual` 的 `sqrt/where`；`optimize.Design` 给
   残差与雅可比，`optimize.lm(design, x0)` 走 AD（传普通函数时才回退中心差分）。
   验证方式：把 `Design.jacobian(θ)` 与中心差分逐列比（相对误差应在 1e-6 量级）。
   明确不做：`BINARY_2` 的相位、`RAIM=2` 的瞄准迭代不进雅可比（见模块 docstring）。
3. **pi-B 瞄准** —— **[已实现·未对账]**，延拓法见 §6.5。
4. **Huygens PSF + STRH** —— **[已实现·未对账]**。
   `psf.psf_huygens`（按像面行分块）与 `psf.strehl_huygens`（峰值比，分母用同一组
   采样点 + 同一参考相位，只把相位置 0）。对账对象：`fixtures/golden/*/op.csv` 的
   `STRH`（piA 0.9194、piB 0.1875、codex 0.8789）。注意 STRH 与采样数和
   Huygens 参考相位（planar/spherical）强相关，必须同设置比。
5. **Binary 2 / Fresnel** —— **[已实现·未对账]**。
   `geometry.Binary2.phase/phase_grad`：φ = M·Σ_{i≤N} Aᵢρ^2i，ρ²=(x²+y²)/r_n²，
   解析梯度 `∂φ/∂x = M·2x/r_n²·Σ i·Aᵢρ^(2i−2)`（删掉了原来那段 x→y 的除零写法）。
   追迹侧：`trace._grating` 解 `n₂d′ = n₁d + (λ/2π)∇φ_t + s·N`，取 `d′·N > 0` 的根；
   光程按手册加 `λφ/2π`。Fresnel 按手册"平面求交 + 基底折射"，并且
   `opd_map` 对 Fresnel 面直报 `NotImplementedError`（手册明说不支持 OPD）。
   样本：pi-B r002 的 `FRESNELS`、r003/r004 的 `BINARY_2`（后者的相位系数全为 0，
   所以只有"零级"路径有样本）。
6. **圆形以外的孔径 + 渐晕码** —— **[已实现·未对账]**。
   `trace.aperture_vignette` 按手册 §14.46 的 10 个类型码逐条实现（含蜘蛛的臂宽/臂数槽位复用），
   类型码用手册 §14.46 的表对齐（None 0 / 圆孔 1 / 圆遮挡 2 / 蜘蛛 3 / 矩形 4、5 /
   椭圆 6、7 / 用户 8、9 / 浮动 10）。用户孔径（8/9）直报未实现。
   注意：**渐晕不再按 `DIAM` 判**——只设通光半口径时 OpticStudio 不遮挡光线。
   这只会影响 `vig` 列，不动光线几何。
7. **MCE pickup** —— **[已实现·列语义部分未标定]**。
   `Lens.thickness` 当 `MCEOperand.pickup` 非空时按 `scale·源厚度 + offset` 递归求解（带成环检测）；
   判据是实测的 `solve[5]`/`solve[6]` 偏离 `(1, 0)`（固定行 `1.0 0.0`，pickup 行 `-1.0 0.1`），
   比猜 `solve` 类型码可靠。源面号取 `solve[4]`；**源结构号不取 `solve[3]`**（r003 随结构变、
   r004 恒为 1，但两份文件实测的取值关系都是"源自同结构"），一律按同结构解。
   三件最终镜头没有 pickup，所以这条不影响已验证的数字。
8. **CLI** —— **[已实现]**：`rtc sag|trace|psf`（`src/rtc/cli.py`，入口在 `pyproject.toml`）。
9. **文档收尾** —— 已更新 `GAP-ANALYSIS.md` 的"未实现"行与本节。

### 顺带解决的两个未定项

- `MNUM` 第 2 个数 = **保存文件时选中的结构**（实测 piA 1 / piB 1 / codex 5，与 golden
  `info.csv` 的 `current_config` 逐位一致）。`zmx.Lens.current_config` 已解析，
  `OpticalSystem(config=None)` 默认用它。
- `CURV` 行的第 2 列 = 半径列的求解类型（0 固定 / 1 变量），已解析成 `Surface.solve_radius`，
  `adtrace.variables_from_lens()` 用它 + `VPAR` 一起生成变量表。

## 9. 待用户确认

1. `HYPR 1`（codex）要不要当作"高精度追迹"来处理？现在完全忽略。
2. 模型玻璃的表外组合要不要现在就把公式**拟合出来**（用 (nd,Vd) 网格 25 组暴力拟合），
   还是保持"表内可用、表外报错"？后者更诚实，前者需要额外一次 oracle 标定。
3. `docs/GAP-ANALYSIS.md` 里点名的 `REAA/REAB` 在三个任务里一次都没出现，
   复现范围还要不要包含它们。

## 10. 2026-10-26：向量化与 saos 集成

saos 以可编辑路径依赖引用本仓库（saos `docs/adr/0014-rtc-fast-copy-for-exploration.md`，`ctx.fast()`）。
改动本仓库的追迹约定，要同时跑 `tools/compare_golden.py` 和 saos 的 `tests/test_zemax_rtc.py`（需要 OpticStudio）。

| 改动 | 依据 / 实测 |
|---|---|
| 求交向量化：`Shape.intersect_batch`；Standard 用二次曲面闭式解（Spencer–Murty，取顶点分支，判别式为负 = 丢失），EVENASPH 从闭式解起 Newton，其余面型用 `geometry.bracket_newton` | golden 三件镜头位置差 ≤ 1.1e-11 mm（原 ≤ 6.5e-10）；piB OPL 9.0e-9 → 3.2e-13 mm |
| 通用求交的迭代改为带括号保护的 Newton；`intersect_value` 改为调用同一算法 | 旧标量版同号时不收缩括号：随机光线约 12% 跑满 200 轮，矢高残差到 4.2e-7 mm（`tests/test_intersect_batch.py` 的光线集） |
| 错误码：丢失 = +面号，全反射 = −面号；渐晕码 = 第一个挡住光线的面号（原为自定的 0x131… 与 `TIR = 2`） | 沿用 OpticStudio 批量追迹约定；saos 只用"是否为 0"，码值本身未对账 |
| 丢失的光线现在记错误码（原来那行 `np.where` 是空操作，丢失光线静默保留上一面的位置） | — |
| `.zmx`：`FTYP` 第 3/4/5 列 = 视场数 / 波长数 / 归一化（0 径向、1 矩形），解析 `XFLN/YFLN/FWGN`；`Lens.active_wavelengths`、`Lens.catalogs`（`GCAT`） | OpticStudio 24.1 存盘实测（同一镜头只改归一化，第 5 列 0→1） |
| 视场：`OpticalSystem.field_slopes(hx, hy)`，按归一化方式取最大视场；可逐光线给数组（原来固定用最后一个视场缩放） | saos 4 面双胶合含 (2°, 5°) 视场，600 条光线位置差 ≤ 1.4e-14 mm |
| EPD 实际瞄准的目标半径：主波长轴上实际边缘光线（高度 EPD/2、平行入射）在光阑面的高度，所有波长共用（原来读 LDE 半口径，改参数就过时） | 光阑在镜后的双胶合：OpticStudio 的交点比该半径短 2.8e-9（相对，是它的瞄准收敛）；近轴 \|A\|·EPD/2 差 1e-3 量级，不是它的定义 |
| 玻璃库：每个 AGF 只解析一次，按 `GCAT` 顺序查牌号；`OpticalSystem(glasses=...)` 可传入 OpticStudio 实际使用的目录 | 两家目录同名牌号参数不同 |
| EPD + 有限物距直接 `NotImplementedError`（原来按物在无穷远发射） | — |

速度（本机，77 条光线）：piA 72 → 18 ms、codex 78 → 17 ms；只含 Standard/EVENASPH 的镜头走闭式解，
saos 双胶合 49 点扫描（每点 12 个点列、21×21 光瞳）0.33 s，OpticStudio 5.5 s。

已处理（2026-10-28）：piA、codex 的 OPL 与 golden 差 2.963e-2 mm，原因是 PARAXIAL 相位项混进了 opl（见 §6.7），
现在 ≤ 2.1e-12 mm；`psf.opd_map` 与 OPDC 差 52 waves（§6.7）。仍未处理：OBNA 的逐光线延拓瞄准仍是 Python 循环；
piA、codex 的 EXPP 与 golden 不符（疑在变形面的近轴处理），三件镜头的 EXPD 都不符，原因未确认（piB 的 EXPP 已一致）。

补充：`Glass.tref` 解析 AGF `TD` 行的参考温度（本模块不做温度修正）；saos 据此拒绝换进 OpticStudio 会做温度修正的玻璃。OBNA 实际瞄准（`compare_golden.py --aim piB`）仍不对：10 条光线 9 条不收敛、位置差 0.096 mm，saos 因此只接受 EPD。
