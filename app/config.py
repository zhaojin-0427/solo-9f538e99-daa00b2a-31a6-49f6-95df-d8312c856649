"""运行配置，全部可通过环境变量覆盖。"""
from __future__ import annotations

import os


class Settings:
    def __init__(self) -> None:
        # SQLite 数据库文件位置，容器内默认落在持久化卷上
        self.db_path: str = os.getenv("GRADING_DB_PATH", "/data/grading.db")
        # 每片首测晶粒数上限
        self.max_first_dies: int = int(os.getenv("GRADING_MAX_FIRST_DIES", "200000"))
        # 分页默认/最大页大小
        self.default_page_size: int = int(os.getenv("GRADING_DEFAULT_PAGE_SIZE", "500"))
        self.max_page_size: int = int(os.getenv("GRADING_MAX_PAGE_SIZE", "5000"))
        # 片号、档位字符串长度保护
        self.max_slice_id_len: int = int(os.getenv("GRADING_MAX_SLICE_ID_LEN", "128"))
        self.max_bin_len: int = int(os.getenv("GRADING_MAX_BIN_LEN", "64"))


settings = Settings()
