"""真实光线的梯度与优化退出回归；差分只用于检验，不进入优化器。"""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
import torch

from saos import prescription
from saos.backend.optiland import OptilandBackend
from saos.backend import optiland_opt, optiland_torch
from saos.backend.optiland_budget import Snapshot
from saos.backend.optiland_guard import DomainPenalty, InvalidTrial, aiming_without_records
from test_lens_snapshot_files import PRESCRIPTION


@pytest.fixture
def lens():
    b = OptilandBackend()
    b.start()
    b.build(prescription.normalize(copy.deepcopy(PRESCRIPTION)))
    b.vary(2, "thickness", True)
    b.set_bounds(2, "thickness", 60., 95.)
    b._merit = [{"type": "TOTR", "int1": 0, "int2": 0, "data": [], "target": 70., "weight": 1.}]
    try:
        yield b
    finally:
        b.close()


def ad_value_gradient(b, name, field):
    import optiland.backend as be

    saved = Snapshot(b)
    problem = optiland_opt.Problem(b, [], None, 1.)
    try:
        b._torch_device = "cpu"
        b._synchronized = None
        b._sync_optical_state()
        state = b._torch_state = optiland_torch.TorchState(b, problem)
        x = torch.tensor(problem.x0, dtype=torch.float64, requires_grad=True)
        with be.grad_mode.temporary_enable():
            for k, (i, column) in enumerate(problem.keys):
                value = x[k] / problem.unit[k]
                if column == "radius":
                    value = 1 / value
                state.write(i, column, value * b._scales.get((i, column), 1.))
            state.settle()
            value, bad = optiland_torch.operand(b, state, name, 3, 1, [0., field])
            gradient = torch.autograd.grad(value, x)[0]
            assert bad == 0
            return float(value.detach()), gradient.detach().numpy().copy()
    finally:
        saved.restore(b)


@pytest.mark.parametrize("model", ["singlet", "cooke"])
@pytest.mark.parametrize("field", [0., 1.])
def test_rwce_reference_geometry_gradient_matches_live_optic(lens, model, field):
    b = lens
    if model == "cooke":
        path = Path(__file__).resolve().parents[1] / "library/starts/entries/cooke/prescription.json"
        p = json.loads(path.read_text(encoding="utf-8"))
        p["ray_aiming"] = "real"
        b.build(prescription.normalize(p))
    for i in range(1, len(b._rows) - 1):
        b.vary(i, "radius", True)
        b.vary(i, "thickness", True)
    value, ad = ad_value_gradient(b, "RWCE", field)
    assert value == pytest.approx(b.operand("RWCE", 3, 1, [0., field]), rel=1e-10, abs=1e-10)
    problem = optiland_opt.Problem(b, [], None, 1.)
    fd = []
    # 缩放坐标步长 1e-6；允许 1e-4 相对误差，兼顾差分舍入和瞄准收敛误差。
    h = 1e-6
    for i in range(len(problem.x0)):
        up, down = problem.x0.copy(), problem.x0.copy()
        up[i] += h
        down[i] -= h
        problem.apply(up)
        upper = b.operand("RWCE", 3, 1, [0., field])
        problem.apply(down)
        lower = b.operand("RWCE", 3, 1, [0., field])
        fd.append((upper - lower) / (2 * h))
    assert np.max(np.abs(ad - fd)) <= 1e-4 * max(np.max(np.abs(fd)), 1e-12), (ad, fd)


