"""FastAPI 入口：判级图 API。

交互式文档：启动后访问 /docs ，按片号分开上传首测与复测 CSV。
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, Query, UploadFile, status

from . import services
from .config import settings
from .csv_utils import CsvError, parse_die_csv
from .database import get_conn, init_db
from .schemas import (
    BatchUploadResponse,
    FirstUploadResponse,
    MapPage,
    ReorientResponse,
    SliceInfo,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建表；数据落在持久化文件，重启后直接读取
    init_db()
    yield


app = FastAPI(
    title="切割分拣判级图 API",
    description=(
        "按晶粒坐标取档位。首测每片一份；复测按批号入库，提交逆时针旋转角，"
        "落点必须全在首测图上。改向带修订号做乐观并发控制，只重算受影响晶粒。"
        "每粒取覆盖它的最高批号档位，无复测覆盖时取首测。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/slices/{slice_id}/first-test",
    response_model=FirstUploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["首测"],
    summary="上传首测 CSV（每片一份，最多 20 万粒）",
)
def upload_first_test(
    slice_id: str,
    file: UploadFile = File(..., description="CSV 文件，表头 x,y,bin，整数坐标，同份坐标唯一"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _check_slice_id(slice_id)
    try:
        rows = parse_die_csv(file.file.read())
    except CsvError as exc:
        raise services.ServiceError(services.BAD_REQUEST, str(exc))
    return services.submit_first_test(conn, slice_id, rows)


@app.post(
    "/slices/{slice_id}/batches/{batch_no}",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["复测"],
    summary="上传复测 CSV（同片批号唯一，可乱序上传）",
)
def upload_batch(
    slice_id: str,
    batch_no: int,
    rotation: int = Form(..., description="逆时针旋转角，仅允许 0、90、180、270"),
    file: UploadFile = File(..., description="CSV 文件，表头 x,y,bin，为复测台原始整数坐标"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _check_slice_id(slice_id)
    if batch_no <= 0:
        raise services.ServiceError(services.BAD_REQUEST, "批号必须为正整数")
    try:
        rows = parse_die_csv(file.file.read())
    except CsvError as exc:
        raise services.ServiceError(services.BAD_REQUEST, str(exc))
    return services.submit_batch(conn, slice_id, batch_no, rotation, rows)


@app.post(
    "/slices/{slice_id}/batches/{batch_no}/reorient",
    response_model=ReorientResponse,
    tags=["复测"],
    summary="改向（带该批所见修订号，沿用原表重算）",
)
def reorient_batch(
    slice_id: str,
    batch_no: int,
    rotation: int = Form(..., description="新的逆时针旋转角，仅允许 0、90、180、270"),
    expected_revision: int = Form(
        ..., alias="expected_revision", description="该批当前修订号（上传/上次改向返回值），过期则拒绝"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _check_slice_id(slice_id)
    if batch_no <= 0:
        raise services.ServiceError(services.BAD_REQUEST, "批号必须为正整数")
    return services.reorient_batch(conn, slice_id, batch_no, rotation, expected_revision)


@app.get(
    "/slices/{slice_id}/map",
    response_model=MapPage,
    tags=["判级图"],
    summary="按片号分页取当前图（只读已提交状态，不重扫各批原表）",
)
def get_map(
    slice_id: str,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(0, description="页大小，留空取服务端默认值"),
    version: int | None = Query(
        None, description="可选，期望读取的片版本号；与当前版本不一致时返回 409，便于翻页一致性校验"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _check_slice_id(slice_id)
    if page_size == 0:
        page_size = settings.default_page_size
    return services.get_map_page(conn, slice_id, page, page_size, version)


@app.get(
    "/slices/{slice_id}",
    response_model=SliceInfo,
    tags=["判级图"],
    summary="查看片信息与各复测批（含当前修订号、旋转角）",
)
def get_slice(
    slice_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    _check_slice_id(slice_id)
    return services.get_slice_info(conn, slice_id)


def _check_slice_id(slice_id: str) -> None:
    if not slice_id or len(slice_id) > settings.max_slice_id_len:
        raise services.ServiceError(
            services.BAD_REQUEST,
            f"片号长度需在 1 到 {settings.max_slice_id_len} 之间",
        )


# 统一业务错误响应，保留语义化 HTTP 状态码
@app.exception_handler(services.ServiceError)
def _service_error_handler(request, exc: services.ServiceError):  # noqa: ANN001
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})
