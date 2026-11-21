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


class DispatchTaskRequest(BaseModel):
    version: int | None = Field(
        None, description="编排所见图版号；与封存时当前图版不一致返回 409"
    )


class DispatchTaskCreated(BaseModel):
    task_id: int = Field(..., description="下发任务号，后续取数凭此号")
    slice_id: str
    version: int = Field(..., description="任务封存时所见并锁定的片图版号")
    total_dies: int = Field(..., description="任务覆盖的晶粒总数")
    instruction_count: int = Field(..., description="封存的指令条数（合并后）")


class DispatchTaskInfo(BaseModel):
    task_id: int
    slice_id: str
    version: int
    total_dies: int
    instruction_count: int


class DispatchInstruction(BaseModel):
    x: int = Field(..., description="本段首粒 x（可能在原指令中间截开）")
    y: int = Field(..., description="本段首粒 y（整行同一 y）")
    step: int = Field(..., description="横向步进方向：1 向右、-1 向左")
    bin: str = Field(..., description="档位")
    die_count: int = Field(..., description="本段粒数")


class DispatchSegment(BaseModel):
    task_id: int
    slice_id: str
    version: int = Field(..., description="任务封存时的图版号，后续改图不影响本任务")
    total_dies: int
    completed_before: int = Field(..., description="本次取数前已累计完成的粒数")
    completed: int = Field(..., description="执行完本段后的累计完成粒数，直接作为下次的 completed")
    next_completed: int = Field(..., description="与 completed 同义，便于连续取数")
    done: bool = Field(..., description="是否已取完整片；已取完时 instructions 为空")
    instructions: list[DispatchInstruction]
