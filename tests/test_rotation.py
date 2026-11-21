"""逆时针旋转变换单元测试。"""
from __future__ import annotations

import pytest

from app.rotation import rotate_ccw


def test_rotation_zero():
    assert rotate_ccw(3, -2, 0) == (3, -2)


def test_rotation_90_ccw():
    assert rotate_ccw(1, 0, 90) == (0, 1)
    assert rotate_ccw(0, 1, 90) == (-1, 0)
    assert rotate_ccw(2, 3, 90) == (-3, 2)


def test_rotation_180():
    assert rotate_ccw(2, 3, 180) == (-2, -3)


def test_rotation_270_ccw():
    assert rotate_ccw(1, 0, 270) == (0, -1)
    assert rotate_ccw(2, 3, 270) == (3, -2)


def test_four_rotations_return_to_origin():
    # 连续转四次 90° 回到原点
    x, y = 5, -7
    for _ in range(4):
        x, y = rotate_ccw(x, y, 90)
    assert (x, y) == (5, -7)


def test_invalid_angle():
    with pytest.raises(ValueError):
        rotate_ccw(0, 0, 45)
