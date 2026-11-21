"""判级图 API 端到端测试。"""
from __future__ import annotations

import threading

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


def _reorient(client, slice_id, batch_no, rotation, expected_revision):
    return client.post(
        f"/slices/{slice_id}/batches/{batch_no}/reorient",
        data={"rotation": rotation, "expected_revision": expected_revision},
    )


def _map_dict(client, slice_id, page=1, page_size=1000, version=None):
    params = {"page": page, "page_size": page_size}
    if version is not None:
        params["version"] = version
    resp = client.get(f"/slices/{slice_id}/map", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {(d["x"], d["y"]): d for d in body["items"]}, body


def test_health_and_docs(client):
    assert client.get("/health").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/slices/{slice_id}/first-test" in openapi["paths"]


def test_first_test_baseline(client):
    rows = [(0, 0, "A"), (1, 0, "A"), (0, 1, "B"), (-1, -1, "C")]
    resp = _upload_first(client, "W001", rows)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["revision"] == 1
    assert body["die_count"] == 4

    m, page = _map_dict(client, "W001")
    assert page["total"] == 4 and page["version"] == 1
    assert m[(0, 0)]["bin"] == "A" and m[(0, 0)]["source"] == "first"
    assert m[(0, 0)]["batch_no"] is None


def test_second_first_test_rejected_and_keeps_original(client):
    _upload_first(client, "W001", [(0, 0, "A")])
    resp = _upload_first(client, "W001", [(0, 0, "Z")])
    assert resp.status_code == 409
    m, _ = _map_dict(client, "W001")
    assert m[(0, 0)]["bin"] == "A"  # 原图保留


def test_csv_duplicate_coords_rejected(client):
    resp = _upload_first(client, "W001", [(0, 0, "A"), (0, 0, "B")])
    assert resp.status_code == 400
    assert "坐标重复" in resp.json()["detail"]


def test_bad_csv_header_and_non_integer(client):
    r = client.post(
        "/slices/W/first-test",
        files={"file": ("x.csv", b"a,b,c\n1,2,X\n", "text/csv")},
    )
    assert r.status_code == 400 and "表头" in r.json()["detail"]

    r = client.post(
        "/slices/W/first-test",
        files={"file": ("x.csv", b"x,y,bin\n1.5,2,X\n", "text/csv")},
    )
    assert r.status_code == 400 and "整数" in r.json()["detail"]


def test_batch_requires_first_test(client):
    resp = _upload_batch(client, "NOPE", 1, 0, [(0, 0, "R1")])
    assert resp.status_code == 422
    assert "首测" in resp.json()["detail"]


def test_batch_rotation_and_highest_batch_wins(client):
    # 首测 4 个点
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A"), (0, 1, "B"), (1, 1, "B")])

    # 批号 1：rotation=90，原始点 (1,0) 逆时针 90° -> (0,1)
    r1 = _upload_batch(client, "W001", 1, 90, [(1, 0, "R1")])
    assert r1.status_code == 201, r1.text
    assert r1.json()["revision"] == 2
    m, _ = _map_dict(client, "W001")
    assert m[(0, 1)]["bin"] == "R1" and m[(0, 1)]["source"] == "retest"
    assert m[(0, 1)]["batch_no"] == 1
    assert m[(0, 0)]["source"] == "first"

    # 批号 2（乱序先传 3 再传 2 也允许，这里先传 2）：rotation=0，覆盖 (0,1)，更高批号胜出
    r2 = _upload_batch(client, "W001", 2, 0, [(0, 1, "R2")])
    assert r2.json()["revision"] == 3
    m, _ = _map_dict(client, "W001")
    assert m[(0, 1)]["bin"] == "R2" and m[(0, 1)]["batch_no"] == 2

    # 乱序上传批号 3：rotation=180，原始 (0,-1) -> (0,1)，再次以最高批号覆盖
    r3 = _upload_batch(client, "W001", 3, 180, [(0, -1, "R3")])
    assert r3.status_code == 201
    m, _ = _map_dict(client, "W001")
    assert m[(0, 1)]["bin"] == "R3" and m[(0, 1)]["batch_no"] == 3
    # 未被任何复测覆盖的点仍是首测
    assert m[(1, 1)]["source"] == "first" and m[(1, 1)]["bin"] == "B"


def test_duplicate_batch_no_rejected(client):
    _upload_first(client, "W001", [(0, 0, "A"), (0, 1, "B")])
    _upload_batch(client, "W001", 1, 90, [(1, 0, "R1")])
    dup = _upload_batch(client, "W001", 1, 0, [(0, 0, "X")])
    assert dup.status_code == 409
    m, _ = _map_dict(client, "W001")
    assert m[(0, 0)]["source"] == "first"  # 失败不影响原图


def test_invalid_landing_rejected_and_keeps_state(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    # rotation=90，原始点 (0,0)->(0,0) 有效；(1,0)->(0,1) 不在首测图
    resp = _upload_batch(client, "W001", 1, 90, [(0, 0, "R1"), (1, 0, "R1")])
    assert resp.status_code == 422
    assert "不在首测图" in resp.json()["detail"]
    # 整批回滚：批次不存在，图不变
    info = client.get("/slices/W001").json()
    assert info["batches"] == []
    m, _ = _map_dict(client, "W001")
    assert all(d["source"] == "first" for d in m.values())


def test_reorient_success_recomputes_old_and_new_coverage(client):
    _upload_first(
        client,
        "W001",
        [(-1, 0, "A"), (0, 0, "A"), (1, 0, "A"), (0, 1, "B"), (0, -1, "C")],
    )
    # 批号 1，rotation=0：覆盖 (1,0)
    up = _upload_batch(client, "W001", 1, 0, [(1, 0, "R1")])
    rev = up.json()["revision"]
    assert rev == 2
    m, _ = _map_dict(client, "W001")
    assert m[(1, 0)]["bin"] == "R1"

    # 改向到 90°：(1,0) -> (0,1)。旧点 (1,0) 应回退首测，新点 (0,1) 变复测
    r = _reorient(client, "W001", 1, 90, rev)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    # 旧覆盖 (1,0) + 新覆盖 (0,1) = 2 个受影响晶粒
    assert body["affected_dies"] == 2

    m, page = _map_dict(client, "W001")
    assert page["version"] == 3
    assert m[(1, 0)]["source"] == "first" and m[(1, 0)]["bin"] == "A"
    assert m[(0, 1)]["source"] == "retest" and m[(0, 1)]["bin"] == "R1"
    assert m[(0, 1)]["batch_no"] == 1
    # 无关晶粒不受影响
    assert m[(-1, 0)]["source"] == "first"

    info = client.get("/slices/W001").json()
    assert info["batches"][0]["rotation"] == 90
    assert info["batches"][0]["revision"] == 3


def test_reorient_stale_revision_rejected(client):
    _upload_first(client, "W001", [(-1, 0, "A"), (0, 0, "A"), (1, 0, "A"), (0, 1, "B")])
    _upload_batch(client, "W001", 1, 0, [(1, 0, "R1")])
    # 先成功改向一次，修订号推进到 3
    ok = _reorient(client, "W001", 1, 90, 2)
    assert ok.status_code == 200
    # 用旧修订号再改：过期拒绝，图保持上一已提交状态
    stale = _reorient(client, "W001", 1, 180, 2)
    assert stale.status_code == 409
    assert "过期" in stale.json()["detail"]
    m, _ = _map_dict(client, "W001")
    assert m[(0, 1)]["bin"] == "R1"  # 仍是 90° 的结果
    assert m[(1, 0)]["source"] == "first"


def test_reorient_invalid_landing_keeps_original(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    up = _upload_batch(client, "W001", 1, 0, [(1, 0, "R1")])
    rev = up.json()["revision"]
    # 90° 后 (1,0)->(0,1) 不在首测图
    r = _reorient(client, "W001", 1, 90, rev)
    assert r.status_code == 422
    m, _ = _map_dict(client, "W001")
    assert m[(1, 0)]["bin"] == "R1"  # 原图保留
    info = client.get("/slices/W001").json()
    assert info["batches"][0]["rotation"] == 0  # 角度未变、修订号未变
    assert info["batches"][0]["revision"] == rev


def test_reorient_missing_batch(client):
    _upload_first(client, "W001", [(0, 0, "A")])
    r = _reorient(client, "W001", 99, 90, 1)
    assert r.status_code == 422


def test_invalid_rotation_form_value(client):
    _upload_first(client, "W001", [(0, 0, "A")])
    r = client.post(
        "/slices/W001/batches/1",
        data={"rotation": 45},
        files={"file": ("b.csv", make_csv([(0, 0, "X")]), "text/csv")},
    )
    assert r.status_code == 400


def test_pagination_and_snapshot_version(client):
    rows = [(i, 0, f"B{i}") for i in range(5)]
    _upload_first(client, "W001", rows)

    p1 = client.get("/slices/W001/map", params={"page": 1, "page_size": 2}).json()
    assert p1["total"] == 5 and len(p1["items"]) == 2
    assert [d["x"] for d in p1["items"]] == [0, 1]
    p3 = client.get("/slices/W001/map", params={"page": 3, "page_size": 2}).json()
    assert [d["x"] for d in p3["items"]] == [4]

    # 版本不匹配 -> 409，调用方可拿新版本重新从第一页翻
    _upload_batch(client, "W001", 1, 0, [(0, 0, "R")])
    conflict = client.get(
        "/slices/W001/map", params={"page": 1, "page_size": 2, "version": 1}
    )
    assert conflict.status_code == 409
    fresh = client.get(
        "/slices/W001/map", params={"page": 1, "page_size": 2, "version": 2}
    )
    assert fresh.status_code == 200


def test_map_unknown_slice_and_bad_paging(client):
    assert client.get("/slices/UNKNOWN/map").status_code == 404
    _upload_first(client, "W001", [(0, 0, "A")])
    bad = client.get("/slices/W001/map", params={"page": 0, "page_size": 10})
    assert bad.status_code in (400, 422)
    big = client.get("/slices/W001/map", params={"page_size": 99999})
    assert big.status_code == 400


def test_interleaved_writes_apply_in_submission_order(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "A")])
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(batch_no: int, bin_value: str):
        barrier.wait()
        r = _upload_batch(client, "W001", batch_no, 0, [(0, 0, bin_value)])
        results.append(r.status_code)

    t1 = threading.Thread(target=worker, args=(1, "R1"))
    t2 = threading.Thread(target=worker, args=(2, "R2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results == [201, 201]
    # 无论谁先拿到锁，最高批号 2 的档位生效
    m, page = _map_dict(client, "W001")
    assert m[(0, 0)]["bin"] == "R2"
    assert page["version"] == 3  # 首测 1 + 两批各 +1


def test_reorient_with_other_batch_keeps_highest_batch_rule(client):
    # 两批覆盖同一晶粒，改向后低批号批移走，高批号批仍覆盖 -> 保持高批号档位
    _upload_first(
        client,
        "W001",
        [(-1, 0, "A"), (0, 0, "A"), (1, 0, "A"), (0, 1, "B"), (0, -1, "C")],
    )
    # 批 1：原始 (1,0)、rotation=0 -> 落点 (1,0)
    up1 = _upload_batch(client, "W001", 1, 0, [(1, 0, "R1")])
    # 批 2：原始 (1,0)、rotation=0 -> 同样落在 (1,0)，更高批号覆盖
    up2 = _upload_batch(client, "W001", 2, 0, [(1, 0, "R2")])
    m, _ = _map_dict(client, "W001")
    assert m[(1, 0)]["bin"] == "R2" and m[(1, 0)]["batch_no"] == 2

    # 批 1 改向 90°：(1,0) -> (0,1)，离开 (1,0)
    r = _reorient(client, "W001", 1, 90, up1.json()["revision"])
    assert r.status_code == 200
    m, _ = _map_dict(client, "W001")
    # (1,0) 仍被批 2 覆盖，最高批号规则不变
    assert m[(1, 0)]["bin"] == "R2" and m[(1, 0)]["batch_no"] == 2
    # 新落点来自批 1
    assert m[(0, 1)]["bin"] == "R1" and m[(0, 1)]["batch_no"] == 1

    # 批 2 再改向 270°：(1,0) -> (0,-1)，(1,0) 失去所有复测覆盖 -> 回退首测
    r2 = _reorient(client, "W001", 2, 270, up2.json()["revision"])
    assert r2.status_code == 200
    m, _ = _map_dict(client, "W001")
    assert m[(1, 0)]["source"] == "first" and m[(1, 0)]["bin"] == "A"
    assert m[(0, -1)]["bin"] == "R2" and m[(0, -1)]["batch_no"] == 2


def test_first_die_limit_boundary(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_first_dies", 100)
    ok = _upload_first(client, "SMALL", [(i, 0, "A") for i in range(100)])
    assert ok.status_code == 201
    over = _upload_first(client, "BIG", [(i, 0, "A") for i in range(101)])
    assert over.status_code == 422 and "上限" in over.json()["detail"]


def test_large_slice_roundtrip(client):
    # 2 万粒规模的完整链路，保证物化方案可扩展（上限为 20 万）
    n = 20_000
    rows = [(i, j, "A") for i in range(1000) for j in range(20)]
    assert len(rows) == n
    r = _upload_first(client, "BIG", rows)
    assert r.status_code == 201 and r.json()["die_count"] == n

    # 复测批：rotation=0 覆盖其中 1000 粒（i 取 0..999，均在首测图上）
    retest = [(i, 0, "R") for i in range(1000)]
    rb = _upload_batch(client, "BIG", 7, 0, retest)
    assert rb.status_code == 201 and rb.json()["die_count"] == 1000

    pages = client.get("/slices/BIG/map", params={"page_size": 1}).json()
    assert pages["total"] == n
    seen = 0
    retest_hits = 0
    for page_no in range(1, n // 5000 + 2):
        body = client.get(
            "/slices/BIG/map", params={"page": page_no, "page_size": 5000}
        ).json()
        seen += len(body["items"])
        retest_hits += sum(1 for d in body["items"] if d["source"] == "retest")
    assert seen == n
    assert retest_hits == 1000


def test_persistence_across_restart(client, tmp_path, monkeypatch):
    _upload_first(client, "PERSIST", [(0, 0, "A"), (1, 0, "A")])
    ok = _upload_batch(client, "PERSIST", 1, 0, [(1, 0, "R1")])
    assert ok.status_code == 201
    m, _ = _map_dict(client, "PERSIST")
    assert m[(1, 0)]["bin"] == "R1"

    # 模拟重启：重新初始化应用（同一数据库文件）
    import importlib

    import app.config as config
    import app.database as database
    import app.main as main
    import app.services as services

    for mod in (config, database, services, main):
        importlib.reload(mod)
    database.init_db()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as new_client:
        m, page = _map_dict(new_client, "PERSIST")
        assert page["version"] == 2
        assert m[(1, 0)]["bin"] == "R1" and m[(1, 0)]["source"] == "retest"
        info = new_client.get("/slices/PERSIST").json()
        assert info["batches"][0]["revision"] == 2
        # 重启后仍可继续改向
        r = new_client.post(
            "/slices/PERSIST/batches/1/reorient",
            data={"rotation": 90, "expected_revision": 2},
        )
        # (1,0)->(0,1) 仍不在首测图 -> 拒绝且保留
        assert r.status_code == 422
