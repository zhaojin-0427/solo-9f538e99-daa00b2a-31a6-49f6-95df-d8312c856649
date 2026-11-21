"""以圆心为原点的逆时针旋转坐标变换。"""
from __future__ import annotations


def rotate_ccw(x: int, y: int, angle: int) -> tuple[int, int]:
    """把 (x, y) 绕原点逆时针旋转 angle 度（仅支持 0/90/180/270）。

    整数坐标旋转后仍是整数坐标：
      90°  逆时针: (x, y) -> (-y, x)
      180°       : (x, y) -> (-x, -y)
      270° 逆时针: (x, y) -> (y, -x)
    """
    if angle == 0:
        return x, y
    if angle == 90:
        return -y, x
    if angle == 180:
        return -x, -y
    if angle == 270:
        return y, -x
    raise ValueError(f"不支持的旋转角: {angle}")
