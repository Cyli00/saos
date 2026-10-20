# Plan 1：Optiland 核心后端、`kit.py` 原语下沉与通用物理补丁

## 1. 为什么要做（Why）

1. **摆脱 ZOS-API 进程启动与单次调用的延迟**：
   * 现有架构下，每次启动 standalone OpticStudio 实例需 `5.8–7.9 s`；扫一遍面型参数列需 `0.2 s`；通过 `IMaterialsCatalog` 读取一个玻璃目录需 `14–27 s`；显微物镜单次验收因反复向 MFE 插入/删除 66 项 `ZERN` 操作数耗时 `22 s`。
   * 引入纯 Python 原生的 `OptilandBackend` 后，系统搭建、参数读写、光线追迹与波前 Zernike 矩阵拟合全部在进程内存内完成，可将单次评估耗时从秒级降至毫秒级（`< 20 ms`）。
2. **保证同一套 `checks/` 判定脚本在 Optiland 与 ZOS 之间 100% 零修改复用**：
   * 目前 `src/saos/kit.py` 的上层函数（`spot`、`cra`、`field_profile`、`wave_profile`、`best_focus`、`kept`）已是纯 NumPy 逻辑，但底层 `_get_zos`、`_set_zos`、`trace`、`global_matrix`、`sag` 直接访问了 ZOS 的 `sysm.LDE` 和 `OpenBatchRayTrace`。
   * 若不把这几个底层操作下沉为 `Backend` 抽象原语，Agent 写的 `checks/` 脚本和配方库（`recipes/fiber`、`recipes/microscope`）就无法在 Optiland 阶段和 OpticStudio 阶段跑同一份代码。
3. **补齐 Optiland 面向通用光学系统的 4 个底层物理缺口**：
   * **缺口 A（自动半口径与求解联动）**：Optiland 修改面参数后不会自动按全视场实光线包络更新 `semi_aperture`（Optiland Issue #519），也不会像 Zemax 那样自动串行触发 `pickup`、`f_number`、`marginal_ray_height` 求解；若不补齐，基于机械外缘（`mech_semi_diameter`）的几何交叠检查（`src/saos/geometry.py`）会漏报镜片穿插。
   * **缺口 B（多温度无热化 $dn/dT$ 与热膨胀）**：Optiland 原生缺少随多重结构 `TEMP`/`PRES` 自动重算玻璃绝对折射率与面间距热膨胀的机制。
   * **缺口 C（大视场/离轴系统的光线瞄准对齐）**：广角或折反系统中，Optiland 与 Zemax 的入瞳瞄准可能存在差异，必须在建模阶段（`saos lens describe`）即完成双端基准对齐，而不是等到优化完才发现追迹的不是同一束光。
   * **缺口 D（特种面型与非序列兜底）**：Optiland 尚不支持 GRIN（梯度折射率，Issue #337）及 Zemax 专有非序列体散射 DLL，需要自动识别并路由回 `ZosBackend`。

---

## 2. 怎么做（How）

### 2.1 前置仓库基线快照与依赖配置
1. **保存纯 ZOS-API 基线快照**：
   * 将当前工作区未提交的修改通过测试后提交，创建归档分支 `archive/zos-only` 并打标签 `v0.1.0-zos-only`。
   * 切出开发分支 `feat/optiland-hybrid`。
2. **更新 `pyproject.toml`**：
   * 在 `dependencies` 中加入 `optiland`、`torch`、`numba`。
   * 不拆分 CPU/GPU 两个包或两套 Skill；在 `src/saos/config.py` 中增加后端与设备配置（`SAOS_BACKEND=optiland|zos|fake`，默认 `optiland`；计算精度全局锁定 `float64`）。

