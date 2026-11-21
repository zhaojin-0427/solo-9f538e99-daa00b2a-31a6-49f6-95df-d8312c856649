"""下发任务：把当前判级图排成逐行折返的分拣指令序列，并按累计完成数分段续取。

编排规则
--------
- 行按 y 递增；偶数 y 行的 x 递增，奇数 y 行的 x 递减（折返走位）。
- 同一行内，相邻（横坐标恰好相差 1）且档位相同的晶粒合并为一条指令；
  中间缺格或档位改变就断开。
- 每条指令记录首粒坐标、横向步进方向（+1 / -1）、档位与粒数。

一致性与续取
------------
- 生成任务时在只读快照内完成排序；封存时在写锁内复核片版本，
  图版已变化则返回 409，不落任何任务数据。
- 任务与全部指令在同一事务内写入；写入失败整体回滚，
  不会留下“有任务号却取不到完整指令”的中间状态。
- 指令一经封存即与后续改图解耦；取数只按 task_id + 累计偏移量走索引查询，
  不重扫 current_map，改图或重启后仍能按原任务续取。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import settings
from .database import write_lock

# 与 services 保持一致的状态码语义
CONFLICT = 409
BAD_REQUEST = 400
NOT_FOUND = 404
INTERNAL = 500


class DispatchError(Exception):
    """下发任务业务错误，http_status 决定返回码。"""

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


@dataclass(frozen=True)
class Instruction:
    x: int
    y: int
    step: int
    bin: str
    die_count: int


def _step_for_row(y: int) -> int:
    """偶数 y 行向右（x 递增），奇数 y 行向左（x 递减）。"""
    return 1 if y % 2 == 0 else -1


def plan_instructions(rows: list[tuple[int, int, str]]) -> list[Instruction]:
    """把按走位顺序排列的 (x, y, bin) 合并成指令。

    ``rows`` 必须已按“y 递增，偶数行 x 递增、奇数行 x 递减”排好。
    同一行内 x 恰好相差 1 且档位相同的相邻晶粒合并；缺格或换档即断开。
    """
    instructions: list[Instruction] = []
    cur: Instruction | None = None
    for x, y, bin_value in rows:
        if (
            cur is not None
            and y == cur.y
            and bin_value == cur.bin
            and x == cur.x + cur.step * cur.die_count
        ):
            cur = Instruction(
                x=cur.x,
                y=cur.y,
                step=cur.step,
                bin=cur.bin,
                die_count=cur.die_count + 1,
            )
        else:
            if cur is not None:
                instructions.append(cur)
            cur = Instruction(
                x=x,
                y=y,
                step=_step_for_row(y),
                bin=bin_value,
                die_count=1,
            )
    if cur is not None:
        instructions.append(cur)
    return instructions


def _collect_snapshot(
    cur: sqlite3.Cursor, slice_id: str, seen_version: int | None = None
) -> tuple[int, list[Instruction]]:
    """在调用方的只读事务内读取片版本并排出全部指令（整片只扫这一遍）。

    若调用方声明的 ``seen_version`` 与快照版本不同，直接判冲突，不白扫整片。
    """
    cur.execute("SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,))
    row = cur.fetchone()
    if row is None:
        raise DispatchError(NOT_FOUND, f"片 {slice_id} 不存在")
    version = int(row[0])
    if seen_version is not None and seen_version != version:
        # 显然过期就不必再扫整片
        raise DispatchError(
            CONFLICT,
            f"所见图版已过期: 当前为 {version}，提交携带的是 {seen_version}",
        )

    # 折返走位直接在 SQL 里定序，避免把 20 万粒再排序一遍。
    # 负奇数在 SQLite 中 %2 仍为非零，与 Python 的奇偶判断一致。
    cur.execute(
        "SELECT x, y, bin FROM current_map WHERE slice_id = ? "
        "ORDER BY y ASC, CASE WHEN y % 2 = 0 THEN x ELSE -x END ASC",
        (slice_id,),
    )
    ordered = [(int(x), int(y), str(b)) for x, y, b in cur.fetchall()]
    return version, plan_instructions(ordered)


def create_task(
    conn: sqlite3.Connection, slice_id: str, seen_version: int | None
) -> dict:
    """按片号与所见图版生成下发任务并封存。

    ``seen_version`` 为调用方看到的图版；为空时以快照版本为准。
    快照与提交时版本不一致（图版已变化）返回 409。
    """
    # 1) 只读快照内完成排序；不持写锁，期间允许复测上传/改向继续提交。
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        version, instructions = _collect_snapshot(cur, slice_id, seen_version)
    finally:
        conn.rollback()

    total_dies = sum(inst.die_count for inst in instructions)

    # 2) 写锁内复核图版并整任务单事务封存。
    with write_lock:
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,))
            row = cur.fetchone()
            if row is None:
                raise DispatchError(NOT_FOUND, f"片 {slice_id} 不存在")
            if int(row[0]) != version:
                raise DispatchError(
                    CONFLICT,
                    f"图版已变化: 编排时为 {version}，封存时为 {int(row[0])}，请重新生成任务",
                )

            cur.execute(
                "INSERT INTO dispatch_task(slice_id, version, total_dies, instruction_count) "
                "VALUES (?, ?, ?, ?)",
                (slice_id, version, total_dies, len(instructions)),
            )
            task_id = int(cur.lastrowid)

            start_die = 0
            payload = []
            for seq, inst in enumerate(instructions):
                payload.append(
                    (
                        task_id,
                        seq,
                        start_die,
                        inst.x,
                        inst.y,
                        inst.step,
                        inst.bin,
                        inst.die_count,
                    )
                )
                start_die += inst.die_count
            cur.executemany(
                "INSERT INTO dispatch_instruction"
                "(task_id, seq, start_die, x, y, step, bin, die_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        except DispatchError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise DispatchError(INTERNAL, f"写入失败，任务未封存: {exc}") from exc

    return {
        "task_id": task_id,
        "slice_id": slice_id,
        "version": version,
        "total_dies": total_dies,
        "instruction_count": len(instructions),
    }


def get_task(conn: sqlite3.Connection, slice_id: str, task_id: int) -> dict:
    """查看已封存任务的元信息（不重扫整片）。"""
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "SELECT slice_id, version, total_dies, instruction_count "
            "FROM dispatch_task WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None or str(row[0]) != slice_id:
            raise DispatchError(NOT_FOUND, f"片 {slice_id} 不存在任务 {task_id}")
        version, total, count = int(row[1]), int(row[2]), int(row[3])
    finally:
        conn.rollback()
    return {
        "task_id": task_id,
        "slice_id": slice_id,
        "version": version,
        "total_dies": total,
        "instruction_count": count,
    }


def _segment(
    task_id: int,
    slice_id: str,
    version: int,
    total_dies: int,
    completed_before: int,
    done: bool,
    instructions: list[dict],
    completed: int | None = None,
) -> dict:
    """统一组织续取响应。

    ``completed`` 表示执行完本段后的累计粒数；``next_completed`` 为同义别名，
    便于调用方直接回传。``completed_before`` 为本次取数前的累计粒数。
    """
    after = completed_before if completed is None else completed
    return {
        "task_id": task_id,
        "slice_id": slice_id,
        "version": version,
        "total_dies": total_dies,
        "completed_before": completed_before,
        "completed": after,
        "next_completed": after,
        "done": done,
        "instructions": instructions,
    }


def get_instructions(
    conn: sqlite3.Connection,
    task_id: int,
    completed: int,
    slice_id: str | None = None,
) -> dict:
    """凭任务号和累计完成粒数，从下一粒起取最多 128 条 / 4096 粒。

    首粒若落在一条指令中间，返回该指令的剩余片段；尾段若触及上限，
    也会在一条指令中间截开。返回的 ``next_completed`` 可直接作为下次调用的
    ``completed``，连续取完时原图每粒恰好出现一次。
    """
    max_instructions = settings.dispatch_max_instructions
    max_dies = settings.dispatch_max_dies

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "SELECT slice_id, version, total_dies FROM dispatch_task WHERE task_id = ?",
            (task_id,),
        )
        task = cur.fetchone()
        if task is None or (slice_id is not None and str(task[0]) != slice_id):
            raise DispatchError(NOT_FOUND, f"任务 {task_id} 不存在")
        task_slice_id = str(task[0])
        version, total_dies = int(task[1]), int(task[2])

        if completed < 0 or completed > total_dies:
            raise DispatchError(
                BAD_REQUEST,
                f"累计完成粒数 {completed} 越界: 任务共 {total_dies} 粒",
            )

        if completed == total_dies:
            return _segment(task_id, task_slice_id, version, total_dies, completed, True, [])

        # 定位“包含下一粒”的指令：start_die <= completed < start_die + die_count
        cur.execute(
            "SELECT seq, start_die, x, y, step, bin, die_count "
            "FROM dispatch_instruction "
            "WHERE task_id = ? AND start_die <= ? AND start_die + die_count > ? "
            "ORDER BY start_die DESC LIMIT 1",
            (task_id, completed, completed),
        )
        first = cur.fetchone()
        if first is None:
            # 指令序列应连续铺满 [0, total)；出现空洞说明任务数据损坏，拒绝误导续取
            raise DispatchError(
                INTERNAL,
                f"任务 {task_id} 的指令序列不完整，无法从第 {completed} 粒续取",
            )

        # 一次索引区间查询取够候选；只读任务表，不碰 current_map。
        cur.execute(
            "SELECT seq, start_die, x, y, step, bin, die_count "
            "FROM dispatch_instruction WHERE task_id = ? AND seq >= ? "
            "ORDER BY seq LIMIT ?",
            (task_id, int(first[0]), max_instructions),
        )
        candidates = cur.fetchall()
    finally:
        conn.rollback()

    first_offset = completed - int(first[1])
    remaining_dies = max_dies
    fragments: list[dict] = []
    cursor_die = completed

    for row in candidates:
        if len(fragments) >= max_instructions or remaining_dies <= 0:
            break
        seq, start_die, x, y, step, bin_value, die_count = row
        seq, start_die = int(seq), int(start_die)
        x, y, step = int(x), int(y), int(step)
        die_count = int(die_count)
        skip = first_offset if seq == int(first[0]) else 0
        available = die_count - skip
        if available <= 0:
            continue
        take = min(available, remaining_dies)
        fragments.append(
            {
                "x": x + step * skip,
                "y": y,
                "step": step,
                "bin": str(bin_value),
                "die_count": take,
            }
        )
        remaining_dies -= take
        cursor_die += take

    return _segment(
        task_id,
        task_slice_id,
        version,
        total_dies,
        completed,
        cursor_die == total_dies,
        fragments,
        cursor_die,
    )
