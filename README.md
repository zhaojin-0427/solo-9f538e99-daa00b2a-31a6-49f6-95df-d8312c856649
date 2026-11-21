# 切割分拣判级图 API

按晶粒坐标取档位的判级图服务。复测台朝向录错时，可凭修订号改向，系统只重算受影响晶粒，
不会把档位写到别的晶粒上。

- 技术栈：FastAPI + SQLite（WAL 模式，单文件持久化）
- 交互式文档：启动后访问 <http://localhost:8000/docs>

## 一、CSV 格式

UTF-8 编码，固定表头，坐标是以**圆心为原点的整数坐标**，同份文件内坐标必须唯一：

```csv
x,y,bin
0,0,A
1,0,A
0,1,B
```

## 二、核心规则

1. **首测**：每片仅一份，最多 20 万粒（`GRADING_MAX_FIRST_DIES` 可配）。
2. **复测批次**：同片批号唯一，按检测先后编号（批号越大越新），可乱序上传。
3. **旋转**：提交逆时针旋转角，仅允许 `0 / 90 / 180 / 270` 度。
   - 90°：`(x,y) -> (-y,x)`；180°：`(x,y) -> (-x,-y)`；270°：`(x,y) -> (y,-x)`
   - 旋转后的全部落点必须在首测图上，否则整批拒绝，原图与已入库批次不受影响。
4. **改向**：必须携带该批当前修订号（乐观并发控制）；过期返回 `409`。
   沿用上传时的原表重算，无需重传 CSV；只重算这批**原先覆盖 ∪ 现在覆盖**的晶粒。
5. **判级取值**：每粒取覆盖它的**最高批号**复测档位；无复测覆盖时取首测档位。
6. **修订号**：每次成功提交（首测入库 / 复测入库 / 改向）都让该片版本 +1，
   该版本号即返回的修订号，同时写入对应批次。
7. **一致性**：所有写操作在单事务内串行生效；读请求在单事务快照内完成，
   每页只读到同一个已提交状态，分页接口只读物化结果表 `current_map`，不重扫各批原表。
8. **持久化**：数据写入挂载卷上的 SQLite 文件，重启后直接读取已生效的图。

## 三、快速启动（Docker Compose）

```bash
docker compose up -d --build
# 查看状态 / 日志
docker compose ps
docker compose logs -f
# 停止（数据保留在命名卷 grading-data 中）
docker compose down
# 停止并删除数据
docker compose down -v
```

服务监听 `8000` 端口，数据库位于容器内 `/data/grading.db`。

> 说明：SQLite 的写串行化在**单进程单 uvicorn worker** 下由应用锁保证，
> compose 默认即单 worker。请勿把 worker 数调到 >1（多进程需要改用 C/S 数据库）。

## 四、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/slices/{slice_id}/first-test` | 上传首测 CSV（multipart，字段 `file`） |
| POST | `/slices/{slice_id}/batches/{batch_no}` | 上传复测 CSV（表单 `rotation` + 文件 `file`） |
| POST | `/slices/{slice_id}/batches/{batch_no}/reorient` | 改向（表单 `rotation`、`expected_revision`） |
| GET | `/slices/{slice_id}/map` | 分页取当前图（`page`、`page_size`、可选 `version`） |
| GET | `/slices/{slice_id}` | 片信息与各批复测修订号、角度 |
| GET | `/health` | 健康检查 |

### curl 示例

```bash
# 1) 首测
curl -X POST http://localhost:8000/slices/W001/first-test \
  -F "file=@first.csv"
# -> {"slice_id":"W001","revision":1,"die_count":5}

# 2) 复测批号 1，逆时针旋转 90°
curl -X POST http://localhost:8000/slices/W001/batches/1 \
  -F "rotation=90" -F "file=@retest.csv"
# -> {"slice_id":"W001","batch_no":1,"rotation":90,"revision":2,"die_count":3}

# 3) 改向（带上一步返回的修订号 2）
curl -X POST http://localhost:8000/slices/W001/batches/1/reorient \
  -F "rotation=180" -F "expected_revision=2"
# -> {"slice_id":"W001","batch_no":1,"rotation":180,"revision":3,"affected_dies":6}

# 4) 分页读当前图
curl "http://localhost:8000/slices/W001/map?page=1&page_size=500"
# 翻页时可带 version 校验一致性，版本变化返回 409：
curl "http://localhost:8000/slices/W001/map?page=2&page_size=500&version=3"
```

`map` 每项：

```json
{"x": 0, "y": 1, "bin": "R1", "source": "retest", "batch_no": 2}
```

`source` 为 `first`（首测）或 `retest`（复测）；复测时 `batch_no` 为实际生效批号。

### 错误码约定

| 状态码 | 场景 |
| --- | --- |
| 400 | CSV 格式错误、旋转角非法、分页参数非法 |
| 404 | 片不存在 |
| 409 | 重复首测 / 重复批号 / 修订号过期 / 期望版本不匹配 |
| 422 | 片尚无首测、旋转或改向后落点不在首测图上、超过 20 万粒上限 |
| 500 | 写入失败（事务回滚，原图与批次保留） |

## 五、数据模型（SQLite）

| 表 | 作用 |
| --- | --- |
| `slice` | 片主表，`current_ver` 为单调递增的已提交版本号 |
| `first_test` | 首测原表，`(slice_id,x,y)` 唯一 |
| `batch` | 批次主表：批号、当前旋转角、修订号 |
| `batch_row` | 复测原表（上传时原始坐标），改向沿用，不重传 |
| `coverage` | 物化覆盖关系（旋转落点 → 批号/档位） |
| `current_map` | 物化当前判级图，分页查询只读此表 |

## 六、本地开发与测试

```bash
pip install -r requirements.txt
export GRADING_DB_PATH=./data/grading.db
uvicorn app.main:app --reload
pytest -q
```