### 2.2 实现 `src/saos/backend/optiland.py`（核心建模与状态同步）
1. **处方构建（`build`）与材料库对接**：
   * 接收 `src/saos/prescription.py` 规范化后的处方字典，构建 `optiland.optic.Optic` 实例。
   * 支持球面、二次曲面（`conic`）、偶次/奇次非球面（`EvenAspheric`/`OddAspheric`）、环形面（`Toroidal`）、Zernike/Chebyshev/Forbes 自由曲面、平面/曲面衍射光栅、近轴面（`Paraxial`）及坐标断点（`CoordinateBreak` 与面倾斜偏心 `decenter_tilt`）。
   * 直接复用 `src/saos/glasscat.py` 解析本地 `.AGF`（含 `SAOS_CDGM_INPROD`、`SAOS_NHG_INPROD`）与 `library/materials/`，构建 Optiland 材料对象，无需启动 OpticStudio 查询折射率。
2. **文件读写与双格式同步（`load` / `save_as`）**：
   * 任务 `lenses/` 目录继续以 `.zmx` 作为主文件名；每次保存时，同时写出标准 `.zmx`（供 OpticStudio 与用户查看）及同名伴随文件 `.optiland.json`（无损保留处方扩展项、机械半口径、求解链与变量边界）。
3. **补丁 1：强制光学状态同步钩子 `_sync_optical_state()`**：
   * 在每次 `build`、`load`、`set_surface`、`interference`、`viz_data` 时自动执行（优化内循环不做这一整套同步，见 Plan 2 §2.1）：
     1. **多重结构状态应用**：应用当前激活的 Configuration（间隔、曲率、温度、气压）。
     2. **无热化状态展开（Thermal Expander）**：当当前结构温度 $T \ne 20^\circ\text{C}$ 或气压 $P \ne 1\text{ atm}$ 时，读取 `.AGF` 中的热光系数 $(D_0, D_1, D_2, E_0, E_1, \lambda_{\text{tk}})$ 与热膨胀系数 $\alpha$（及 `library/thermal/`、`library/mechanics/`），实时计算各波长真实折射率 $n(\lambda, T, P)$ 及热膨胀后的曲率半径与厚度。
     3. **Solves 求解链结算**：按面序号依次计算 `pickup`（源面值 × `scale` + `offset`）、`f_number`（近轴边缘光线角求曲率）、`marginal_ray_height`（近轴/实边缘光线高度求厚度）。
     4. **全视场实光线包络重算口径**：对未固定孔径（Floating/Auto）的面，追迹全视场边缘光线环（16 辐线），取最大径向落点高度更新 `semi_diameter`；若未显式指定 `mech_semi_diameter`，保持与净口径联动，确保交给 `src/saos/geometry.py` 的外缘干涉检查（`interference()`）反映最新真实口径。
4. **补丁 2：能力分级与特种系统回退路由（`can_handle(prescription)`）**：
   * 检查处方或 `.zmx`：若含 `mode: "non_sequential"`、混合模式 `objects`、`GRIN`（`Gradient*`）或 Optiland 尚未实现的特殊面型，返回原因说明并自动将该任务路由至 `ZosBackend`。

### 2.3 下沉 `src/saos/kit.py` 的 6 大标准原语
在 `Backend` 基类（`src/saos/backend/base.py`）、`OptilandBackend` 与 `ZosBackend` 上统一实现以下 6 个原语，并将 `kit.py` 中所有 `_need_system(ctx, ...)` 改为调用 `ctx.backend`：

