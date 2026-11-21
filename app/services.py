"""判级图核心业务逻辑。

并发与一致性：
- 所有写操作持进程级 write_lock，并在单个 SQLite 事务内完成；
  上传和改向交错时严格按提交（拿到锁）顺序生效。
- 每个片维护单调递增的 current_ver：每次成功提交 +1，并作为该批的修订号返回。
- 读操作不开写锁，在单个只读事务里取快照，每页只读到某个已提交状态。
"""
from __future__ import annotations

import sqlite3

from .config import settings
from .csv_utils import DieRow
from .database import write_lock
from .rotation import rotate_ccw


class ServiceError(Exception):
    """业务错误，http_status 决定返回码。"""

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


# 409 冲突：状态冲突 / 修订过期
CONFLICT = 409
# 422：提交内容本身不合法（落点不在首测图上等）
UNPROCESSABLE = 422
# 400：参数格式错误
BAD_REQUEST = 400


def _touch_slice(cur: sqlite3.Cursor, slice_id: str) -> int:
    """把片版本号 +1 并返回新版本号（片不存在则创建，版本从 1 开始）。"""
    cur.execute(
        """
        INSERT INTO slice(slice_id, current_ver) VALUES (?, 1)
        ON CONFLICT(slice_id) DO UPDATE SET current_ver = current_ver + 1
        """,
        (slice_id,),
    )
    cur.execute("SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,))
    return int(cur.fetchone()[0])


def _load_first_coords(cur: sqlite3.Cursor, slice_id: str) -> set[tuple[int, int]]:
    cur.execute("SELECT x, y FROM first_test WHERE slice_id = ?", (slice_id,))
    return {(int(r[0]), int(r[1])) for r in cur.fetchall()}


def _recompute(cur: sqlite3.Cursor, slice_id: str) -> int:
    """按物化 coverage 重算受影响晶粒的 current_map。

    每粒取覆盖它的最高批号档位；无复测覆盖时回退首测。
    只重算临时表 affected 中列出的晶粒，不重扫整图、不重算无关晶粒。
    """
    # 1) 受影响晶粒若仍被复测覆盖，取最高批号的档位（批号按检测先后编，越大越新）
    cur.execute(
        """
        UPDATE current_map
           SET bin = picked.bin,
               source = 'retest',
               batch_no = picked.batch_no
          FROM (
                SELECT c.slice_id, c.x, c.y, c.batch_no, c.bin
                  FROM coverage c
                  JOIN affected a
                    ON a.slice_id = c.slice_id AND a.x = c.x AND a.y = c.y
                 WHERE c.batch_no = (
                       SELECT MAX(c2.batch_no)
                         FROM coverage c2
                        WHERE c2.slice_id = c.slice_id
                          AND c2.x = c.x AND c2.y = c.y)
               ) AS picked
         WHERE current_map.slice_id = picked.slice_id
           AND current_map.x = picked.x
           AND current_map.y = picked.y
        """
    )
    # 2) 已无复测覆盖的受影响晶粒回退首测档位
    cur.execute(
        """
        UPDATE current_map
           SET bin = ft.bin,
               source = 'first',
               batch_no = NULL
          FROM first_test ft
         WHERE current_map.slice_id = ft.slice_id
           AND current_map.x = ft.x
           AND current_map.y = ft.y
           AND current_map.slice_id = ?
           AND (current_map.x, current_map.y) IN (SELECT x, y FROM affected)
           AND NOT EXISTS (
                 SELECT 1 FROM coverage c
                  WHERE c.slice_id = current_map.slice_id
                    AND c.x = current_map.x AND c.y = current_map.y)
        """,
        (slice_id,),
    )
    cur.execute("SELECT COUNT(*) FROM affected")
    return int(cur.fetchone()[0])


def submit_first_test(
    conn: sqlite3.Connection, slice_id: str, rows: list[DieRow]
) -> dict:
    """每片收一份首测，最多 max_first_dies 粒。"""
    if len(rows) > settings.max_first_dies:
        raise ServiceError(
            UNPROCESSABLE,
            f"首测晶粒数 {len(rows)} 超过每片上限 {settings.max_first_dies}",
        )

    with write_lock:
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT 1 FROM slice WHERE slice_id = ?", (slice_id,))
            if cur.fetchone() is not None:
                raise ServiceError(CONFLICT, f"片 {slice_id} 已存在首测，每片仅可上传一份")

            revision = _touch_slice(cur, slice_id)
            cur.executemany(
                "INSERT INTO first_test(slice_id, x, y, bin) VALUES (?, ?, ?, ?)",
                [(slice_id, r.x, r.y, r.bin) for r in rows],
            )
            # 首测提交即当前图，来源全部为 first
            cur.executemany(
                "INSERT INTO current_map(slice_id, x, y, bin, source, batch_no) "
                "VALUES (?, ?, ?, ?, 'first', NULL)",
                [(slice_id, r.x, r.y, r.bin) for r in rows],
            )
            conn.commit()
        except ServiceError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ServiceError(500, f"写入失败，已保留原图: {exc}") from exc

    return {"slice_id": slice_id, "revision": revision, "die_count": len(rows)}


def submit_batch(
    conn: sqlite3.Connection,
    slice_id: str,
    batch_no: int,
    rotation: int,
    rows: list[DieRow],
) -> dict:
    """复测批次入库：校验旋转落点全在首测图上，再物化覆盖并重算受影响晶粒。"""
    if batch_no <= 0:
        raise ServiceError(BAD_REQUEST, "批号必须为正整数")
    if rotation not in (0, 90, 180, 270):
        raise ServiceError(BAD_REQUEST, "旋转角只允许 0、90、180、270 度")

    # 旋转与落点校验在锁外先做（纯计算），缩短写事务
    landed: list[tuple[int, int, str]] = []
    seen_landed: set[tuple[int, int]] = set()
    for r in rows:
        lx, ly = rotate_ccw(r.x, r.y, rotation)
        if (lx, ly) in seen_landed:
            raise ServiceError(
                UNPROCESSABLE,
                f"批号 {batch_no} 旋转后落点重复: ({lx}, {ly})，同批坐标必须唯一",
            )
        seen_landed.add((lx, ly))
        landed.append((lx, ly, r.bin))

    with write_lock:
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT 1 FROM slice WHERE slice_id = ?", (slice_id,))
            if cur.fetchone() is None:
                raise ServiceError(UNPROCESSABLE, f"片 {slice_id} 尚未上传首测")
            cur.execute(
                "SELECT 1 FROM batch WHERE slice_id = ? AND batch_no = ?",
                (slice_id, batch_no),
            )
            if cur.fetchone() is not None:
                raise ServiceError(
                    CONFLICT, f"片 {slice_id} 的批号 {batch_no} 已存在，同片批号必须唯一"
                )

            first_coords = _load_first_coords(cur, slice_id)
            invalid = [(lx, ly) for (lx, ly, _) in landed if (lx, ly) not in first_coords]
            if invalid:
                sample = ", ".join(f"({x},{y})" for x, y in invalid[:5])
                more = "" if len(invalid) <= 5 else f" 等共 {len(invalid)} 个"
                raise ServiceError(
                    UNPROCESSABLE,
                    f"批号 {batch_no} 有旋转落点不在首测图上: {sample}{more}",
                )

            revision = _touch_slice(cur, slice_id)
            cur.execute(
                "INSERT INTO batch(slice_id, batch_no, rotation, revision) VALUES (?, ?, ?, ?)",
                (slice_id, batch_no, rotation, revision),
            )
            # 原表：保存上传时的原始坐标，改向时沿用
            cur.executemany(
                "INSERT INTO batch_row(slice_id, batch_no, x, y, bin) VALUES (?, ?, ?, ?, ?)",
                [(slice_id, batch_no, r.x, r.y, r.bin) for r in rows],
            )
            # 物化覆盖：旋转落点 -> 本批
            cur.executemany(
                "INSERT INTO coverage(slice_id, x, y, batch_no, bin) VALUES (?, ?, ?, ?, ?)",
                [(slice_id, lx, ly, batch_no, b) for (lx, ly, b) in landed],
            )

            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS affected(slice_id TEXT, x INTEGER, y INTEGER, "
                "PRIMARY KEY(slice_id, x, y))"
            )
            cur.execute("DELETE FROM affected")
            cur.executemany(
                "INSERT OR IGNORE INTO affected(slice_id, x, y) VALUES (?, ?, ?)",
                [(slice_id, lx, ly) for (lx, ly, _) in landed],
            )
            affected = _recompute(cur, slice_id)
            cur.execute("DROP TABLE affected")
            conn.commit()
        except ServiceError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ServiceError(500, f"写入失败，已保留原图和批次: {exc}") from exc

    return {
        "slice_id": slice_id,
        "batch_no": batch_no,
        "rotation": rotation,
        "revision": revision,
        "die_count": affected,
    }


def reorient_batch(
    conn: sqlite3.Connection,
    slice_id: str,
    batch_no: int,
    new_rotation: int,
    expected_revision: int,
) -> dict:
    """改向：带该批所见修订号做乐观并发控制，只重算原覆盖与现覆盖的晶粒。"""
    if new_rotation not in (0, 90, 180, 270):
        raise ServiceError(BAD_REQUEST, "旋转角只允许 0、90、180、270 度")

    with write_lock:
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "SELECT rotation, revision FROM batch WHERE slice_id = ? AND batch_no = ?",
                (slice_id, batch_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ServiceError(
                    UNPROCESSABLE, f"片 {slice_id} 不存在批号 {batch_no}"
                )
            old_rotation, current_revision = int(row[0]), int(row[1])
            if current_revision != expected_revision:
                raise ServiceError(
                    CONFLICT,
                    f"修订号过期: 该批当前修订号为 {current_revision}，"
                    f"提交携带的是 {expected_revision}",
                )

            # 沿用原表，用新角度重算落点
            cur.execute(
                "SELECT x, y, bin FROM batch_row WHERE slice_id = ? AND batch_no = ?",
                (slice_id, batch_no),
            )
            original = [(int(r[0]), int(r[1]), str(r[2])) for r in cur.fetchall()]
            first_coords = _load_first_coords(cur, slice_id)
            new_landed: list[tuple[int, int, str]] = []
            seen: set[tuple[int, int]] = set()
            for ox, oy, b in original:
                nx, ny = rotate_ccw(ox, oy, new_rotation)
                if (nx, ny) in seen:
                    raise ServiceError(
                        UNPROCESSABLE,
                        f"改向后落点重复: ({nx}, {ny})，同批坐标必须唯一",
                    )
                seen.add((nx, ny))
                if (nx, ny) not in first_coords:
                    raise ServiceError(
                        UNPROCESSABLE,
                        f"改向后落点 ({nx}, {ny}) 不在首测图上，已保留原图和批次",
                    )
                new_landed.append((nx, ny, b))

            # 受影响晶粒 = 这批原先覆盖 ∪ 现在覆盖（去重在 affected 表内完成）
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS affected(slice_id TEXT, x INTEGER, y INTEGER, "
                "PRIMARY KEY(slice_id, x, y))"
            )
            cur.execute("DELETE FROM affected")
            cur.execute(
                "INSERT OR IGNORE INTO affected(slice_id, x, y) "
                "SELECT slice_id, x, y FROM coverage WHERE slice_id = ? AND batch_no = ?",
                (slice_id, batch_no),
            )
            cur.executemany(
                "INSERT OR IGNORE INTO affected(slice_id, x, y) VALUES (?, ?, ?)",
                [(slice_id, nx, ny) for (nx, ny, _) in new_landed],
            )

            # 删旧覆盖、插新覆盖
            cur.execute(
                "DELETE FROM coverage WHERE slice_id = ? AND batch_no = ?",
                (slice_id, batch_no),
            )
            cur.executemany(
                "INSERT INTO coverage(slice_id, x, y, batch_no, bin) VALUES (?, ?, ?, ?, ?)",
                [(slice_id, nx, ny, batch_no, b) for (nx, ny, b) in new_landed],
            )

            affected = _recompute(cur, slice_id)

            revision = _touch_slice(cur, slice_id)
            cur.execute(
                "UPDATE batch SET rotation = ?, revision = ? WHERE slice_id = ? AND batch_no = ?",
                (new_rotation, revision, slice_id, batch_no),
            )
            cur.execute("DROP TABLE affected")
            conn.commit()
        except ServiceError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ServiceError(500, f"写入失败，已保留原图和批次: {exc}") from exc

    return {
        "slice_id": slice_id,
        "batch_no": batch_no,
        "rotation": new_rotation,
        "revision": revision,
        "affected_dies": affected,
    }


def get_map_page(
    conn: sqlite3.Connection,
    slice_id: str,
    page: int,
    page_size: int,
    expected_version: int | None = None,
) -> dict:
    """按片号分页取当前图，只读 current_map，不重扫各批原表。"""
    if page < 1:
        raise ServiceError(BAD_REQUEST, "页码 page 从 1 开始")
    if page_size < 1 or page_size > settings.max_page_size:
        raise ServiceError(
            BAD_REQUEST, f"页大小需在 1 到 {settings.max_page_size} 之间"
        )

    cur = conn.cursor()
    # 整个读取在一个事务里，保证页内是同一已提交快照
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,))
        srow = cur.fetchone()
        if srow is None:
            raise ServiceError(404, f"片 {slice_id} 不存在")
        version = int(srow[0])
        if expected_version is not None and expected_version != version:
            raise ServiceError(
                CONFLICT,
                f"片版本已变化: 当前 {version}，请求期望 {expected_version}",
            )

        cur.execute(
            "SELECT COUNT(*) FROM current_map WHERE slice_id = ?", (slice_id,)
        )
        total = int(cur.fetchone()[0])
        offset = (page - 1) * page_size
        cur.execute(
            "SELECT x, y, bin, source, batch_no FROM current_map "
            "WHERE slice_id = ? ORDER BY x, y LIMIT ? OFFSET ?",
            (slice_id, page_size, offset),
        )
        items = [
            {
                "x": int(r[0]),
                "y": int(r[1]),
                "bin": r[2],
                "source": r[3],
                "batch_no": r[4],
            }
            for r in cur.fetchall()
        ]
    finally:
        # 只读事务直接回滚结束（不产生任何写入）
        conn.rollback()

    return {
        "slice_id": slice_id,
        "version": version,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": items,
    }


def get_slice_info(conn: sqlite3.Connection, slice_id: str) -> dict:
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,)
        )
        srow = cur.fetchone()
        if srow is None:
            raise ServiceError(404, f"片 {slice_id} 不存在")
        version = int(srow[0])
        cur.execute(
            "SELECT COUNT(*) FROM first_test WHERE slice_id = ?", (slice_id,)
        )
        first_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT b.batch_no, b.rotation, b.revision, "
            "(SELECT COUNT(*) FROM batch_row br WHERE br.slice_id = b.slice_id "
            " AND br.batch_no = b.batch_no) AS die_count "
            "FROM batch b WHERE b.slice_id = ? ORDER BY b.batch_no",
            (slice_id,),
        )
        batches = [
            {
                "batch_no": int(r[0]),
                "rotation": int(r[1]),
                "revision": int(r[2]),
                "die_count": int(r[3]),
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.rollback()
    return {
        "slice_id": slice_id,
        "version": version,
        "first_die_count": first_count,
        "batches": batches,
    }
