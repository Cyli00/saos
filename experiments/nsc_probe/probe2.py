"""第二批非序列功能的实测：光源页、Type 页、衍射页、折射率页、光致发光、单位、混合模式、各类探测器读数、
网格轮廓与交叠、探测器伸出所在体的检查、非序列优化。

每项按处方搭最小系统（saos.prescription.normalize → ZosBackend.build），读回并追迹核对。
用法：uv run python experiments/nsc_probe/probe2.py <输出 JSON> [探测名 ...]
"""

from __future__ import annotations

import json
import sys
import traceback

from saos import geometry
from saos.backend import nsc
from saos.backend.zos import ZosBackend, _load_zosapi
from saos.prescription import normalize
from saos.protocol import SaosError


def system(objects, waves=(0.55,), **top):
    return {"mode": "non_sequential", "title": "nsc probe 2", "units": "mm", "wavelengths": {"um": list(waves)},
            "catalogs": ["SCHOTT"], "nsc": {"simple_ray_splitting": True}, "objects": objects, **top}


def beam(rays=50000, cone=1.0, **extra):
    return {"type": "SourcePoint", "comment": "beam", "params": {"# Layout Rays": 10, "# Analysis Rays": rays, "Power(Watts)": 1, "Cone Angle": cone}, **extra}


def plate(z=10.0, **extra):
    return {"type": "StandardLens", "comment": "plate", "position": [0, 0, z], "material": "N-BK7",
            "params": {"Clear 1": 20, "Edge 1": 20, "Thickness": 2, "Clear 2": 20, "Edge 2": 20}, **extra}


def detector(z, half, x=0.0, comment="det", pixels=10, kind="DetectorRectangle"):
    return {"type": kind, "comment": comment, "position": [x, 0, z],
            "params": {"X Half Width": half, "Y Half Width": half, "# X Pixels": pixels, "# Y Pixels": pixels}}