@pytest.mark.parametrize("algorithm", ["torch-trf", "torch-lbfgs", "torch-adam"])
def test_nonfinite_trial_is_rejected_and_optimization_continues(lens, monkeypatch, algorithm, tmp_path):
    original = DomainPenalty.trace
    calls = 0
    injected = False

    def trace(self, *args):
        nonlocal calls, injected
        calls += 1
        thickness = float(self.backend._torch_state.thickness(2).detach())
        if not injected and abs(thickness - 95.) > 0.01:
            injected = True
            self.add(torch.tensor(float("nan"), dtype=torch.float64))
        return original(self, *args)

    monkeypatch.setattr(DomainPenalty, "trace", trace)
    result = lens.optimize(cycles="10", algorithm=algorithm, max_seconds=30)
    assert calls > 3 and injected
    assert result["final_merit"] < 0.99 * result["initial_merit"], result
    assert result["admissible_result"] and result["accepted_steps"] > 0
    assert result["rejected_steps"] > 0
    assert abs(lens.operand("TOTR", 0, 0, []) - 70.) == pytest.approx(result["final_merit"], abs=1e-8)
    path = tmp_path / "accepted.zmx"
    lens.save_as(path)
    lens.load(path)
    assert abs(lens.operand("TOTR", 0, 0, []) - 70.) == pytest.approx(result["final_merit"], abs=1e-8)


def test_glass_trial_runtime_error_restores_verified_lens(lens, monkeypatch, tmp_path):
    original = optiland_opt.optimize
    verified = []

    def optimize(b, **kwargs):
        if verified:
            raise RuntimeError("试算异常")
        result = original(b, **kwargs)
        verified.append(copy.deepcopy(b._prescription))
        return result

    monkeypatch.setattr(optiland_opt, "optimize", optimize)
    with pytest.raises(RuntimeError, match="试算异常"):
        lens.optimize_glass(surfaces=[1], catalog="SAOS_CDGM_INPROD", neighbours=2, cycles="1", max_seconds=60)
    assert verified and lens._prescription == verified[0]
    expected = lens.operand("TOTR", 0, 0, [])
    path = tmp_path / "restored.zmx"
    lens.save_as(path)
    lens.load(path)
    assert lens.operand("TOTR", 0, 0, []) == pytest.approx(expected, abs=1e-10)
    assert not hasattr(lens, "_opt_budget") and not hasattr(lens, "_glass_budget")


def test_glass_first_settle_timeout_records_algorithm(lens, monkeypatch):
    original = DomainPenalty.trace

    def trace(self, *args):
        lens._glass_budget.started -= 1000
        lens._opt_budget.started -= 1000
        return original(self, *args)

    monkeypatch.setattr(DomainPenalty, "trace", trace)
    before = copy.deepcopy(lens._prescription)
    result = lens.optimize_glass(surfaces=[1], catalog="SAOS_CDGM_INPROD", neighbours=2, cycles="1", max_seconds=60)
    assert result["stopped_by"] == "timeout", result
    assert result["local_algorithms"] == [{"algorithm": "torch-trf", "requested_algorithm": "auto", "fallback": None}]
    assert lens._prescription == before
    assert not hasattr(lens, "_opt_budget") and not hasattr(lens, "_glass_budget")


@pytest.mark.parametrize("algorithm", ["torch-trf", "torch-lbfgs"])
def test_nonfinite_initial_domain_still_raises_and_restores(lens, monkeypatch, algorithm):
    before = copy.deepcopy(lens._prescription)

    def trace(self, *args):
        self.add(torch.tensor(float("inf"), dtype=torch.float64))

    monkeypatch.setattr(DomainPenalty, "trace", trace)
    with pytest.raises(InvalidTrial, match="non-finite"):
        lens.optimize(cycles="1", algorithm=algorithm, max_seconds=30)
    assert lens._prescription == before
    assert lens._torch_state is None and lens._torch_device is None
    assert not hasattr(lens, "_opt_budget")


def test_aiming_guard_forces_no_records_and_restores_on_error(lens, monkeypatch):
    surface = lens.optic.surfaces[1]
    records = []

    def trace(rays, record=False):
        records.append(record)

    monkeypatch.setattr(surface, "trace", trace)
    with pytest.raises(RuntimeError, match="退出"):
        with aiming_without_records(lens.optic):
            surface.trace(None, record=True)
            with aiming_without_records(lens.optic):
                surface.trace(None, record=True)
            raise RuntimeError("退出")
    surface.trace(None, record=True)
    assert records == [False, False, True]
    assert surface.trace is trace
