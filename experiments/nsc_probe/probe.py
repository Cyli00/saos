"""非序列处方新增项的实测：面属性（反射/吸收、膜层、面散射）、Scatter To、要文件的物体类型。

每项按处方搭一个最小系统（经 saos.prescription.normalize 和 ZosBackend.build，与 lens build 同一条路），
读回（backend.objects），再追迹看探测器功率是否符合物理预期；错误写法应在搭建前或搭建时报 BAD_ARGS。
用法：uv run python experiments/nsc_probe/probe.py <输出 JSON> [探测名 ...]
只开一个 standalone 实例（占一个许可证）。
"""

from __future__ import annotations

import json
import math
import sys
import traceback

from saos.backend import nsc
from saos.backend.zos import ZosBackend, _load_zosapi
from saos.prescription import normalize
from saos.protocol import SaosError

RAYS = 100000


def system(objects, waves=(0.55,)):
    return {"mode": "non_sequential", "title": "nsc probe", "units": "mm", "wavelengths": {"um": list(waves)},
            "catalogs": ["SCHOTT"], "nsc": {"simple_ray_splitting": True}, "objects": objects}


def beam(z=0.0, cone=1.0, rays=RAYS):
    """接近准直的点光源：锥半角 cone 度。"""
    return {"type": "SourcePoint", "comment": "beam", "position": [0, 0, z],
            "params": {"# Layout Rays": 10, "# Analysis Rays": rays, "Power(Watts)": 1, "Cone Angle": cone}}


def plate(faces=None, scatter_to=None, z=10.0, comment="plate"):
    """平板玻璃：Standard Lens 两面平、厚 2 mm、口径 20 mm。面 0 侧面、1 前表面、2 后表面。"""
    o = {"type": "StandardLens", "comment": comment, "position": [0, 0, z], "material": "N-BK7",
         "params": {"Clear 1": 20, "Edge 1": 20, "Thickness": 2, "Clear 2": 20, "Edge 2": 20}}
    if faces:
        o["faces"] = faces
    if scatter_to:
        o["scatter_to"] = scatter_to
    return o


def detector(z, half, x=0.0, comment="det", pixels=10):
    return {"type": "DetectorRectangle", "comment": comment, "position": [x, 0, z],
            "params": {"X Half Width": half, "Y Half Width": half, "# X Pixels": pixels, "# Y Pixels": pixels}}


