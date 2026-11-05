"""R9：CODE V AUT 与 rtc AD-DLS 的参考基准，终点统一交给 OpticStudio 测量。

显式运行才启动商业软件：uv run python tests/bench_codev.py --case iris --output <新目录>。
只复用案例库；FLOA 先冻结为起点 EPD，两边变量均为首面曲率和最后空气间隔。
两边评价函数权重不同，结果不用于准入；JSON 保留转换检查、命令、耗时及终点。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

import numpy as np

from case_variants import CASES, pupil_grid
from rtc import firstorder, zmx
from rtc.adtrace import Variable
from rtc.optimize import Design, lm
from rtc.system import OpticalSystem
from rtc.trace import trace
from saos.backend import rtc_lens
from saos.kit import KitMethods

# AUT 的正常输出有小节标题 ERROR FUNCTION CONSTRUCTION，不算失败
FAILED = re.compile(r"(?im)^\s*\**\s*(error(?!\s+function)|fatal)\b")


class CodeV:
    """用已有 pythonnet 依赖做 COM 晚绑定；同步调用限时，AUT 异步并支持取消。"""

    def __init__(self, directory: Path, timeout: float):
        self.directory, self.timeout = directory, timeout
        self.object = None
        self.log = []

    def __enter__(self):
        import clr  # noqa: F401
        from System import Activator, Array, Int32, Object, Type
        from System.Globalization import CultureInfo
        from System.Reflection import BindingFlags
        from System.Runtime.InteropServices import Marshal

        self.flags, self.array, self.culture, self.marshal = BindingFlags, Array[Object], CultureInfo.InvariantCulture, Marshal
        self.int32 = Int32
        self.type = Type.GetTypeFromProgID("CodeV.Application", True)
        self.object = Activator.CreateInstance(self.type)
        try:
            self.invoke("StartingDirectory", self.flags.SetProperty, str(self.directory))
            self.invoke("CommandTimeout", self.flags.SetProperty, Int32(int(self.timeout * 1000)))
            self.call("StartCodeV")
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def invoke(self, name, flag, *args):
        return self.type.InvokeMember(name, flag, None, self.object, self.array(args), self.culture)

    def call(self, name, *args):
        return self.invoke(name, self.flags.InvokeMethod, *args)

    def get(self, name):
        return self.invoke(name, self.flags.GetProperty)

    def command(self, command: str):
        output = str(self.call("Command", command) or "")
        self.log.append({"command": command, "output": output})
        if FAILED.search(output):
            raise RuntimeError(f"CODE V 命令失败：{command}\n{output}")
        return output

    def number(self, expression: str) -> float:
        value = float(str(self.call("EvaluateExpression", expression)))
        if not math.isfinite(value):
            raise ValueError(f"CODE V 的 {expression} 返回非有限数值")
        return value

    def optimize(self, iterations: int) -> dict:
        command = f"aut; err cdv; mxc {iterations}; go"
        start = time.monotonic()
        self.call("AsyncCommand", command)
        stopped = False
        try:
            while self.get("IsExecutingCommand"):
                if time.monotonic() - start >= self.timeout:
                    self.call("StopCommand")
                    stopped = True
                    deadline = time.monotonic() + 5.0
                    while self.get("IsExecutingCommand"):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("CODE V 取消后 5 秒内未退出计算")
                        self.call("Wait", self.int32(100))
                    break
                self.call("Wait", self.int32(100))
        except BaseException as exc:
            try:
                self.call("StopCommand")
            except Exception as cleanup:
                exc.add_note(f"CODE V 取消失败：{cleanup}")
            raise
        output = str(self.get("CommandOutput"))
        self.log.append({"command": command, "output": output})
        if FAILED.search(output):
            raise RuntimeError(f"CODE V AUT 失败：{output}")
        return {"elapsed_s": time.monotonic() - start, "stopped_by": "timeout" if stopped else None}

    def __exit__(self, kind, exception, traceback):
        if self.object is None:
            return False
        failures = []
        try:
            try:
                self.call("StopCodeV")
            except Exception as cleanup:
                failures.append(cleanup)
            try:
                self.marshal.FinalReleaseComObject(self.object)
            except Exception as cleanup:
                failures.append(cleanup)
        finally:
            self.object = None
        if failures:
            if exception is None:
                raise ExceptionGroup("CODE V 关闭失败", failures)
            for cleanup in failures:
                exception.add_note(f"CODE V 关闭失败：{cleanup}")
        return False


class TimedDesign(Design):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = math.inf
        self.best, self.cost = self.values(), math.inf

    def _check(self, theta):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("rtc AD-DLS 达到预算")
        system = self.system_with(theta)
        rays = np.asarray(self.rays)
        block = trace(system, rays[:, 2], rays[:, 3], rays[:, 0], rays[:, 1])
        chiefs = np.unique(rays[:, :2], axis=0)
        reference = trace(system, np.zeros(len(chiefs)), np.zeros(len(chiefs)), chiefs[:, 0], chiefs[:, 1])
        if np.any(block.error) or np.any(reference.error):
            raise ValueError("rtc 候选有追迹失败的光线，不能用被清零的 AD 导数继续比较")

    def _keep_best(self, theta, r):
        if not np.all(np.isfinite(r)):
            raise ValueError("rtc 候选的残差不是有限值")
        cost = float(r @ r)
        if cost < self.cost:
            self.best, self.cost = np.asarray(theta).copy(), cost

    def residual(self, theta):
        self._check(theta)
        r = super().residual(theta)
        self._keep_best(theta, r)
        return r

    def jacobian(self, theta):
        self._check(theta)
        r, jacobian = super().jacobian(theta)
        if not np.all(np.isfinite(jacobian)):
            raise ValueError("rtc 候选的雅可比不是有限值")
        self._keep_best(theta, r)
        return r, jacobian


class Context(KitMethods):
    in_check = False

    def __init__(self, backend):
        self.backend, self.cache = backend, {}


def run(case: str, output: Path, seconds: float, iterations: int, codev_home: Path) -> dict:
    from saos.backend.zos import ZosBackend

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {"status": "incomplete", "case": case, "budget_s": seconds, "iterations_limit": iterations,
              "purpose": "第二参考，不参与准入", "objectives": {"rtc": "主波长、固定光瞳点、相对主光线的横向残差平方和",
                                                                 "codev": "ERR CDV 默认加权 RMS 光斑"}}
    cv = None
    try:
        lens = zmx.parse(CASES / f"{case}.zmx")
        original = OpticalSystem(lens)
        lens.aperture_type, lens.aperture_value = "EPD", firstorder.entrance_pupil_diameter(original)
        lens.ray_aiming = 0
        for surface in lens.surfaces:
            surface.solve_radius = surface.solve_thickness = 0
            surface.variable_params.clear()
            for key in ("VDSZ", "VCON", "MAZH"):
                surface.raw.pop(key, None)
        start_path = output / "start.zmx"
        rtc_lens.write(lens, start_path)
        system = OpticalSystem(lens)
        variables = [Variable(0, "curvature"), Variable(len(system.items) - 2, "thickness")]
        sx, sy = system._field_scale()
        fields = [(x / sx if sx else 0.0, y / sy if sy else 0.0) for x, y, _ in lens.fields]
        grid = np.asarray(pupil_grid(9))
        rays = []
        for hx, hy in fields:
            block = trace(system, grid[:, 0], grid[:, 1], hx, hy)
            keep = (block.error == 0) & (block.vig == 0)
            if keep.sum() < 4:
                raise ValueError(f"视场 {(hx, hy)} 可用光线不足 4 条")
            rays += [(hx, hy, px, py) for px, py in grid[keep]]
        design = TimedDesign(system, variables, rays=rays)
        design.jacobian(design.values())
        started = time.monotonic()
        design.deadline = started + seconds
        stopped = None
        try:
            lm(design, design.values(), max_iter=iterations)
        except TimeoutError:
            stopped = "timeout"
        elapsed = time.monotonic() - started
        rtc_endpoint = design.system_with(design.best).lens
        rtc_lens.write(rtc_endpoint, output / "rtc.zmx")
        result["rtc"] = {"elapsed_s": elapsed, "stopped_by": stopped,
                         "values": design.best.tolist(), "objective": design.cost}
        macro = codev_home.resolve() / "macro" / "zemaxos_to_cv.seq"
        if not macro.is_file():
            raise FileNotFoundError(macro)
        if any('"' in str(p) or '\n' in str(p) or ';' in str(p) for p in (macro, start_path)):
            raise ValueError("CODE V 输入路径不能包含引号、分号或换行")
        with CodeV(output, seconds) as cv:
            cv.command(f'in "{macro}" "{start_path}"')
            cv.command("frz sa")
            cv.command("ca ape")
            cv.command(f"cir s{lens.stop_index} 1000000")
            # 导入不是等价性的证据：先核对每个面的曲率、厚度及一阶量，再进行优化。
            for s in lens.surfaces[1:-1]:
                for code, want in (("CUY", s.curvature), ("THI", s.thickness)):
                    got = cv.number(f"({code} S{s.index})")
                    if not math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9):
                        raise ValueError(f"CODE V 导入不一致：面 {s.index} {code}，{got} != {want}")
            efl = cv.number("(EFL)")
            if not math.isclose(efl, firstorder.effl(system), rel_tol=1e-4, abs_tol=1e-6):
                raise ValueError("CODE V 导入后一阶焦距不一致，基准不可比")
            last = system.items[-2].index
            cv.command(f"ccy s1 0; thc s{last} 0")
            result["codev"] = cv.optimize(iterations)
            values = [cv.number("(CUY S1)"), cv.number(f"(THI S{last})")]
            result["codev"]["values"] = values
            result["import"] = {"efl": efl, "fixed_epd_mm": lens.aperture_value, "geometry_agrees": True,
                                 "ray_agreement": "未做 CODE V 逐条光线对账"}
            endpoint = design.system_with(values).lens
            rtc_lens.write(endpoint, output / "codev.zmx")
        zos = ZosBackend()
        try:
            zos.start()
            result["opticstudio"] = {"identity": zos.identity()}
            for name in ("start", "rtc", "codev"):
                zos.load(output / f"{name}.zmx")
                result["opticstudio"][name] = Context(zos).spot(fields, wave=list(range(1, len(lens.active_wavelengths) + 1)),
                                                               grid=15, refocus=False)
        finally:
            zos.close()
        result["status"] = "measured"
        return result
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if cv is not None:
            result["codev_commands"] = cv.log
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=("iris", "reflective"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--codev-home", type=Path, default=Path("C:/CodeV"))
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600 or not 1 <= args.iterations <= 10000:
        parser.error("seconds 必须在 (0, 3600]，iterations 必须在 [1, 10000]")
    run(args.case, args.output, args.seconds, args.iterations, args.codev_home)


if __name__ == "__main__":
    main()
