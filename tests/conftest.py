"""pytest 夹具：每个测试使用独立临时数据库。"""
from __future__ import annotations

import importlib
import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_grading.db"
    monkeypatch.setenv("GRADING_DB_PATH", str(db_path))

    import app.config as config
    import app.database as database
    import app.main as main
    import app.services as services
    import app.csv_utils as csv_utils
    import app.schemas as schemas
    import app.rotation as rotation

    for mod in (config, database, services, csv_utils, schemas, rotation, main):
        importlib.reload(mod)

    database.init_db()
    with TestClient(main.app) as c:
        yield c


def make_csv(rows: list[tuple[int, int, str]]) -> bytes:
    buf = io.StringIO()
    buf.write("x,y,bin\n")
    for x, y, b in rows:
        buf.write(f"{x},{y},{b}\n")
    return buf.getvalue().encode("utf-8")
