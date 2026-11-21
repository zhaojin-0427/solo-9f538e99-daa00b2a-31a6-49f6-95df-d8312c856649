"""API 响应模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CommitResponse(BaseModel):
    slice_id: str
    revision: int = Field(..., description="该提交产生的修订号（同时是该片当前版本号）")


class FirstUploadResponse(CommitResponse):
    die_count: int = Field(..., description="首测晶粒数")


class BatchUploadResponse(CommitResponse):
    batch_no: int
    rotation: int
    die_count: int = Field(..., description="本批复测有效落点晶粒数")


class ReorientResponse(CommitResponse):
    batch_no: int
    rotation: int
    affected_dies: int = Field(..., description="本次重算涉及的去重晶粒数")


class DieOut(BaseModel):
    x: int
    y: int
    bin: str
    source: Literal["first", "retest"] = Field(..., description="first=首测，retest=复测")
    batch_no: int | None = Field(None, description="复测来源时的覆盖批号")


class MapPage(BaseModel):
    slice_id: str
    version: int = Field(..., description="本页读取到的片版本号，翻页时可用来校验一致性")
    page: int
    page_size: int
    total: int
    items: list[DieOut]


class BatchInfo(BaseModel):
    batch_no: int
    rotation: int
    revision: int
    die_count: int


class SliceInfo(BaseModel):
    slice_id: str
    version: int
    first_die_count: int
    batches: list[BatchInfo]


class ErrorBody(BaseModel):
    detail: str
