"""CSV 解析：每行是以圆心为原点的整数坐标 + 档位，同份坐标唯一。

要求表头为：x,y,bin
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .config import settings


@dataclass(frozen=True)
class DieRow:
    x: int
    y: int
    bin: str


class CsvError(ValueError):
    """CSV 内容不合法。"""


def parse_die_csv(raw: bytes) -> list[DieRow]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("CSV 必须是 UTF-8 编码") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError("CSV 为空，必须包含表头 x,y,bin")

    if [h.strip().lower() for h in header] != ["x", "y", "bin"]:
        raise CsvError("表头必须为 x,y,bin")

    rows: list[DieRow] = []
    seen: set[tuple[int, int]] = set()
    max_bin_len = settings.max_bin_len
    for line_no, fields in enumerate(reader, start=2):
        if not fields or all(not f.strip() for f in fields):
            # 容忍文件末尾空行
            continue
        if len(fields) != 3:
            raise CsvError(f"第 {line_no} 行字段数应为 3（x,y,bin），实际为 {len(fields)}")
        sx, sy, sb = (f.strip() for f in fields)
        try:
            x = int(sx)
            y = int(sy)
        except ValueError as exc:
            raise CsvError(f"第 {line_no} 行坐标必须是整数: ({sx}, {sy})") from exc
        if not sb:
            raise CsvError(f"第 {line_no} 行档位不能为空")
        if len(sb) > max_bin_len:
            raise CsvError(f"第 {line_no} 行档位长度超过 {max_bin_len}")
        coord = (x, y)
        if coord in seen:
            raise CsvError(f"第 {line_no} 行坐标重复: ({x}, {y})，同份坐标必须唯一")
        seen.add(coord)
        rows.append(DieRow(x=x, y=y, bin=sb))

    if not rows:
        raise CsvError("CSV 没有任何数据行")
    return rows
