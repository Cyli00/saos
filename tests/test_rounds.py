from __future__ import annotations

import math

from zea.rounds import decide, significant

INF = math.inf


def test_significant_rules():
    assert significant(INF, 3.0) is True
    assert significant(1.0, INF) is False
    assert significant(1.0, 0.99) is True
    assert significant(1.0, 0.995) is False


def test_pass_and_continue():
    assert decide([2.0])["status"] == "continue"
    assert decide([2.0, 1.0, 0.0])["status"] == "pass"
    assert decide([2.0, 1.0])["status"] == "continue"


def test_stagnation_after_three_flat_rounds():
    assert decide([1.0, 0.999, 0.998])["status"] == "continue"
    out = decide([1.0, 0.999, 0.998, 0.997])
    assert out["status"] == "stagnated" and out["rounds_without_progress"] == 3
    # 中间有一轮明显进步就重新计数
    assert decide([1.0, 0.999, 0.9, 0.899, 0.898])["status"] == "continue"


def test_infinite_rounds_count_as_no_progress():
    assert decide([INF, INF, INF, INF])["status"] == "stagnated"
    assert decide([INF, INF, 5.0])["rounds_without_progress"] == 0


def test_round_limit():
    history = [10.0 * 0.9**k for k in range(21)]
    assert decide(history)["status"] == "limit"
