"""为中心系统的实际光线瞄准补上隐式导数，保留原瞄准器的前向光线。"""

from __future__ import annotations

from contextlib import contextmanager

from saos.protocol import ErrorCode, SaosError
from saos.backend.optiland_guard import aiming_without_records


@contextmanager
def differentiable_aiming(backend):
    import torch
    import optiland.backend as be
    from optiland.rays import RealRays
    from optiland.rays.ray_aiming.paraxial import ParaxialRayAimer

    if backend._aiming == "paraxial":
        yield
        return
    from saos.backend.optiland_torch import TorchUnavailable
    if (backend._effective["fields"]["type"] not in ("angle", "object_height")
            or any(row.get("decenter_tilt") or row.get("type", "").lower() == "coordinatebreak"
                   or str(row.get("material", "")).upper() == "MIRROR" for row in backend._rows)):
        raise TorchUnavailable("aiming_geometry", "differentiable real-ray aiming requires a centered, unfolded system with angle/object-height fields")

    optic = backend.optic
    generator = optic.ray_tracer.ray_generator
    if getattr(generator, "_saos_differentiable_aiming", False):
        yield
        return
    original = generator.generate_rays
    own = vars(generator).get("generate_rays")
    stop = int(optic.surfaces.stop_index)
    infinite = bool(optic.object_surface.is_infinite)

    def reference_radius():
        if backend._effective["aperture"]["type"] == "float_by_stop":
            from optiland.rays.ray_aiming.initialization import FloatByStopStrategy
            return be.array(FloatByStopStrategy(optic).calculate_stop_radius())
        coords = ParaxialRayAimer(optic).aim_rays((0., 0.), optic.primary_wavelength, (be.array([0.]), be.array([1.])))
        ray = RealRays(*coords, intensity=be.array([1.]), wavelength=be.array([optic.primary_wavelength]))
        for surface in optic.surfaces[1:stop + 1]:
            surface.trace(ray, record=False)
        optic.surfaces[stop].geometry.cs.localize(ray)
        radius = torch.sqrt(ray.x.square() + ray.y.square()).reshape(())
        if not torch.isfinite(radius):
            heights, _ = optic.paraxial.marginal_ray()
            radius = heights[stop].abs().reshape(())
        return radius

    def generate(hx, hy, px, py, wavelength, *, retain_launch=False):
        enabled = torch.is_grad_enabled() and be.grad_mode.requires_grad
        old_grad = be.grad_mode.requires_grad
        try:
            be.grad_mode.disable()
            with torch.no_grad(), aiming_without_records(optic):
                seed = original(hx, hy, px, py, wavelength)
        finally:
            be.grad_mode.requires_grad = old_grad
        if not enabled:
            if retain_launch:
                seed._capture_launch_state()
            return seed
        radius = reference_radius()
        values = [getattr(seed, name).detach() for name in ("x", "y", "z", "L", "M", "N")]
        valid = torch.stack([torch.isfinite(value) for value in values]).all(dim=0)
        if not valid.any():
            return seed
        base = [v[valid] for v in values]
        q = torch.zeros((int(valid.sum()), 2), dtype=torch.float64, device=values[0].device, requires_grad=True)

        def launch(offset):
            x, y, z, l, m, n = base
            if infinite:
                x, y = x + offset[:, 0], y + offset[:, 1]
            else:
                object_z = be.array(optic.object_surface.geometry.cs.z)
                z = z + object_z - object_z.detach()
                l, m = l + offset[:, 0], m + offset[:, 1]
                n = n.sign() * torch.sqrt(1 - l.square() - m.square())
            return RealRays(x, y, z, l, m, n, intensity=seed.i[valid], wavelength=seed.w[valid])

        rays = launch(q)
        for surface in optic.surfaces[:stop + 1]:
            surface.trace(rays, record=False)
        optic.surfaces[stop].geometry.cs.localize(rays)
        pupil_x = torch.broadcast_to(be.array(px), valid.shape)[valid]
        pupil_y = torch.broadcast_to(be.array(py), valid.shape)[valid]
        residual = torch.stack((rays.x - pupil_x * radius, rays.y - pupil_y * radius), dim=-1)
        jac = torch.stack([torch.autograd.grad(residual[:, i].sum(), q, retain_graph=True)[0] for i in range(2)], dim=1).detach()
        if not torch.isfinite(jac).all() or not torch.isfinite(residual).all():
            raise SaosError(ErrorCode.OPTILAND_ERROR, "non-finite aiming Jacobian")
        # dq/dp = -Fq^-1 Fp；减去 detach 后前向修正严格为零，不改变原生瞄准的解。
        correction, info = torch.linalg.solve_ex(jac, (residual - residual.detach()).unsqueeze(-1))
        if torch.any(info != 0):
            raise SaosError(ErrorCode.OPTILAND_ERROR, "singular aiming Jacobian")
        attached = launch(-correction.squeeze(-1))
        for name, value in zip(("x", "y", "z", "L", "M", "N"), values):
            result = value.clone()
            result[valid] = getattr(attached, name)
            setattr(seed, name, result)
        if retain_launch:
            seed._capture_launch_state()
        return seed

    generator.generate_rays = generate
    generator._saos_differentiable_aiming = True
    try:
        yield
    finally:
        del generator._saos_differentiable_aiming
        if own is None:
            del generator.generate_rays
        else:
            generator.generate_rays = own