class Probe:
    def __init__(self):
        self.b = ZosBackend("standalone")
        self.b.start()
        self.z = _load_zosapi(self.b.root)
        self.out: dict = {}

    def build(self, p):
        self.b.build(normalize(p))
        return self.b.objects()

    def power(self, numbers, seed=1, **kw):
        t = nsc.trace(self.b.system, seed=seed, **kw)
        return {"trace": {k: t[k] for k in ("succeeded", "dead_ray_errors", "seconds")},
                **{str(n): {k: nsc.detector(self.z, self.b.system, n)[k] for k in ("power", "hits")} for n in numbers}}

    def expect_error(self, p):
        try:
            self.build(p)
        except SaosError as exc:
            return {"code": str(exc.code), "message": str(exc)[:300], "details": {k: (v[:12] if isinstance(v, list) else v) for k, v in (exc.details or {}).items()}}
        return {"code": None, "message": "built without error"}

    # ---- 面：反射、吸收、膜层 ----
    def faces_kind(self):
        rows = {}
        for label, faces in (("default", None), ("rear_absorbing", {"2": {"is": "absorbing"}}),
                             ("front_reflective", {"1": {"is": "reflective"}}), ("all_absorbing", {"all": {"is": "absorbing"}})):
            objs = self.build(system([beam(), plate(faces), detector(20, 50)]))
            rows[label] = {"faces": [{k: f[k] for k in ("face", "name", "is")} for f in objs[1]["faces"]],
                           **self.power([3], split=False, scatter=False)}
        return rows

    def coating(self):
        """理想膜 I.x 的透过率为 x（与角度无关）：两面 I.50 与两面 I.99999999 的探测器功率之比应为 0.25。"""
        rows = {}
        for name in ("I.50", "I.99999999"):
            objs = self.build(system([beam(), plate({"1": {"coating": name}, "2": {"coating": name}}), detector(20, 50)]))
            rows[name] = {"coating_read": [objs[1]["faces"][k].get("coating") for k in (1, 2)], **self.power([3], split=False, scatter=False)}
        rows["ratio"] = rows["I.50"]["3"]["power"] / rows["I.99999999"]["3"]["power"]
        return rows

    # ---- 面散射 ----
    def lambertian(self):
        """后表面 Lambertian 散射 30%：远处小探测器只收到未散射的光，应为不散射时的 0.70；大探测器在近处，散射光大多也收到。"""
        rows = {}
        for label, faces in (("none", None), ("lamb30", {"2": {"scatter": {"model": "lambertian", "fraction": 0.3, "rays": 1}}})):
            objs = self.build(system([beam(), plate(faces), detector(200, 5, comment="far small"), detector(12.5, 100, comment="near big")]))
            # 远处小探测器挡在近处大探测器后面会被挡住：两个分开追，先追远处（近处的移开）
            rows[label] = {"face2": objs[1]["faces"][2], "far": self.power([3], split=False, scatter=True)["3"]}
        rows["far_ratio"] = rows["lamb30"]["far"]["power"] / rows["none"]["far"]["power"]
        return rows

    def scatter_to(self):
        """散射 100%（Lambertian），散射光只追向物体 4（一个不在光路上的小探测器）：近处大探测器收不到散射光，功率应接近 0；
        不设 Scatter To 时近处大探测器收到透射一侧的散射光。"""
        rows = {}
        for label, st in (("free", None), ("to_4", {"objects": [4]})):
            faces = {"2": {"scatter": {"model": "lambertian", "fraction": 1.0, "rays": 1}}}
            objs = self.build(system([beam(), plate(faces, st), detector(12.5, 100, comment="near big"), detector(12.5, 1, x=150, comment="aside")]))
            rows[label] = {"scatter_to_read": objs[1].get("scatter_to"), **self.power([3, 4], split=False, scatter=True)}
        return rows

    def importance(self):
        """重要性采样不改平均功率、只把散射光集中到目标：偏轴小探测器的功率应与不用时在统计误差内一致，命中数大幅增加。
    size 是以目标物体原点为心的球半径，要包住整个目标（±2 mm 方形探测器半对角线 2.83 mm），size 2 时只算到球内的部分。"""
        rows = {}
        for label, st in (("plain", None), ("importance_size2", {"importance": [{"towards": 3, "size": 2.0, "limit": 1.0}]}),
                          ("importance_size3", {"importance": [{"towards": 3, "size": 3.0, "limit": 1.0}]}),
                          ("importance_size5", {"importance": [{"towards": 3, "size": 5.0, "limit": 1.0}]})):
            faces = {"2": {"scatter": {"model": "lambertian", "fraction": 1.0, "rays": 1}}}
            objs = self.build(system([beam(rays=200000), plate(faces, st), detector(50, 2, x=20, comment="off-axis small")]))
            seeds = [self.power([3], seed=s, split=False, scatter=True)["3"] for s in range(1, (25 if label == "plain" else 5))]
            p = [x["power"] for x in seeds]
            mean = sum(p) / len(p)
            se = math.sqrt(sum((x - mean) ** 2 for x in p) / (len(p) - 1) / len(p))
            rows[label] = {"scatter_to_read": objs[1].get("scatter_to"), "power_mean": mean, "power_se": se, "hits": [x["hits"] for x in seeds]}
        return rows

    def models_readback(self):
        """其余模型只核对写入和回读，再追一遍看不报错。"""
        names = {}
        rows = {}
        for label, sc in (("gaussian", {"model": "gaussian", "fraction": 0.2, "sigma": 0.05, "rays": 2}),
                          ("abg", {"model": "abg", "reflect": "ABG-EXAMPLE", "transmit": "ABG-EXAMPLE"}),
                          ("bsdf", {"model": "bsdf", "reflect": "FractalBlack_VIS.bsdf"}),
                          ("user", {"model": "user", "dll": "Lambertian.DLL", "fraction": 0.4}),
                          ("profile", None)):
            face = {"profile": "LAMBERTIAN50"} if sc is None else {"scatter": sc}
            try:
                objs = self.build(system([beam(), plate({"2": face}), detector(20, 50)]))
                rows[label] = {"face2": objs[1]["faces"][2], **self.power([3], split=False, scatter=True)}
            except SaosError as exc:
                rows[label] = {"error": str(exc)[:300], "details": exc.details}
        names["rows"] = rows
        return names

    # ---- 要文件的类型 ----
    def files(self):
        rows = {}
        objs = self.build(system([
            # sample.SDF 只有 11 条光线，# Analysis Rays 写多了 OpticStudio 改回 11（回读报错）；用 10 万条的光线文件
            {"type": "SourceFile", "file": "RAYFILE_LB_T67C_100K_190608_ZEMAX.DAT", "params": {"# Layout Rays": 10, "# Analysis Rays": 20000, "Power(Watts)": 1}},
            {"type": "CADPartSTL", "file": "block.stl", "position": [0, 0, 30], "material": "N-BK7"},
            {"type": "PolygonObject", "file": "cube.pob", "position": [0, 30, 30], "material": "N-BK7",
             "faces": {"all": {"is": "absorbing"}}},
            detector(60, 500, comment="big")]))
        rows["read"] = [{k: o.get(k) for k in ("index", "type", "file", "kind")} | {"faces": len(o.get("faces") or [])} for o in objs]
        rows["cube_faces"] = [f["is"] for f in objs[2]["faces"] or []]
        rows["trace"] = self.power([4], split=False, scatter=False)
        return rows

    # ---- 写错时的报错 ----
    def errors(self):
        return {
            "type_needs_file": self.expect_error(system([{"type": "CADPartSTL"}, detector(10, 5)])),
            "unknown_file": self.expect_error(system([{"type": "CADPartSTL", "file": "nope.stl"}, detector(10, 5)])),
            "file_not_taken": self.expect_error(system([plate() | {"file": "block.stl", "comment": ""}, detector(20, 5)])),
            "face_out_of_range": self.expect_error(system([plate({"7": {"is": "absorbing"}}), detector(20, 5)])),
            "unknown_coating": self.expect_error(system([plate({"1": {"coating": "NO-SUCH"}}), detector(20, 5)])),
            "coating_on_absorbing": self.expect_error(system([plate({"1": {"is": "absorbing", "coating": "AR"}}), detector(20, 5)])),
            "lambertian_sigma": self.expect_error(system([plate({"1": {"scatter": {"model": "lambertian", "fraction": 0.1, "sigma": 1}}}), detector(20, 5)])),
            "unknown_abg": self.expect_error(system([plate({"1": {"scatter": {"model": "abg", "reflect": "NO-SUCH"}}}), detector(20, 5)])),
            "source_faces": self.expect_error(system([beam() | {"faces": {"0": {"is": "absorbing"}}}, detector(20, 5)])),
            "comment_on_file": self.expect_error(system([{"type": "CADPartSTL", "comment": "x", "file": "block.stl"}, detector(10, 5)])),
            "scatter_to_beyond": self.expect_error(system([plate(scatter_to={"objects": [9]}), detector(20, 5)])),
        }


def main(path):
    p = Probe()
    names = sys.argv[2:] or ["faces_kind", "coating", "lambertian", "scatter_to", "importance", "models_readback", "files", "errors"]
    for name in names:
        try:
            p.out[name] = getattr(p, name)()
        except Exception as exc:  # noqa: BLE001
            p.out[name] = {"crash": f"{type(exc).__name__}: {exc}"[:500], "trace": traceback.format_exc()[-1500:]}
        json.dump(p.out, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
        print(name, "done", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
