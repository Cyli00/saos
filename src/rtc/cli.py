"""命令行入口：``rtc sag`` / ``rtc trace`` / ``rtc psf``。

只做"把库里的东西拿出来看一眼"，不实现任何射计算，方便没有 Python 环境的使用方。
所有子命令都接受 ``--wave``（波长 µm，缺省用文件主波长）与 ``--config``（缺省用文件
保存时选中的结构，即 ``MNUM`` 第 2 个数）。
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from . import zmx
from .system import OpticalSystem


def _floats(text: str) -> list[float]:
    return [float(t) for t in text.replace(",", " ").split()]


def _system(args) -> OpticalSystem:
    lens = zmx.parse(args.lens)
    cfg = args.config if args.config is not None else lens.current_config
    return OpticalSystem(lens, wavelength_um=args.wave, config=cfg)


def _write_csv(path: str, header: list[str], rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(header) + "\n")
        for row in rows:
            fh.write(",".join(f"{v:.15g}" if isinstance(v, float) else str(v) for v in row) + "\n")


def cmd_sag(args) -> int:
    system = _system(args)
    item = next((it for it in system.items if it.index == args.surface), None)
    if item is None:
        print(f"没有第 {args.surface} 面（可用的面号：{[it.index for it in system.items]}）", file=sys.stderr)
        return 2
    if args.grid:
        v = np.linspace(-args.half_width, args.half_width, args.grid)
        x, y = np.meshgrid(v, v)
    else:
        x = np.array(_floats(args.x), dtype=float)
        y = np.array(_floats(args.y), dtype=float)
    z, zx, zy = item.shape.sag(x, y)
    z = np.asarray(z, dtype=float)
    rows = [(float(a), float(b), float(c)) for a, b, c in zip(np.ravel(x), np.ravel(y), np.ravel(z))]
    if args.csv:
        _write_csv(args.csv, ["x", "y", "sag"], rows)
        print(f"写到 {args.csv}（{len(rows)} 行）")
    print(f"第 {item.index} 面 {item.type}: n={len(rows)}  z ∈ [{z.min():.12g}, {z.max():.12g}] mm")
    for a, b, c in rows[: args.head]:
        print(f"  x={a:+.9g}  y={b:+.9g}  z={c:+.15g}")
    if len(rows) > args.head:
        print(f"  …（省略 {len(rows) - args.head} 行，用 --head 调大或看 --csv）")
    return 0


def cmd_trace(args) -> int:
    from .trace import trace

    system = _system(args)
    px = np.array(_floats(args.px), dtype=float)
    py = np.array(_floats(args.py), dtype=float)
    px, py = np.broadcast_arrays(px, py)
    block = trace(system, px, py, hx=args.hx, hy=args.hy)
    surf = block.surface_index[-1] if args.surface is None else args.surface
    got = block.at(surf)
    print(
        f"{len(system.items)} 面，stop={system.lens.stop_index}，RAIM={system.lens.ray_aiming}，"
        f"wave={system.wavelength} µm，config={system.config}"
    )
    print(f"第 {surf} 面（px,py → x,y,z,l,m,n,opl,error,vig）")
    rows = []
    for i in range(px.size):
        row = (float(px[i]), float(py[i]), float(got["x"][i]), float(got["y"][i]), float(got["z"][i]),
               float(got["l"][i]), float(got["m"][i]), float(got["n"][i]), float(got["opl"][i]),
               int(block.error[i]), int(block.vig[i]))
        rows.append(row)
        if i < args.head:
            print("  px={:+.4g} py={:+.4g}  x={:+.10g} y={:+.10g} z={:+.10g}  "
                  "l={:+.10g} m={:+.10g} n={:+.10g}  opl={:+.10g}  err={} vig={}".format(*row))
    if len(rows) > args.head:
        print(f"  …（省略 {len(rows) - args.head} 条）")
    if args.csv:
        _write_csv(args.csv, ["px", "py", "x", "y", "z", "l", "m", "n", "opl", "error", "vig"], rows)
        print(f"写到 {args.csv}")
    return 0


def cmd_psf(args) -> int:
    from .psf import opd_map, psf_fft, psf_huygens, rms_wavefront, strehl, strehl_huygens

    system = _system(args)
    print(f"wave={system.wavelength} µm，config={system.config}，n={args.n}，kind={args.kind}")
    print(f"RMS 波前 = {rms_wavefront(system, n=args.n, kind=args.kind):.6g} waves")
    print(f"Strehl(FFT) = {strehl(system, n=args.n, kind=args.kind):.6f}")
    if args.strh:
        print(f"STRH(Huygens, {args.reference}) = "
              f"{strehl_huygens(system, n=args.n, image_pixels=args.pixels, reference=args.reference, kind=args.kind):.6f}")
    if args.mode == "huygens":
        psf, xs, ys = psf_huygens(system, n=args.n, image_pixels=args.pixels,
                                  reference=args.reference, kind=args.kind)
    else:
        psf = psf_fft(system, n=args.n, kind=args.kind)
        xs = ys = np.arange(psf.shape[0], dtype=float)
    opd, mask, _, _ = opd_map(system, n=args.n, kind=args.kind)
    print(f"OPD: 峰值 {np.abs(opd[mask]).max():.6g} waves，采样点 {int(mask.sum())}")
    print(f"PSF {psf.shape}：峰值 {psf.max():.6g}，半高宽 {_fwhm(psf):.3g} 像素")
    if args.csv:
        _write_csv(args.csv, ["row", "x_mm", "y_mm", "intensity"],
                   [(j, float(xs[i]), float(ys[j]), float(psf[j, i]))
                    for j in range(psf.shape[0]) for i in range(psf.shape[1])])
        print(f"写到 {args.csv}")
    return 0


def _fwhm(psf) -> float:
    (j, i) = np.unravel_index(int(np.argmax(psf)), psf.shape)
    half = 0.5 * psf[j, i]
    k = i
    while k + 1 < psf.shape[1] and psf[j, k + 1] > half:
        k += 1
    m = i
    while m - 1 >= 0 and psf[j, m - 1] > half:
        m -= 1
    return float(k - m + 1)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rtc", description="rtc 命令行：面形矢高、批量追迹、PSF")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("lens", help=".zmx 路径")
        p.add_argument("--wave", type=float, default=None, help="波长 µm（缺省用主波长）")
        p.add_argument("--config", type=int, default=None, help="多重结构号（缺省用文件当前结构）")

    p = sub.add_parser("sag", help="算某一面的矢高")
    common(p)
    p.add_argument("--surface", type=int, required=True)
    p.add_argument("--x", default="0", help="x 列表，逗号或空格分隔")
    p.add_argument("--y", default="0", help="y 列表")
    p.add_argument("--grid", type=int, default=0, help="改用 N×N 网格（配合 --half-width）")
    p.add_argument("--half-width", type=float, default=0.1)
    p.add_argument("--head", type=int, default=12, help="屏幕最多打印多少行")
    p.add_argument("--csv", default=None)
    p.set_defaults(func=cmd_sag)

    p = sub.add_parser("trace", help="批量追迹（归一化光瞳坐标）")
    common(p)
    p.add_argument("--px", default="0", help="归一化光瞳 x 列表")
    p.add_argument("--py", default="0", help="归一化光瞳 y 列表")
    p.add_argument("--hx", type=float, default=0.0)
    p.add_argument("--hy", type=float, default=0.0)
    p.add_argument("--surface", type=int, default=None, help="输出哪一面（缺省像面）")
    p.add_argument("--head", type=int, default=20)
    p.add_argument("--csv", default=None)
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("psf", help="OPD / RMS / Strehl / PSF")
    common(p)
    p.add_argument("--mode", choices=("fft", "huygens"), default="fft")
    p.add_argument("--n", type=int, default=32, help="光瞳采样（每边点数）")
    p.add_argument("--pixels", type=int, default=48, help="Huygens 像面网格点数")
    p.add_argument("--kind", choices=("square", "rings"), default="square")
    p.add_argument("--reference", choices=("planar", "spherical"), default="planar")
    p.add_argument("--strh", action="store_true", help="同时算 STRH（Huygens 峰值比）")
    p.add_argument("--csv", default=None)
    p.set_defaults(func=cmd_psf)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