class Probe:
    def __init__(self):
        self.b = ZosBackend("standalone")
        self.b.start()
        self.z = _load_zosapi(self.b.root)

    def build(self, p):
        self.b.build(normalize(p))
        return self.b.objects()

    def power(self, numbers, seed=1, **kw):
        t = nsc.trace(self.b.system, seed=seed, **kw)
        return {"trace": {k: t[k] for k in ("succeeded", "dead_ray_errors", "seconds", "error")},
                **{str(n): {k: v for k, v in nsc.detector(self.z, self.b.system, n).items() if k in ("power", "hits", "pixels", "type")} for n in numbers}}

    def safe(self, fn):
        try:
            return fn()
        except SaosError as exc:
            return {"error": str(exc)[:400], "details": {k: (v[:15] if isinstance(v, list) else v) for k, v in (exc.details or {}).items()}}

    def sources_tab(self):
        src = {"polarization": {"jx": 1.0, "jy": 0.0, "x_phase": 0.0, "y_phase": 0.0}, "sampling": "random", "pre_propagation": 1.0,
               "color": {"mode": "blackbody", "temperature": 5000, "from": 0.45, "to": 0.65, "count": 20},
               "array": {"type": "rectangular", "number_x": 3, "number_y": 2, "spacing_x": 2.0, "spacing_y": 2.0}}
        out = {}
        objs = self.build(system([beam(source=src), detector(20, 100)]))
        out["read"] = objs[0]["source"]
        out["with_array"] = self.power([2], split=False, scatter=False)
        objs = self.build(system([beam(), detector(20, 100)]))
        out["plain"] = self.power([2], split=False, scatter=False)
        out["bad_color_field"] = self.safe(lambda: self.build(system([beam(source={"color": {"mode": "d65", "temperature": 5000}}), detector(20, 5)])))
        return out

    def type_tab(self):
        """光线忽略平板（always）时远处探测器应收到全部功率（没有菲涅耳损失）；不忽略时约 0.917。"""
        out = {}
        for label, opts in (("normal", None), ("rays_ignore_always", {"rays_ignore": "always"})):
            objs = self.build(system([beam(), plate(**({"type_options": opts} if opts else {})), detector(20, 50)]))
            out[label] = {"read": objs[1]["type_options"], **self.power([3], split=False, scatter=False)}
        objs = self.build(system([beam(), plate(type_options={"consider": [1, 3], "ignore": [1], "is_detector": True}), detector(20, 50)]))
        out["lists_read"] = objs[1]["type_options"]
        return out

    def diffraction_tab(self):
        grating = {"type": "DiffractionGrating", "comment": "grating", "position": [0, 0, 10], "material": "N-BK7",
                   "diffraction": {"split": "dll", "dll": "Diff2DSample.dll", "start_order": -1, "stop_order": 1}}
        objs = self.build(system([beam(), grating, detector(30, 100)]))
        out = {"read": objs[1].get("diffraction"), "params": [p["name"] for p in objs[1]["params"]][:8],
               **self.power([3], split=True, scatter=False)}
        out["bad_dll"] = self.safe(lambda: self.build(system([beam(), {**grating, "diffraction": {"split": "dll", "dll": "nope.dll"}}, detector(30, 5)])))
        out["no_tab"] = self.safe(lambda: self.build(system([beam(), plate(diffraction={"split": "table"}), detector(30, 5)])))
        return out

    def index_tab(self):
        out = {}
        box = {"type": "RectangularVolume", "comment": "block", "position": [0, 0, 10], "material": "N-BK7",
               "params": {"X1 Half Width": 5, "Y1 Half Width": 5, "Z Length": 5, "X2 Half Width": 5, "Y2 Half Width": 5}}
        objs = self.build(system([beam(), {**box, "index_model": {"type": "grin", "dll": "grin1.DLL", "max_step": 0.5}}, detector(30, 100)]))
        out["grin"] = {"read": objs[1].get("index_model"), **self.power([3], split=False, scatter=False)}
        objs = self.build(system([beam(), {**box, "index_model": {"type": "birefringent", "axis": [0, 1, 0]}}, detector(30, 100)]))
        out["birefringent"] = {"read": objs[1].get("index_model"), **self.power([3], split=False, scatter=False)}
        return out

    def photoluminescence(self):
        vol = {"type": "CylinderVolume", "comment": "dye", "position": [0, 0, 10], "material": "N-BK7",
               "params": {"Front R": 5, "Z Length": 5, "Back R": 5},
               "volume_physics": {"model": "photoluminescence", "efficiency": "quantum_yield", "consider_mie": False, "threshold": 0.005,
                                  "pl_density": 2e17}}
        out = {}
        # 光谱文件写不进去（实测），处方应拒绝 / the spectrum files cannot be written (measured): the prescription refuses them
        out["files_refused"] = self.safe(lambda: normalize(system([beam(), {**vol, "volume_physics": {**vol["volume_physics"], "absorption": "x.zas"}},
                                                                    detector(30, 5)])))
        try:
            objs = self.build(system([beam(), vol, detector(30, 100)], waves=(0.488, 0.52)))
            out["read"] = objs[1]["volume_physics"]
            out["trace"] = self.power([3], split=False, scatter=True)
        except SaosError as exc:
            out["error"] = str(exc)[:500]
            out["details"] = exc.details
        return out

    def units(self):
        # 单位改成流明后光源的功率列标题变成 Power(Lumens) / the power column header follows the source units
        src = beam()
        src["params"] = {k if k != "Power(Watts)" else "Power(Lumens)": v for k, v in src["params"].items()}
        objs = self.build(system([src, detector(20, 50)], source_units="lumens", analysis_units="W/mm2"))
        return {"units": nsc.units(self.b.system), "n": len(objs)}

    def mixed(self):
        p = {"title": "mixed", "units": "mm", "aperture": {"type": "entrance_pupil_diameter", "value": 4}, "fields": {"type": "angle", "y": [0]},
             "wavelengths": {"um": [0.55]}, "catalogs": ["SCHOTT"], "object": {"thickness": "inf"},
             "surfaces": [{"thickness": 5, "comment": "front"},
                          {"type": "NonSequential", "comment": "group", "thickness": 0, "params": {"Exit Loc Z": 10},
                           "objects": [{"type": "StandardLens", "comment": "lens", "position": [0, 0, 2], "material": "N-BK7",
                                        "params": {"Radius 1": 20, "Clear 1": 5, "Edge 1": 5, "Thickness": 3, "Clear 2": 5, "Edge 2": 5},
                                        "faces": {"1": {"coating": "AR"}}}]},
                          {"thickness": 30, "comment": "after"}]}
        out = {}
        self.b.build(normalize(p))
        objs = self.b.objects()
        out["objects"] = [{k: o.get(k) for k in ("group", "index", "type", "comment", "material")} | {"coating": o["faces"][1].get("coating")} for o in objs]
        out["info"] = {k: v for k, v in self.b.system_info().items() if k in ("nsc_groups", "num_surfaces")}
        # 有透镜时轴上边缘光线应被会聚：像面上边缘光线高度不等于光瞳高度
        z = self.z
        lde = self.b.system.LDE
        bt = self.b.system.Tools.OpenBatchRayTrace()
        norm = bt.CreateNormUnpol(2, z.Tools.RayTrace.RaysType.Real, int(lde.NumberOfSurfaces) - 1)
        for py in (0.0, 1.0):
            norm.AddRay(1, 0.0, 0.0, 0.0, py, getattr(z.Tools.RayTrace.OPDMode, "None"))
        bt.RunAndWaitForCompletion()
        norm.StartReadingResults()
        out["rays_at_image"] = [[round(float(x), 5) if isinstance(x, float) else str(x) for x in list(norm.ReadNextResult())[:9]] for _ in range(2)]
        bt.Close()
        desc = self.b.describe_model(None)
        out["describe_groups"] = [{"surface": s["index"], "objects": len(s["objects"])} for s in desc["surfaces"] if s.get("objects")]
        out["bad_objects_on_standard"] = self.safe(lambda: normalize({**p, "surfaces": [{"thickness": 5, "objects": []}, *p["surfaces"][1:]]}))
        return out

    def detectors(self):
        src = beam(rays=20000, cone=10.0)
        objs = [src,
                {"type": "DetectorPolar", "comment": "polar", "position": [0, 0, 40], "params": {"Maximum Angle": 90, "Radial Size": 200, "# Radial Pixels": 10, "# Angular Pixels": 12}},
                ]
        out = {}
        self.build(system(objs))
        nsc.trace(self.b.system, seed=1, split=False, scatter=False)
        d = nsc.detector(self.z, self.b.system, 2, grid=True)
        out["polar"] = {k: v for k, v in d.items() if k not in ("power_grid", "intensity_grid")}
        out["polar_grid"] = [len(d["power_grid"]), len(d["power_grid"][0])]
        self.build(system([src, detector(40, 100, kind="DetectorColor", pixels=20)]))
        nsc.trace(self.b.system, seed=1, split=False, scatter=False)
        d = nsc.detector(self.z, self.b.system, 2, grid=True)
        out["color"] = {k: v for k, v in d.items() if k != "power_grid"}
        out["color_grid"] = [len(d["power_grid"]), len(d["power_grid"][0]), round(sum(map(sum, d["power_grid"])), 6)]
        vol = {"type": "DetectorVolume", "comment": "vol", "position": [0, 0, 20],
               "params": {"X Half Width": 100, "Y Half Width": 100, "Z Half Length": 1, "# X Pixels": 4, "# Y Pixels": 3, "# Z Pixels": 2}}
        self.build(system([src, vol]))
        nsc.trace(self.b.system, seed=1, split=False, scatter=False)
        d = nsc.detector(self.z, self.b.system, 2, grid=True)
        out["volume"] = {k: v for k, v in d.items() if k != "power_grid"}
        out["volume_grid"] = d["power_grid"]
        self.build(system([src, detector(40, 100)]))
        nsc.trace(self.b.system, seed=1, split=False, scatter=False)
        out["rectangle"] = {k: v for k, v in nsc.detector(self.z, self.b.system, 2).items()}
        return out

    def meshes(self):
        """Standard Lens 和 STL 用导出网格：包围盒应在物体位置；两片重叠的透镜应报交叠。"""
        lens = lambda z, c: {"type": "StandardLens", "comment": c, "position": [0, 0, z], "material": "N-BK7",  # noqa: E731
                             "params": {"Radius 1": 20, "Clear 1": 5, "Edge 1": 5, "Thickness": 3, "Clear 2": 5, "Edge 2": 5}}
        items = self.build(system([lens(10, "a"), lens(11.5, "b"), {"type": "CADPartSTL", "file": "block.stl", "position": [0, 30, 20], "material": "N-BK7"},
                                   beam(), detector(40, 10)]))
        nsc.attach_meshes(self.b.system, items)
        out = {"boxes": {it["index"]: it.get("mesh_box") for it in items if it.get("mesh_box")},
               "edges": {it["index"]: len(it.get("mesh_edges") or []) for it in items},
               "sections": {it["index"]: len(it.get("mesh_section") or []) for it in items}}
        clash = geometry.nsc_overlaps(items)
        out["overlaps"] = clash["overlaps"]
        out["unchecked"] = clash["unchecked"]
        out["inside_lens_a"] = geometry._contains(items[0], [0.0, 0.0, 11.0])
        out["outside_lens_a"] = geometry._contains(items[0], [0.0, 0.0, 30.0])
        return out

    def spill(self):
        """杂散荧光任务的情形：圆柱 r 5 里放 ±5 方形探测器会报伸出；外面加吸收环、或探测器缩到 ±3.5 都不报。"""
        cyl = {"type": "CylinderVolume", "comment": "glass", "position": [0, 0, 0], "material": "N-BK7", "params": {"Front R": 5, "Z Length": 1, "Back R": 5}}
        ring = {"type": "AnnularVolume", "comment": "ring", "position": [0, 0, -1], "material": "ABSORB",
                "params": {"Front X Min": 5, "Front Y Min": 5, "Rear X Min": 5, "Rear Y Min": 5, "Front X Max": 8, "Front Y Max": 8,
                           "Rear X Max": 8, "Rear Y Max": 8, "Z Length": 3}}
        out = {}
        for label, objs in (("square_in_cylinder", [cyl, beam(), detector(0.5, 5)]),
                            ("with_absorber_ring", [cyl, ring, beam(), detector(0.5, 5)]),
                            ("small_detector", [cyl, beam(), detector(0.5, 3.5)]),
                            ("detector_in_air", [cyl, beam(), detector(5, 50)])):
            items = self.build(system(objs))
            nsc.attach_meshes(self.b.system, items)
            spill, unchecked = geometry.detector_spill(items)
            out[label] = {"spill": spill, "unchecked": unchecked}
        return out

    def optimize(self):
        """透镜沿 z 可变、非序列向导按探测器总通量取最大（Greater Than 目标）：正交下降后探测器功率应变大。"""
        lens = {"type": "StandardLens", "comment": "lens", "position": [0, 0, 5], "material": "N-BK7",
                "params": {"Radius 1": 15, "Clear 1": 10, "Edge 1": 10, "Thickness": 4, "Clear 2": 10, "Edge 2": 10}}
        self.build(system([beam(rays=5000, cone=20.0), lens, detector(60, 1.5)]))
        before = self.power([3], split=False, scatter=False)["3"]["power"]
        out = {"before": before, "vary": self.b.vary_object(None, 2, "z", True),
               "wizard_rows": self.b.merit_wizard({"criterion": "Total Flux", "detector": 3, "boundary": "Greater Than", "target": 1.0})}
        out["merit_rows"] = [r["type"] for r in self.b.merit_rows()]
        res = self.b.optimize("5", algorithm="od", max_seconds=120)
        out["optimize"] = {k: res[k] for k in ("initial_merit", "final_merit", "algorithm", "variables", "succeeded", "stopped_by")}
        out["z_after"] = float(self.b.system.NCE.GetObjectAt(2).ZPosition)
        out["after"] = self.power([3], split=False, scatter=False)["3"]["power"]
        return out


def main(path):
    p = Probe()
    out = {}
    names = sys.argv[2:] or ["sources_tab", "type_tab", "diffraction_tab", "index_tab", "photoluminescence", "units", "mixed",
                             "detectors", "meshes", "spill", "optimize"]
    for name in names:
        try:
            out[name] = getattr(p, name)()
        except Exception as exc:  # noqa: BLE001
            out[name] = {"crash": f"{type(exc).__name__}: {exc}"[:600], "trace": traceback.format_exc()[-1800:]}
        json.dump(out, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
        print(name, "done", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
