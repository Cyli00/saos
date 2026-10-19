"""逐项实测 ZOS-API 2024 R1 中 saos 尚未使用的接口（见 README.md）。

用法：uv run python experiments/zosapi_probe/probe.py <输出目录> [探测名 ...]
只开一个 standalone 实例；镜头先复制到输出目录再加载，全局优化等工具写出的文件都落在那里。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

from saos import glasscat
from saos.backend import analysis as an
from saos.backend.zos import ZosBackend, _load_zosapi

SAMPLE_DIR = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives"
COOKE = "Cooke 40 degree field.zmx"
DOUBLE_GAUSS = "Double Gauss 28 degree field.zmx"
CORES = 2


class Probe:
    def __init__(self, out: Path):
        self.out = out
        self.b = ZosBackend("standalone")
        self.info = self.b.start()
        self.z = _load_zosapi(self.b.root)
        self.lenses = {}
        for name in (COOKE, DOUBLE_GAUSS):
            dst = out / "lenses" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SAMPLE_DIR / name, dst)
            zda = (SAMPLE_DIR / name).with_suffix(".ZDA")
            if zda.is_file():
                shutil.copy(zda, dst.with_suffix(".ZDA"))
            self.lenses[name] = dst

    @property
    def sys(self):
        return self.b.system

    def load(self, name=COOKE):
        self.b.load(self.lenses[name])

    # ---- 1. New_Analysis 是否先按默认设置算一遍 ----
    def new_analysis(self):
        self.load(DOUBLE_GAUSS)
        cases = [
            ("HuygensPsf", {"PupilSampleSize": "S_256x256", "ImageSampleSize": "S_256x256"}),
            ("FftMtf", {"SampleSize": "S_256x256"}),
            ("StandardSpot", {"ReferTo": "Centroid"}),
            ("ZernikeStandardCoefficients", {"SampleSize": "S_256x256"}),
        ]
        rows = []
        for name, settings in cases:
            idm = an.idm(self.z, name)
            for rep in range(3):
                row = {"analysis": name, "rep": rep}
                for flow, opener in (("New_Analysis", self.sys.Analyses.New_Analysis),
                                     ("SettingsFirst", self.sys.Analyses.New_Analysis_SettingsFirst)):
                    t0 = time.perf_counter()
                    run = opener(idm)
                    t_open = time.perf_counter() - t0
                    # 打开后、改设置前有没有结果：有就说明已经按默认设置算过
                    pre = run.GetResults()
                    pre_has = pre is not None and (int(pre.NumberOfDataSeries) + int(pre.NumberOfDataGrids)) > 0
                    impl = an.settings_of(run)
                    try:
                        an.apply_settings(impl, settings)
                    except Exception as exc:  # noqa: BLE001
                        row[f"{flow}_settings_error"] = str(exc)[:200]
                    t1 = time.perf_counter()
                    run.ApplyAndWaitForCompletion()
                    t_apply = time.perf_counter() - t1
                    res = run.GetResults()
                    data = an.extract(res, 3, 3)
                    run.Close()
                    row[flow] = {"open_s": round(t_open, 3), "results_before_apply": pre_has,
                                 "apply_s": round(t_apply, 3), "total_s": round(t_open + t_apply, 3),
                                 "digest": _digest(data)}
                row["same_result"] = row["New_Analysis"]["digest"] == row["SettingsFirst"]["digest"]
                rows.append(row)
        return rows

    # ---- 2. 评价函数向导：旧版与 Wizard2 ----
    def wizard2(self):
        self.load(COOKE)
        out = {}
        mfe = self.sys.MFE
        old = mfe.SEQOptimizationWizard
        old.Type, old.Data, old.Reference = 0, 1, 0  # RMS / Spot Radius / Centroid（按向导列表序号）
        out["old_lists"] = {"type": [str(old.GetTypeAt(i)) for i in range(old.NumberOfTypes)],
                            "data": [str(old.GetDataTypeAt(i)) for i in range(old.NumberOfDataTypes)],
                            "reference": [str(old.GetReferenceAt(i)) for i in range(old.NumberOfReferences)]}
        old.Ring, old.Arm = 2, 0
        old.Apply()
        out["old"] = {"operands": int(mfe.NumberOfOperands), "merit": float(mfe.CalculateMeritFunction())}
        w2 = mfe.SEQOptimizationWizard2
        out["w2_enums"] = {k: [str(n) for n in type(getattr(w2, k)).GetNames(type(getattr(w2, k)))]
                           for k in ("Criterion", "Type", "Reference", "Arms")}
        out["w2_yield_available"] = bool(w2.IsHighManufacturingYieldAvailable)
        variants = {
            "gq_spot": dict(Criterion="Spot", Type="RMS", Reference="Centroid", UseGaussianQuadrature=True, Rings=3, Arms="Arms_6"),
            "rect_spot_delvig": dict(Criterion="Spot", Type="RMS", Reference="Centroid", UseRectangularArray=True, GridSizeNxN=8, DeleteVignetted=True),
            "mtf": dict(Criterion="Contrast", Type="RMS", Reference="Centroid", UseGaussianQuadrature=True, Rings=3, Arms="Arms_6", SpatialFrequency=30.0),
            "distortion": dict(Criterion="Spot", Type="RMS", Reference="Centroid", UseGaussianQuadrature=True, Rings=3, Arms="Arms_6",
                               UseMaximumDistortion=True, MaxDistortionPct=1.0),
        }
        for key, settings in variants.items():
            try:
                for k, v in settings.items():
                    cur = getattr(w2, k)
                    if isinstance(v, str) and not isinstance(cur, str):
                        v = getattr(type(cur), v)
                    setattr(w2, k, v)
                w2.Apply()
                types = [str(mfe.GetOperandAt(i).Type) for i in range(1, mfe.NumberOfOperands + 1)]
                out[key] = {"operands": len(types), "merit": float(mfe.CalculateMeritFunction()),
                            "types": sorted(set(types))}
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        return out

    def _cooke_with_variables(self):
        self.load(COOKE)
        n_r = int(self.sys.Tools.SetAllRadiiVariable())
        wiz = self.sys.MFE.SEQOptimizationWizard2
        wiz.Criterion = self.z.Wizards.CriterionTypes.Spot
        wiz.Apply()
        return n_r

    # ---- 3. 优化工具：OrthogonalDescent、Hammer、Global 与玻璃替换 ----
    def optimizers(self):
        # 只用 2 个核：实测时主工作区的 worker 池正在跑扫描，不抢它的 CPU
        out = {"cores": CORES}
        algo = self.z.Tools.Optimization.OptimizationAlgorithm
        for name in ("DampedLeastSquares", "OrthogonalDescent"):
            n_r = self._cooke_with_variables()
            tool = self.sys.Tools.OpenLocalOptimization()
            tool.Algorithm = getattr(algo, name)
            tool.NumberOfCores = CORES
            tool.Cycles = self.z.Tools.Optimization.OptimizationCycles.Automatic
            t0 = time.perf_counter()
            tool.RunAndWaitForCompletion()
            out[f"local_{name}"] = {"variables": int(tool.Variables), "radii_set": n_r, "initial": float(tool.InitialMeritFunction),
                                    "final": float(tool.CurrentMeritFunction), "s": round(time.perf_counter() - t0, 2)}
            tool.Close()

        # Hammer：限时 15 s，同时让一个面做目录内玻璃替换，看玻璃会不会变
        self._cooke_with_variables()
        lde = self.sys.LDE
        glass_rows = [i for i in range(1, lde.NumberOfSurfaces - 1) if str(lde.GetSurfaceAt(i).Material)]
        before = {i: str(lde.GetSurfaceAt(i).Material) for i in glass_rows}
        for i in glass_rows:
            cell = lde.GetSurfaceAt(i).MaterialCell
            sub = cell.CreateSolveType(self.z.Editors.SolveType.MaterialSubstitute)
            sub._S_MaterialSubstitute.Catalog = ""
            cell.SetSolveData(sub)
        tool = self.sys.Tools.OpenHammerOptimization()
        try:
            tool.NumberOfCores = CORES
            out["hammer_props"] = {"TargetRunTimeM": float(tool.TargetRunTimeM), "AutomaticOptimization": bool(tool.AutomaticOptimization)}
            t0 = time.perf_counter()
            status = tool.RunAndWaitWithTimeout(15.0)
            elapsed_wait = time.perf_counter() - t0
            cancelled = bool(tool.Cancel()) if tool.IsRunning else False
            tool.WaitForCompletion()
            out["hammer"] = {"run_status": str(status), "wait_s": round(elapsed_wait, 2), "cancelled": cancelled,
                             "total_s": round(time.perf_counter() - t0, 2), "systems": int(tool.Systems),
                             "initial": float(tool.InitialMeritFunction), "final": float(tool.CurrentMeritFunction),
                             "succeeded": bool(tool.Succeeded)}
        finally:
            tool.Close()
        after = {i: str(lde.GetSurfaceAt(i).Material) for i in glass_rows}
        out["hammer_glass"] = {"before": before, "after": after, "changed": before != after,
                               "merit_now": float(self.sys.MFE.CalculateMeritFunction())}

        # Global：限时 15 s，读前几名的评价函数值；看它在镜头目录写了什么文件
        self._cooke_with_variables()
        lens_dir = self.lenses[COOKE].parent
        files_before = set(p.name for p in lens_dir.iterdir())
        tool = self.sys.Tools.OpenGlobalOptimization()
        try:
            tool.NumberOfCores = CORES
            tool.NumberToSave = self.z.Tools.Optimization.OptimizationSaveCount.Save_10
            t0 = time.perf_counter()
            tool.Run()
            progress = []
            while tool.IsRunning and time.perf_counter() - t0 < 15:
                time.sleep(3)
                progress.append({"t": round(time.perf_counter() - t0, 1), "systems": int(tool.Systems),
                                 "best": float(tool.CurrentMeritFunction(1))})
            if tool.IsRunning:
                tool.Cancel()
            tool.WaitForCompletion()
            out["global"] = {"progress": progress, "initial": float(tool.InitialMeritFunction),
                             "best10": [float(tool.CurrentMeritFunction(k)) for k in range(1, 11)],
                             "systems": int(tool.Systems), "total_s": round(time.perf_counter() - t0, 2),
                             "merit_in_editor_after": float(self.sys.MFE.CalculateMeritFunction())}
        finally:
            tool.Close()
        out["global_files_written"] = sorted(set(p.name for p in lens_dir.iterdir()) - files_before)
        return out

    # ---- 3b. Hammer / Global 的生命周期：会不会自己结束、文件何时写、计数器怎么走 ----
    def _lens_files(self):
        d = self.lenses[COOKE].parent
        return {p.name: p.stat().st_mtime for p in d.iterdir() if p.name.upper().startswith("GLOPT") or "HAMMER" in p.name.upper()}

    def substitute_scope(self):
        """MaterialSubstitute 求解在局部优化、全局搜索里是否生效（Hammer 已证实生效）。"""
        out = {}
        for label in ("local", "global"):
            self._cooke_with_variables()
            lde = self.sys.LDE
            rows = [i for i in range(1, lde.NumberOfSurfaces - 1) if str(lde.GetSurfaceAt(i).Material)]
            for i in rows:
                cell = lde.GetSurfaceAt(i).MaterialCell
                sub = cell.CreateSolveType(self.z.Editors.SolveType.MaterialSubstitute)
                sub._S_MaterialSubstitute.Catalog = ""
                cell.SetSolveData(sub)
            before = [str(lde.GetSurfaceAt(i).Material) for i in rows]
            tool = self.sys.Tools.OpenLocalOptimization() if label == "local" else self.sys.Tools.OpenGlobalOptimization()
            try:
                tool.NumberOfCores = CORES
                if label == "local":
                    tool.Cycles = self.z.Tools.Optimization.OptimizationCycles.Automatic
                    tool.RunAndWaitForCompletion()
                    final = float(tool.CurrentMeritFunction)
                else:
                    tool.NumberToSave = self.z.Tools.Optimization.OptimizationSaveCount.Save_10
                    tool.RunAndWaitWithTimeout(30.0)
                    tool.Cancel()
                    tool.WaitForCompletion()
                    final = float(tool.CurrentMeritFunction(1))
                out[label] = {"initial": float(tool.InitialMeritFunction), "final": final, "systems": int(getattr(tool, "Systems", 0) or 0),
                              "glass": [before, [str(lde.GetSurfaceAt(i).Material) for i in rows]]}
            finally:
                tool.Close()
        return out

    def hammer_auto(self):
        """自动模式的 Hammer 何时自己结束；TargetRunTimeM 有没有影响；HAMMER 文件何时出现、何时删掉。"""
        out = {}
        for p in self.lenses[COOKE].parent.glob("*HAMMER*"):
            p.unlink()
        for label, minutes, substitute in (("radii_target30", None, False), ("radii_target1", 1.0, False),
                                           ("substitute_target30", None, True), ("substitute_target1", 1.0, True)):
            self._cooke_with_variables()
            lde = self.sys.LDE
            if substitute:
                for i in range(1, lde.NumberOfSurfaces - 1):
                    cell = lde.GetSurfaceAt(i).MaterialCell
                    if str(lde.GetSurfaceAt(i).Material):
                        sub = cell.CreateSolveType(self.z.Editors.SolveType.MaterialSubstitute)
                        sub._S_MaterialSubstitute.Catalog = ""
                        cell.SetSolveData(sub)
            glass_before = [str(lde.GetSurfaceAt(i).Material) for i in range(1, lde.NumberOfSurfaces - 1)]
            tool = self.sys.Tools.OpenHammerOptimization()
            try:
                tool.NumberOfCores = CORES
                tool.AutomaticOptimization = True
                if minutes is not None:
                    tool.TargetRunTimeM = minutes
                info = {"target_min": float(tool.TargetRunTimeM), "initial": float(tool.InitialMeritFunction)}
                info.update(self._watch(tool, 150, lambda: float(tool.CurrentMeritFunction)))
                info["final"] = float(tool.CurrentMeritFunction)
                info["systems"] = int(tool.Systems)
                info["files_before_close"] = sorted(self._lens_files())
            finally:
                tool.Close()
            info["files_after_close"] = sorted(self._lens_files())
            info["glass"] = [glass_before, [str(lde.GetSurfaceAt(i).Material) for i in range(1, lde.NumberOfSurfaces - 1)]]
            info["trail"] = [{k: t[k] for k in ("t", "progress", "merit", "glopt_files")} for t in info["trail"]]
            out[label] = info
        return out

    def _watch(self, tool, seconds, merit):
        t0 = time.perf_counter()
        tool.Run()
        trail = []
        while tool.IsRunning and time.perf_counter() - t0 < seconds:
            time.sleep(2)
            trail.append({"t": round(time.perf_counter() - t0, 1), "progress": int(tool.Progress), "status": str(tool.Status),
                          "systems": int(tool.Systems), "merit": merit(), "glopt_files": len(self._lens_files())})
        ended_alone = not tool.IsRunning
        if tool.IsRunning:
            tool.Cancel()
        tool.WaitForCompletion()
        return {"ended_alone": ended_alone, "total_s": round(time.perf_counter() - t0, 1), "trail": trail,
                "succeeded": bool(tool.Succeeded), "error": str(tool.ErrorMessage or "")}

    def opt_lifecycle(self):
        out = {}
        opt = self.z.Tools.Optimization
        for p in self.lenses[COOKE].parent.glob("GLOPT*"):
            p.unlink()
        for label, minutes in (("global_default", None), ("global_target_0.2min", 0.2), ("global_again", None)):
            self._cooke_with_variables()
            before = self._lens_files()
            tool = self.sys.Tools.OpenGlobalOptimization()
            try:
                tool.NumberOfCores = CORES
                tool.NumberToSave = opt.OptimizationSaveCount.Save_10
                info = {"finite": bool(tool.IsFiniteDuration), "target_min_default": float(tool.TargetRunTimeM), "cycles": int(tool.Cycles)}
                if minutes is not None:
                    tool.TargetRunTimeM = minutes
                info.update(self._watch(tool, 40 if minutes else 14, lambda: float(tool.CurrentMeritFunction(1))))
                info["best3"] = [float(tool.CurrentMeritFunction(k)) for k in (1, 2, 3)]
            finally:
                tool.Close()
            after = self._lens_files()
            info["new_files"] = sorted(set(after) - set(before))
            info["rewritten"] = sorted(n for n in before if n in after and after[n] != before[n])
            info["editor_merit"] = float(self.sys.MFE.CalculateMeritFunction())
            out[label] = info
        for label, auto, minutes in (("hammer_auto_0.2min", True, 0.2), ("hammer_manual_0.2min", False, 0.2)):
            self._cooke_with_variables()
            tool = self.sys.Tools.OpenHammerOptimization()
            try:
                tool.NumberOfCores = CORES
                info = {"finite": bool(tool.IsFiniteDuration), "auto_default": bool(tool.AutomaticOptimization)}
                tool.AutomaticOptimization = auto
                tool.TargetRunTimeM = minutes
                info.update(self._watch(tool, 40, lambda: float(tool.CurrentMeritFunction)))
            finally:
                tool.Close()
            info["editor_merit"] = float(self.sys.MFE.CalculateMeritFunction())
            out[label] = info
        return out

    # ---- 4. 模型玻璃 → 目录玻璃：GetClosestGlass、DesignLockdown 与 glasscat.nearest ----
    def closest_glass(self):
        self.load(COOKE)
        lde = self.sys.LDE
        catalogs = [str(c) for c in self.sys.SystemData.MaterialCatalogs.GetCatalogsInUse() if str(c)]
        library = glasscat.load_catalogs(catalogs, Path(str(self.b.app.GlassDir)))
        targets = [(1.5168, 64.17), (1.6200, 36.37), (1.6700, 47.20), (1.7000, 30.00), (1.5500, 55.00), (1.8000, 25.00)]
        glass_rows = [i for i in range(1, lde.NumberOfSurfaces - 1) if str(lde.GetSurfaceAt(i).Material)]
        rows = []
        for k, (nd, vd) in enumerate(targets):
            i = glass_rows[k % len(glass_rows)]
            self.b._set_material(lde.GetSurfaceAt(i), {"model": {"nd": nd, "vd": vd, "dpgf": 0.0}}, "probe")
            zos = str(lde.GetClosestGlass(i))
            ours = glasscat.nearest(nd, vd, library, k=3)
            pick = next((g for g in library if g.name.upper() == zos.upper()), None)
            rows.append({"target": [nd, vd], "GetClosestGlass": zos,
                         "zos_pick": (pick.nd, pick.vd, pick.status) if pick else None,
                         "glasscat_top3": [(g.name, g.nd, g.vd, g.status, round(d, 1)) for d, g in ours],
                         "same": bool(ours) and ours[0][1].name.upper() == zos.upper()})
        # DesignLockdown：在当前（含模型玻璃的）系统上跑，看模型玻璃变成什么
        mats_before = [self.b._material_source(lde.GetSurfaceAt(i), {}) for i in glass_rows]
        tool = self.sys.Tools.OpenDesignLockdown()
        try:
            tool.FixModelGlasses = True
            tool.UsePrecisionRounding = True
            tool.DecimalPrecision = 3
            tool.ConvertSDToMaxApertures = True
            tool.RunAndWaitForCompletion()
            lock = {"succeeded": bool(tool.Succeeded), "error": str(tool.ErrorMessage)}
        finally:
            tool.Close()
        lock["materials_after"] = [str(lde.GetSurfaceAt(i).Material) for i in glass_rows]
        lock["solves_after"] = [str(lde.GetSurfaceAt(i).MaterialCell.GetSolveData().Type) for i in glass_rows]
        lock["radii_after"] = [float(lde.GetSurfaceAt(i).Radius) for i in glass_rows]
        lock["sd_solve_after"] = [str(lde.GetSurfaceAt(i).SemiDiameterCell.GetSolveData().Type) for i in glass_rows]
        lock["models_before"] = mats_before
        return {"catalogs": catalogs, "rows": rows, "lockdown": lock}

    # ---- 5. IMaterialsCatalog 与 glasscat 解析 AGF ----
    def materials_catalog(self):
        self.load(COOKE)
        glass_dir = str(self.b.app.GlassDir)
        out = {"app.GlassDir": glass_dir, "glasscat.DEFAULT_GLASSCAT_DIR": str(glasscat.DEFAULT_GLASSCAT_DIR)}
        tool = self.sys.Tools.OpenMaterialsCatalog()
        try:
            out["all_catalogs"] = len(list(tool.GetAllCatalogs()))
            for cat in ("SCHOTT", "OHARA", "CDGM"):
                try:
                    ours = {g.name.upper(): g for g in glasscat.load_catalogs([cat], Path(glass_dir))}
                except FileNotFoundError as exc:
                    out[cat] = {"error": str(exc)}
                    continue
                tool.SelectedCatalog = cat
                names = [str(n) for n in tool.GetAllMaterials()]
                t0 = time.perf_counter()
                mism = []
                for n in names:
                    tool.SelectedMaterial = n
                    g = ours.get(n.upper())
                    zs = str(tool.MaterialStatus)
                    if g is None:
                        mism.append({"name": n, "issue": "not in AGF parse"})
                        continue
                    # AGF 的 nd/νd 是印刷舍入值，ZOS 给的是按色散公式算出的值；只报超出舍入量级的差别和状态差别
                    if abs(float(tool.Nd) - g.nd) > 5e-5 or abs(float(tool.Vd) - g.vd) > 0.01 or zs.lower() != g.status.lower():
                        mism.append({"name": n, "zos": [float(tool.Nd), float(tool.Vd), zs], "agf": [g.nd, g.vd, g.status]})
                out[cat] = {"zos_count": len(names), "agf_count": len(ours), "mismatches": len(mism), "examples": mism[:5],
                            "read_s": round(time.perf_counter() - t0, 2), "extra_fields_example": None}
                tool.SelectedMaterial = names[0]
                out[cat]["extra_fields_example"] = {"name": names[0], "RelCost": str(tool.RelCost), "MeltFreq": str(tool.MeltFreq),
                                                    "formula": str(tool.MaterialFormula), "TCE": float(tool.TCE)}
        finally:
            tool.Close()
        return out

    # ---- 6. RMS 光斑：IComputeRMSSpotSize、StandardSpot、批量追迹；GEO 的波长含义 ----
    def spot(self):
        self.load(COOKE)
        out = {}
        tool = self.sys.Tools.OpenRMSSpotRadiusCalculator()
        try:
            tool.SetMethodGaussianQuadrature(6, 12, 0.0)
            t0 = time.perf_counter()
            tool.RunAndWaitForCompletion()
            out["rms_calculator_gq"] = {"s": round(time.perf_counter() - t0, 3),
                                        "radii": [float(tool.GetSpotRadius(1, f)) for f in range(1, tool.NumberOfFields + 1)]}
            tool.SetMethodRectangularArray(32, True)
            tool.RunAndWaitForCompletion()
            out["rms_calculator_rect32"] = [float(tool.GetSpotRadius(1, f)) for f in range(1, tool.NumberOfFields + 1)]
        except Exception as exc:  # noqa: BLE001
            out["rms_calculator_error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            tool.Close()
        out["RSCE_operand"] = [self.b.operand("RSCE", 6, 0, [0.0, f]) for f in (0.0, 0.7, 1.0)]
        run = self.sys.Analyses.New_Analysis_SettingsFirst(self.z.Analysis.AnalysisIDM.StandardSpot)
        try:
            an.apply_settings(an.settings_of(run), {"ReferTo": "Centroid"})
            run.ApplyAndWaitForCompletion()
            r = run.GetResults()
            sd = r.SpotData
            out["standard_spot"] = {
                "rms": [[float(sd.GetRMSSpotSizeFor(f, w)) for w in range(0, 4)] for f in range(1, 4)],
                "geo": [[float(sd.GetGeoSpotSizeFor(f, w)) for w in range(0, 4)] for f in range(1, 4)],
                "note": "columns are wave 0..3",
                "ray_data_sets": int(r.NumberOfRayData), "scatter_sets": int(r.NumberOfDataScatterPoints),
            }
            if int(r.NumberOfDataScatterPoints):
                sp = r.GetDataScatterPoint(0)
                out["standard_spot"]["scatter0"] = {"desc": str(sp.Description), "n": int(sp.NumPoints)}
        finally:
            run.Close()
        # 分别只选一个波长，看 GetRMSSpotSizeFor 的波长参数是否被忽略
        per_wave = {}
        for w in ("all", 1, 2, 3):
            run = self.sys.Analyses.New_Analysis_SettingsFirst(self.z.Analysis.AnalysisIDM.StandardSpot)
            try:
                an.apply_settings(an.settings_of(run), {"ReferTo": "Centroid", "Wavelength": w})
                run.ApplyAndWaitForCompletion()
                sd = run.GetResults().SpotData
                per_wave[str(w)] = {"rms_f1_w0..3": [round(float(sd.GetRMSSpotSizeFor(1, k)), 4) for k in range(4)],
                                    "geo_f2_w0..3": [round(float(sd.GetGeoSpotSizeFor(2, k)), 4) for k in range(4)]}
            finally:
                run.Close()
        out["standard_spot_by_wavelength_setting"] = per_wave
        return out

    # ---- 7. 光线瞄准向导 ----
    def ray_aiming(self):
        out = {}
        for name in (COOKE, DOUBLE_GAUSS):
            self.load(name)
            tool = self.sys.Tools.OpenRayAimingWizard()
            try:
                # 2026-10-18 首次实测：Cooke 样例上 RunAndWaitForCompletion 超过 10 分钟不返回，这里限时 30 s
                t0 = time.perf_counter()
                status = tool.RunAndWaitWithTimeout(30.0)
                run_s = round(time.perf_counter() - t0, 3)
                if tool.IsRunning:
                    out[name] = {"timeout": True, "run_status": str(status), "run_s": run_s, "progress": int(tool.Progress),
                                 "status": str(tool.Status), "can_cancel": bool(tool.CanCancel),
                                 "cancel": bool(tool.Cancel()) if tool.CanCancel else None}
                    continue
                rec = tool.GetRecommendedSettings()
                out[name] = {"current": str(self.sys.SystemData.RayAiming.RayAiming), "run_s": run_s,
                             "recommended": {k: str(getattr(rec, k)) for k in dir(rec)
                                             if k[0].isupper() and not callable(getattr(rec, k, None))}}
            except Exception as exc:  # noqa: BLE001
                out[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            finally:
                tool.Close()
        return out

    # ---- 8. 实体模型三角网格 ----
    def shaded_triangles(self):
        self.load(COOKE)
        tool = self.sys.Tools.OpenShadedModelVisualizationExport()
        out = {"settings": [k for k in dir(tool) if k[0].isupper()][:60]}
        try:
            t0 = time.perf_counter()
            tool.RunAndWaitForCompletion()
            out["run_s"] = round(time.perf_counter() - t0, 3)
            res = None
            for attr in ("GetResults", "Results", "TriangleList"):
                if hasattr(tool, attr):
                    v = getattr(tool, attr)
                    res = v() if callable(v) else v
                    out["result_attr"] = attr
                    break
            if res is not None:
                out["triangles"] = int(res.NumberOfTriangles)
                xs = res.XCoords
                out["vector_data_members"] = [k for k in dir(xs) if k[0].isupper()]
                t1 = time.perf_counter()
                data = list(xs.Data) if hasattr(xs, "Data") else None
                out["bulk_read_s"] = round(time.perf_counter() - t1, 3)
                out["x_count"] = len(data) if data is not None else None
                surf = list(res.SurfaceIndex.Data) if hasattr(res.SurfaceIndex, "Data") else []
                out["surfaces_present"] = sorted(set(int(s) for s in surf))
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            tool.Close()
        return out

    # ---- 9. 透镜目录检索 ----
    def lens_catalogs(self):
        self.load(COOKE)
        tool = self.sys.Tools.OpenLensCatalogs()
        out = {}
        try:
            vendors = [str(v) for v in tool.GetAllVendors()]
            out["vendors"] = len(vendors)
            out["vendor_sample"] = vendors[:15]
            vendor = next((v for v in vendors if "THORLABS" in v.upper()), vendors[0])
            tool.SelectedVendor = vendor
            tool.UseEFL, tool.MinEFL, tool.MaxEFL = True, 45.0, 55.0
            tool.UseEPD, tool.MinEPD, tool.MaxEPD = True, 10.0, 30.0
            t0 = time.perf_counter()
            tool.RunAndWaitForCompletion()
            n = int(tool.MatchingLenses)
            out["search"] = {"vendor": vendor, "lenses_for_vendor": int(tool.LensesForVendor), "matching": n,
                             "s": round(time.perf_counter() - t0, 2),
                             "first": [{"name": str(r.LensName), "efl": float(r.EFL), "epd": float(r.EPD), "shape": str(r.Shape),
                                        "type": str(r.Type), "elements": int(r.NumberOfElements)}
                                       for r in (tool.GetResult(k) for k in range(min(n, 5)))]}
            if n:
                lens = tool.GetResult(0)
                before = int(self.sys.LDE.NumberOfSurfaces)
                ok = lens.InsertLensSeq(1, True, False)
                out["insert"] = {"ok": bool(ok), "surfaces_before": before, "surfaces_after": int(self.sys.LDE.NumberOfSurfaces)}
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            tool.Close()
        return out

    # ---- 10. LDE.GetIndex / GetGlass 对比逐个 INDX ----
    def lde_bulk(self):
        from System import Array, Double

        self.load(COOKE)
        lde = self.sys.LDE
        nw = int(self.sys.SystemData.Wavelengths.NumberOfWavelengths)
        t0 = time.perf_counter()
        indx = [[self.b.operand("INDX", i, w, []) for w in range(1, nw + 1)] for i in range(lde.NumberOfSurfaces)]
        t_indx = time.perf_counter() - t0
        t0 = time.perf_counter()
        bulk = []
        for i in range(lde.NumberOfSurfaces):
            arr = Array[Double]([0.0] * nw)
            r = lde.GetIndex(i, nw, arr)
            vals = r[1] if isinstance(r, tuple) else arr
            bulk.append([float(v) for v in vals])
        t_bulk = time.perf_counter() - t0
        glass = []
        for i in range(lde.NumberOfSurfaces):
            r = lde.GetGlass(i, "", 0.0, 0.0, 0.0)
            glass.append([bool(r[0]), str(r[1]), float(r[2]), float(r[3])])
        diff = max(abs(a - b) for ra, rb in zip(indx, bulk) for a, b in zip(ra, rb))
        fo = lde.GetFirstOrderData(0.0, 0.0, 0.0, 0.0, 0.0)
        return {"INDX_s": round(t_indx, 4), "GetIndex_s": round(t_bulk, 4), "max_abs_diff": diff, "GetGlass": glass,
                "GetFirstOrderData": [float(v) for v in fo] if not isinstance(fo, float) else fo,
                "EFFL_operand": self.b.operand("EFFL", 0, 1, [])}

    # ---- 11. 哪些分析没有完整的设置接口（只能用 ModifySettings 改）----
    def settings_coverage(self):
        self.load(COOKE)
        names = an.analysis_names(self.z)
        missing, errors = [], {}
        for n in names:
            try:
                run = self.sys.Analyses.New_Analysis_SettingsFirst(an.idm(self.z, n))
                if run is None:
                    errors[n] = "None"
                    continue
                if not bool(run.HasAnalysisSpecificSettings):
                    missing.append(n)
                run.Close()
            except Exception as exc:  # noqa: BLE001
                errors[n] = f"{type(exc).__name__}: {exc}"[:120]
        return {"total": len(names), "without_specific_settings": missing, "errors": errors}

    # ---- 12. 分析能否中途终止 ----
    def terminate(self):
        self.load(DOUBLE_GAUSS)
        run = self.sys.Analyses.New_Analysis_SettingsFirst(self.z.Analysis.AnalysisIDM.HuygensPsf)
        an.apply_settings(an.settings_of(run), {"PupilSampleSize": "S_1024x1024", "ImageSampleSize": "S_512x512", "Wavelength": "all", "Field": 1})
        out = {}
        try:
            t0 = time.perf_counter()
            msg = run.Apply()
            out["apply_returned_s"] = round(time.perf_counter() - t0, 3)
            out["apply_message"] = str(msg) if msg else None
            out["is_running_after_apply"] = bool(run.IsRunning) if hasattr(run, "IsRunning") else None
            time.sleep(1.0)
            t1 = time.perf_counter()
            out["terminate"] = bool(run.Terminate())
            run.WaitForCompletion()
            out["terminate_to_done_s"] = round(time.perf_counter() - t1, 3)
            out["total_s"] = round(time.perf_counter() - t0, 3)
        finally:
            run.Close()
        return out


def _digest(data) -> str:
    import hashlib

    keep = {"series": data.get("series"), "grids": [{k: g[k] for k in ("nx", "ny", "dx", "stats")} for g in data.get("grids", [])],
            "spot": data.get("spot")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:12]


ALL = ["new_analysis", "wizard2", "optimizers", "closest_glass", "materials_catalog", "spot", "ray_aiming",
       "shaded_triangles", "lens_catalogs", "lde_bulk", "settings_coverage", "terminate", "opt_lifecycle", "hammer_auto", "substitute_scope"]


def main() -> int:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[2:] or ALL
    p = Probe(out)
    results = {"connection": p.info}
    try:
        for name in wanted:
            t0 = time.perf_counter()
            try:
                results[name] = getattr(p, name)()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]}
            results[name + "_s"] = round(time.perf_counter() - t0, 2)
            print(f"{name}: {results[name + '_s']} s", flush=True)
            (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    finally:
        p.b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
