"""下发任务：编排规则、封存一致性与续取切片测试。"""
from __future__ import annotations

import threading

import app.dispatch as dispatch
from app.dispatch import plan_instructions

from .conftest import make_csv


def _upload_first(client, slice_id, rows):
    return client.post(
        f"/slices/{slice_id}/first-test",
        files={"file": ("first.csv", make_csv(rows), "text/csv")},
    )


def _upload_batch(client, slice_id, batch_no, rotation, rows):
    return client.post(
        f"/slices/{slice_id}/batches/{batch_no}",
        data={"rotation": rotation},
        files={"file": ("retest.csv", make_csv(rows), "text/csv")},
    )


def _create(client, slice_id, version=None):
    params = {} if version is None else {"version": version}
    return client.post(f"/slices/{slice_id}/tasks", params=params)


def _fetch(client, task_id, completed):
    return client.get(
        f"/tasks/{task_id}/instructions", params={"completed": completed}
    )


def _expand(instructions):
    """把返回的指令片段展开为按顺序的晶粒坐标。"""
    dies = []
    for inst in instructions:
        for i in range(inst["die_count"]):
            dies.append((inst["x"] + inst["step"] * i, inst["y"]))
    return dies


def test_plan_orders_rows_and_merges_adjacent_same_bin():
    # 偶数行 y=0 从左到右；奇数行 y=1 从右到左
    rows = [
        (0, 0, "A"),
        (1, 0, "A"),
        (2, 0, "B"),
        (3, 0, "B"),
        (3, 1, "A"),
        (2, 1, "A"),
        (1, 1, "C"),
        (0, 1, "C"),
    ]
    insts = plan_instructions(rows)
    assert [(i.x, i.y, i.step, i.bin, i.die_count) for i in insts] == [
        (0, 0, 1, "A", 2),
        (2, 0, 1, "B", 2),
        (3, 1, -1, "A", 2),
        (1, 1, -1, "C", 2),
    ]


def test_plan_breaks_on_gap_and_bin_change():
    rows = [(0, 0, "A"), (1, 0, "A"), (3, 0, "A"), (4, 0, "B"), (5, 0, "B")]
    insts = plan_instructions(rows)
    assert [(i.x, i.die_count, i.bin) for i in insts] == [
        (0, 2, "A"),
        (3, 1, "A"),
        (4, 2, "B"),
    ]


def test_plan_single_die_rows_and_negative_coords():
    rows = [(-1, -1, "A"), (-1, 0, "B"), (-1, 1, "A")]
    insts = plan_instructions(rows)
    # 负奇数按奇数处理（x 递减方向），负偶数按偶数处理（x 递增方向）
    assert [i.step for i in insts] == [-1, 1, -1]
    assert [i.x for i in insts] == [-1, -1, -1]


