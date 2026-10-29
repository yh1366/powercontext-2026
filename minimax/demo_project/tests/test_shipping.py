"""运费回归测试（SPEC-002）——TASK-0007 不得改变这些行为。"""

from __future__ import annotations

import pytest

from ordercore.shipping import calc_shipping


def test_normal_region_base_fee() -> None:
    assert calc_shipping(4_165) == 800


def test_normal_region_free_at_threshold() -> None:
    assert calc_shipping(9_900) == 0


def test_remote_region_adds_surcharge() -> None:
    assert calc_shipping(4_165, "remote") == 2_000


def test_remote_region_free_at_higher_threshold() -> None:
    assert calc_shipping(19_899, "remote") == 2_000
    assert calc_shipping(19_900, "remote") == 0


def test_negative_amount_is_rejected() -> None:
    with pytest.raises(ValueError):
        calc_shipping(-1)