| 原语方法 | 功能定义与 `OptilandBackend` 实现细节 |
|---|---|
| `trace_batch(rays, to) -> Rays` | 输入归一化坐标 `(wave, hx, hy, px, py)` 数组，批量追迹至目标面 `to`，返回目标面局部坐标系下的 `(error, vignette, x, y, z, l, m, n, intensity)`；直接支撑 `kit.trace`、`kit.spot`、`kit.cra`、`kit.field_profile`、`kit.wave_profile`。 |
| `get_param(key)` / `set_param(key, val)` | 统一处理 `"<面>.<radius|curvature|thickness|conic|semi_diameter|mech_semi_diameter|material|comment|par<N>|nd|vd>"`、`"aperture"`、`"field.<n>.x|y"`、`"wave.<n>"`、`"config.<c>.<r>"`。 |
| `global_matrix(index)` & `sag(index, x, y)` | 返回第 `index` 面的 `(3×3 旋转矩阵 R, 3D 平移向量 t)` 与局部 `(x, y)` 处的矢高；直接支撑 `src/saos/geometry.py` 的交叠检查和 `viz_data()` 光路渲染。 |
| `zernike(wave, field, samp, max_term, terms)` | 基于 Optiland 出瞳光程差（OPD）网格与正交 Zernike 标准多项式最小二乘矩阵拟合，单次调用耗时 `< 5 ms`，直接替换 `recipes/microscope/acceptance.py` 中的慢速 MFE 插行拟合。 |
| `operand(name, int1, int2, data)` | 内置高频操作数计算表：近轴量（`EFFL`, `PMAG`, `WFNO`, `ISNA`, `ENPD`, `EXPD`, `EXPP`, `TOTR`, `PARY`, `PARA`）、实光线量（`REAX`, `REAY`, `REAZ`, `RAID`, `RANG`, `PLEN`）、像差与像质（`RSCE`, `RSCH`, `RWCE`, `RWCH`, `SPHA`, `COMA`, `ASTI`, `FCUR`, `DIST`, `AXCL`, `LACL`, `ZERN`）、几何边界（`CTVA`, `ETVA`, `SSAG`, `MNCG`, `MXCG`, `MNCA`, `MNEA`）。 |
| `analysis(name, settings)` & `quick_focus()` | 将 `StandardSpot`、`FftMtf`、`RayFan`、`FieldCurvatureAndDistortion`、`WavefrontMap`、`SeidelCoefficients` 映射到 `optiland.analysis` 并输出统一结构；`quick_focus()` 基于 `kit.best_focus` 闭式最佳焦移更新像面前厚度。 |

### 2.4 补丁 3：`saos lens describe` 建模期双端基准对齐检查（Baseline Parity Check）
* 在 `src/saos/model_report.py` 生成建模说明（`models/NNN-<lens>/`）时：
  * 若本机存在可用的 OpticStudio 许可证，自动在后台用 `ZosBackend` 加载同一镜头，与 `OptilandBackend` 对比一阶量（EFL、工作距离、入瞳/出瞳）、各视场主光线落点、通光率与 RMS 光斑半径。
  * 若因大视场瞳差导致 `paraxial` 光线瞄准存在偏差，自动将 Optiland 切换为 `iterative` / `robust` 光线瞄准并重测；将双端对齐结果写入 `model.json` 的 `parity_check` 字段并在 `index.html` 展示。

---

## 3. 做完预期（Expected Outcome）

1. **性能指标**：
   * `saos daemon start` 启动耗时从 `5.8–7.9 s` 降至 **`< 0.2 s`**（默认启动 `OptilandBackend`，零许可证占用）。
   * 常规序列镜头的 `view.json`（含 2D/3D 全局几何、光线追迹与机械外缘干涉检查）生成耗时 **`< 20 ms`**。
   * 显微物镜配方的单次调焦与 66 项 Zernike 波像差验收评估（`acceptance.py`）耗时从 **`22 s` 降至 `< 0.2 s`（提速 100 倍以上）**。
2. **数值精度与兼容性指标**：
   * 在 Cooke 三片镜、双高斯及 `library/microscope/` 25 个专利基准物镜上，Optiland 与 OpticStudio 的近轴焦距（EFL）相对偏差 **`< 1e-4`**，实光线落点与 RMS 光斑半径相对偏差 **`< 0.5%`**。
   * 现有的所有 `checks/` 脚本、`saos.recipes.fiber.side_view`（除 `HuygensPsf` 外）和 `saos.recipes.microscope` 在不改一行判定代码的情况下，直接在 `OptilandBackend` 上跑通。
3. **阶段快照**：
   * 所有单元测试与双端基准对齐测试通过后，提交并打标签 `v0.2.0-plan1`。