def test_create_task_returns_task_version_and_total(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A"), (0, 1, "B")])
    resp = _create(client, "W001", version=1)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slice_id"] == "W001"
    assert body["version"] == 1
    assert body["total_dies"] == 3
    assert body["instruction_count"] == 2
    assert body["task_id"] > 0


def test_create_task_stale_seen_version_conflicts(client):
    _upload_first(client, "W001", [(0, 0, "A")])
    _upload_batch(client, "W001", 1, 0, [(0, 0, "R")])
    stale = _create(client, "W001", version=1)
    assert stale.status_code == 409
    fresh = _create(client, "W001", version=2)
    assert fresh.status_code == 201


def test_create_task_unknown_slice(client):
    assert _create(client, "NOPE").status_code == 404


def test_create_task_accepts_body_and_alias_and_slice_path_fetch(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    # JSON 请求体携带所见图版
    body = client.post("/slices/W001/tasks", json={"version": 1})
    assert body.status_code == 201, body.text
    task_id = body.json()["task_id"]
    # 别名参数
    alias = client.post("/slices/W001/tasks", params={"seen_version": 1})
    assert alias.status_code == 201
    # 按片路径续取，与全局路径等价
    one = client.get(f"/tasks/{task_id}/instructions", params={"completed": 1})
    two = client.get(
        f"/slices/W001/tasks/{task_id}/instructions", params={"completed": 1}
    )
    assert one.status_code == two.status_code == 200
    assert one.json() == two.json()
    # 片号与任务不匹配时按 404 处理
    assert (
        client.get(
            f"/slices/OTHER/tasks/{task_id}/instructions", params={"completed": 0}
        ).status_code
        == 404
    )


def test_seal_conflict_when_map_changes_during_planning(client, monkeypatch):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    real_collect = dispatch._collect_snapshot

    def racing_collect(cur, slice_id, seen_version=None):
        version, insts = real_collect(cur, slice_id, seen_version)
        # 编排读取快照后、封存前，另一请求提交了复测
        _upload_batch(client, "W001", 1, 0, [(0, 0, "R")])
        return version, insts

    monkeypatch.setattr(dispatch, "_collect_snapshot", racing_collect)
    resp = _create(client, "W001")
    assert resp.status_code == 409
    assert "图版已变化" in resp.json()["detail"]
    # 冲突不封存任务：任务号 1 不存在
    assert _fetch(client, 1, 0).status_code == 404


def test_write_failure_leaves_no_task(client, monkeypatch):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])

    # 让指令表写入必然失败：任务头即便已插入，也必须随事务一起回滚
    import app.database as database

    setup = database._connect()
    try:
        setup.execute(
            "CREATE TRIGGER fail_dispatch_instruction "
            "BEFORE INSERT ON dispatch_instruction "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
        setup.commit()
    finally:
        setup.close()

    resp = _create(client, "W001")
    assert resp.status_code == 500

    cleanup = database._connect()
    try:
        cleanup.execute("DROP TRIGGER fail_dispatch_instruction")
        cleanup.commit()
        # 没有留下可取指令的任务
        assert cleanup.execute("SELECT COUNT(*) FROM dispatch_task").fetchone()[0] == 0
        assert (
            cleanup.execute("SELECT COUNT(*) FROM dispatch_instruction").fetchone()[0]
            == 0
        )
    finally:
        cleanup.close()

    assert _fetch(client, 1, 0).status_code == 404
    # 失败恢复后仍可正常生成任务
    retry = _create(client, "W001")
    assert retry.status_code == 201


def test_fetch_walks_whole_map_exactly_once(client):
    rows = [(i, j, "A" if (i + j) % 2 else "B") for j in range(3) for i in range(4)]
    _upload_first(client, "W001", rows)
    task = _create(client, "W001").json()

    seen = []
    completed = 0
    guard = 0
    while True:
        guard += 1
        assert guard < 100
        body = _fetch(client, task["task_id"], completed).json()
        assert body["completed_before"] == completed
        assert body["completed"] == body["next_completed"] >= completed
        seen.extend(_expand(body["instructions"]))
        completed = body["next_completed"]
        if body["done"]:
            break

    assert completed == task["total_dies"] == len(rows)
    # 整片已完成后再次取数返回空指令
    finished = _fetch(client, task["task_id"], completed).json()
    assert finished["done"] is True and finished["instructions"] == []
    assert len(seen) == len(rows)
    assert sorted(seen) == sorted((x, y) for x, y, _ in rows)
    # 走位顺序：y 递增，偶数行 x 递增、奇数行 x 递减
    for y in sorted({p[1] for p in seen}):
        xs = [p[0] for p in seen if p[1] == y]
        assert xs == (sorted(xs) if y % 2 == 0 else sorted(xs, reverse=True))


def test_fetch_splits_first_and_last_mid_instruction(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dispatch_max_instructions", 2)
    monkeypatch.setattr(settings, "dispatch_max_dies", 5)

    # 一条 10 粒的长指令
    _upload_first(client, "W001", [(i, 0, "A") for i in range(10)])
    task = _create(client, "W001").json()
    assert task["instruction_count"] == 1

    first = _fetch(client, task["task_id"], 0).json()
    assert len(first["instructions"]) == 1
    assert first["instructions"][0]["die_count"] == 5
    assert first["instructions"][0]["x"] == 0
    assert first["next_completed"] == 5
    assert first["done"] is False

    second = _fetch(client, task["task_id"], 5).json()
    assert second["instructions"][0]["die_count"] == 5
    assert second["instructions"][0]["x"] == 5
    assert second["next_completed"] == 10
    assert second["done"] is True

    third = _fetch(client, task["task_id"], 10).json()
    assert third["done"] is True and third["instructions"] == []


def test_fetch_respects_both_limits(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dispatch_max_instructions", 3)
    monkeypatch.setattr(settings, "dispatch_max_dies", 1000)
    # 10 条独立单粒指令（不同档位断开）
    _upload_first(client, "W001", [(i, 0, f"B{i}") for i in range(10)])
    task = _create(client, "W001").json()
    assert task["instruction_count"] == 10

    body = _fetch(client, task["task_id"], 0).json()
    assert len(body["instructions"]) == 3
    assert body["next_completed"] == 3

    monkeypatch.setattr(settings, "dispatch_max_instructions", 128)
    monkeypatch.setattr(settings, "dispatch_max_dies", 4)
    body = _fetch(client, task["task_id"], 0).json()
    assert len(body["instructions"]) == 4
    assert body["next_completed"] == 4


def test_fetch_out_of_bounds_and_unknown(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    task = _create(client, "W001").json()
    assert _fetch(client, task["task_id"], -1).status_code in (400, 422)
    over = _fetch(client, task["task_id"], task["total_dies"] + 1)
    assert over.status_code == 400
    assert "越界" in over.json()["detail"]
    assert _fetch(client, 99999, 0).status_code == 404


def test_fetch_does_not_rescan_current_map(client):
    _upload_first(client, "W001", [(i, 0, "A") for i in range(6)])
    task = _create(client, "W001").json()

    # 直接删掉物化当前图，续取仍应只凭已封存任务工作
    import app.database as database

    conn = database._connect()
    try:
        conn.execute("DROP TABLE current_map")
        conn.commit()
    finally:
        conn.close()

    body = _fetch(client, task["task_id"], 0).json()
    assert body["total_dies"] == 6
    assert body["next_completed"] == 6 and body["done"] is True


def test_full_walk_exactly_once_under_default_limits(client):
    # 5000 粒、带缺格与换档，超过单次 4096 粒上限，需要至少两段
    rows = []
    for y in range(50):
        for x in range(100):
            if y % 7 == 3 and x % 5 == 0:
                continue  # 缺格
            rows.append((x, y, f"B{x % 3}"))
    _upload_first(client, "W001", rows)
    task = _create(client, "W001").json()
    assert task["total_dies"] == len(rows)

    seen = []
    completed = 0
    while True:
        body = _fetch(client, task["task_id"], completed).json()
        assert len(body["instructions"]) <= 128
        assert sum(i["die_count"] for i in body["instructions"]) <= 4096
        seen.extend(_expand(body["instructions"]))
        completed = body["completed"]
        if body["done"]:
            break

    assert completed == len(rows)
    assert len(seen) == len(rows)
    assert len(set(seen)) == len(rows)
    expected = {(x, y) for x, y, _ in rows}
    assert set(seen) == expected


def test_task_survives_map_changes_and_restart(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A"), (0, 1, "A")])
    task = _create(client, "W001", version=1).json()
    first = _fetch(client, task["task_id"], 0).json()
    assert first["completed_before"] == 0
    assert first["completed"] == 3 and first["next_completed"] == 3

    # 任务封存后改图，任务仍按原图汇报图版与总粒数，并继续按原任务续取
    _upload_batch(client, "W001", 1, 0, [(0, 0, "R")])
    second = _fetch(client, task["task_id"], 2).json()
    assert second["version"] == 1
    assert second["total_dies"] == 3
    assert second["next_completed"] == 3

    # 模拟重启：重新加载应用后同一任务号仍可续取
    import importlib

    import app.config as config
    import app.database as database
    import app.dispatch as dispatch_mod
    import app.main as main

    for mod in (config, database, dispatch_mod, main):
        importlib.reload(mod)
    database.init_db()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as new_client:
        again = _fetch(new_client, task["task_id"], 1).json()
        assert again["task_id"] == task["task_id"]
        assert again["next_completed"] == 3
        assert again["done"] is True


def test_create_does_not_block_concurrent_uploads(client):
    """编排读取快照期间，复测上传仍可提交（不占用写锁）。"""
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    real_collect = dispatch._collect_snapshot
    uploaded = threading.Event()

    def slow_collect(cur, slice_id, seen_version=None):
        result = real_collect(cur, slice_id, seen_version)
        # 快照读取完成后，让另一个线程提交复测；写锁可用，说明没有被编排占用
        t = threading.Thread(target=lambda: (uploaded.set(), _upload_batch(
            client, "W001", 1, 0, [(0, 0, "R")]
        )))
        t.start()
        t.join(timeout=5)
        return result

    dispatch._collect_snapshot = slow_collect
    try:
        resp = _create(client, "W001")
    finally:
        dispatch._collect_snapshot = real_collect

    assert uploaded.is_set()
    # 图版在封存前已变化 -> 冲突，且未落任务
    assert resp.status_code == 409
