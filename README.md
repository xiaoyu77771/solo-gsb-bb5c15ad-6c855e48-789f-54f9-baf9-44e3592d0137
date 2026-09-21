# 时间序列异常检测 API

基于 **Python + FastAPI + SQLite** 的时间序列突变（spike / 水平偏移）检测服务。

接收带时间戳的数值序列，使用**稳健滑动窗口**（中位数 + MAD）逐点评分，识别突变点并把相邻异常合并为异常区间，返回每段的基线、分数以及触发点的自然语言解释；检测结果持久化到 SQLite，支持按 ID / series_id 查询。

## 特性

- **稳健基线**：每个点只使用其之前 `window_size` 个点（trailing window，无前视泄漏），以中位数作为基线中心、`1.4826 × MAD` 作为稳健标准差。
- **突变评分**：稳健 z 分数 `z = (x − median) / robust_sd`，`|z| ≥ threshold` 判为异常；MAD 为 0 时自动回退到样本标准差，基线真正常量且值跳变时按最强突变处理。
- **两种基线策略**：`rolling`（默认，经典滑动窗口，结果稳定兼容）与 `guarded`（仅正常点进入基线，孤立尖刺不污染中位数/离散度；连续异常达到 `rebase_after` 时按异常段尾部重建基线并递增代次，触发点仍计异常、区间不拆分）。
- **区间合并**：相邻触发点间隔不超过 `merge_gap + 1` 个点时合并为一个异常区间（能把一次持续数点的水平偏移归为一段）。
- **可解释结果**：每段返回基线窗口、最大/平均 |z|、全部触发点及中文原因说明。
- **持久化检索**：SQLite（WAL 模式）保存完整结果，按检测 ID 查询，可按业务 `series_id` 过滤、分页列出。
- **接口文档**：启动后访问 Swagger UI `/docs`、ReDoc `/redoc`。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由与入参校验
│   ├── detection.py   # 滑动窗口稳健异常检测核心算法
│   ├── models.py      # Pydantic 请求/响应模型、时间戳解析
│   └── database.py    # SQLite 存储层
├── examples/
│   ├── request.json          # rolling 模式示例（含一次上升突变与一次下降突变）
│   └── request_guarded.json  # guarded 模式示例（孤立尖刺 + 持续水平切换 + 回落）
├── scripts/
│   └── smoke_test.py  # 算法 + API 端到端冒烟测试
├── data/              # SQLite 数据库目录（compose 挂载命名卷）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

服务启动在 <http://localhost:8000>，数据库保存在命名卷 `ts_data` 中。

验证：

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/v1/detections \
  -H "Content-Type: application/json" \
  -d @examples/request.json
```

### 方式二：本地 Python 运行

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

可选环境变量：`DB_PATH`（SQLite 文件路径，默认 `./data/detections.db`）。

## 接口说明

### `POST /api/v1/detections` — 提交检测

请求体：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `points` | `[{timestamp, value}]` | 必填（≥2） | 时间戳支持 ISO-8601 字符串（`...Z` 或带时区，无时区按 UTC）或 epoch 秒/毫秒数字；无需预先排序，服务端自动排序并拒绝重复时间戳 |
| `series_id` | string | `null` | 业务序列标识，便于过滤检索 |
| `window_size` | int | 20 | 滑动基线窗口大小（2~10000） |
| `min_window` | int | 5 | 开始评估所需的最小基线点数（前 `min_window` 个点不评分） |
| `threshold` | float | 3.0 | 稳健 z 分数阈值，`|z|` 超过即异常 |
| `merge_gap` | int | 2 | 合并区间时允许桥接的连续正常点数 |
| `baseline_policy` | `"rolling"` \| `"guarded"` | `rolling` | 基线策略，见下文 |
| `rebase_after` | int \| null | `null` | guarded 模式连续异常达到该点数即重建基线；默认取 `min_window`，且必须不小于 `min_window`；rolling 模式忽略 |
| `include_points` | bool | false | 响应是否带逐点注解（完整结果始终入库） |

约束：`min_window ≤ window_size`；点数必须大于 `min_window`；`rebase_after` 若提供则必须 `≥ min_window`。

### `GET /api/v1/detections/{id}` — 按 ID 查询

查询参数 `include_points`（默认 `true`）控制是否返回逐点注解。

### `GET /api/v1/detections?series_id=&limit=&offset=` — 列表

按创建时间倒序返回检测摘要，可按 `series_id` 过滤，支持分页。

### `GET /health` — 健康检查

## 调用示例

```bash
# 1) 发起检测（用仓库自带示例，包含上升/下降两段突变）
curl -s -X POST http://localhost:8000/api/v1/detections \
  -H "Content-Type: application/json" \
  -d @examples/request.json | python -m json.tool

# 2) 记下返回的 id，查询完整结果
curl -s http://localhost:8000/api/v1/detections/<ID> | python -m json.tool

# 3) 按业务 series_id 列出历史检测
curl -s "http://localhost:8000/api/v1/detections?series_id=sensor-room-a-demo" | python -m json.tool
```

Python 调用示例：

```python
import requests

resp = requests.post("http://localhost:8000/api/v1/detections", json={
    "series_id": "cpu-usage",
    "window_size": 30,
    "threshold": 3.5,
    "merge_gap": 3,
    "points": [
        {"timestamp": "2026-09-20T10:00:00Z", "value": 12.3},
        {"timestamp": "2026-09-20T10:01:00Z", "value": 12.5},
        # ...
    ],
})
result = resp.json()
for seg in result["segments"]:
    print(seg["start_time"], "->", seg["end_time"], seg["score"]["max_abs_zscore"])
    print(seg["explanation"])

# 后续按 ID 取回
requests.get(f"http://localhost:8000/api/v1/detections/{result['id']}")
```

## 两种基线策略

### `baseline_policy=rolling`（默认）

经典滑动窗口：每个点的基线是其之前 `window_size` 个点（**包括其中的异常点**）。

适合异常多为短尖刺、且希望基线随数据自然滑动的场景。该模式的检测结果与历史版本完全一致；响应中的 `baseline_generation` 恒为 `0`、`baseline_reset` 恒为 `false`、`baseline_rebuild_count` 恒为 `0`、`rebase_after` 恒为 `null`。

```bash
curl -s -X POST http://localhost:8000/api/v1/detections \
  -H "Content-Type: application/json" \
  -d @examples/request.json | python -m json.tool
```

Python：

```python
resp = requests.post("http://localhost:8000/api/v1/detections", json={
    "baseline_policy": "rolling",   # 可省略，默认即 rolling
    "window_size": 20,
    "threshold": 3.0,
    "points": [...],
})
```

### `baseline_policy=guarded`

**防污染的自适应基线**，适合“孤立尖刺 + 真实水平切换”并存的指标（如流量、温度、功耗）：

1. **仅正常点入池**：判为异常的点不进入后续基线，因此一个孤立尖刺既不会抬高/压低其后的中位数，也不会撑大 MAD/离散度，尖刺之后的微小偏离仍能被灵敏检出。
2. **连续异常触发重建（rebase）**：当连续异常点数达到 `rebase_after`（默认取 `min_window`，必须 `≥ min_window`）时，认为指标发生了真实水平切换，系统用**该异常段最近 `min_window` 个点**重建基线，基线代次 `baseline_generation` 由 0 起递增。
3. **触发点不翻案、区间不拆分**：重建触发点仍计为异常（它是相对旧基线的突变），重建也不会把同一个异常区间切成两段；重建之后的点用新基线评分，若已运行在新水平上则恢复为正常。
4. 摘要通过 `baseline_rebuild_count` 返回重建次数；逐点注解的 `baseline_generation` 表示评分该点时使用的代次，`baseline_reset=true` 标记重建触发点。

```bash
# 仓库自带 guarded 示例：一个孤立尖刺 + 一段持续抬升（触发重建）+ 回落到原水平（再次重建）
curl -s -X POST http://localhost:8000/api/v1/detections \
  -H "Content-Type: application/json" \
  -d @examples/request_guarded.json | python -m json.tool
```

显式指定重建阈值（必须不小于 `min_window`，否则返回 422）：

```bash
curl -s -X POST http://localhost:8000/api/v1/detections \
  -H "Content-Type: application/json" \
  -d '{
    "series_id": "cpu-usage",
    "baseline_policy": "guarded",
    "min_window": 5,
    "rebase_after": 8,
    "threshold": 3.0,
    "points": [
      {"timestamp": "2026-09-20T10:00:00Z", "value": 12.3},
      {"timestamp": "2026-09-20T10:01:00Z", "value": 12.5}
    ]
  }'
```

Python：

```python
resp = requests.post("http://localhost:8000/api/v1/detections", json={
    "baseline_policy": "guarded",
    "rebase_after": 5,          # 连续 5 个异常即重建；省略时取 min_window
    "window_size": 20,
    "min_window": 5,
    "threshold": 3.0,
    "include_points": True,     # 需要逐点看 baseline_generation / baseline_reset
    "points": [...],
})
result = resp.json()
print("重建次数:", result["baseline_rebuild_count"])
for p in result["points"]:
    if p["baseline_reset"]:
        print("基线在该点重建:", p["timestamp"], "重建前代次:", p["baseline_generation"])
```

## 响应结构（节选）

```json
{
  "id": "b601c077...",
  "series_id": "sensor-room-a-demo",
  "created_at": "2026-09-20T12:00:00.000000+00:00",
  "point_count": 46,
  "anomaly_segment_count": 2,
  "anomaly_point_count": 6,
  "baseline_policy": "rolling",
  "rebase_after": null,
  "baseline_rebuild_count": 0,
  "segments": [
    {
      "start_index": 24,
      "end_index": 26,
      "start_time": "2026-09-20T10:00:00+00:00",
      "end_time": "2026-09-20T10:10:00+00:00",
      "point_count": 3,
      "anomaly_point_count": 3,
      "baseline": {
        "median": 10.05,
        "mad": 0.1,
        "robust_sd": 0.22239,
        "dispersion_method": "mad",
        "scale": 1.4826,
        "window_size": 20,
        "window_start_time": "...",
        "window_end_time": "..."
      },
      "score": {
        "max_abs_zscore": 27.65,
        "max_signed_zscore": 27.65,
        "max_zscore_time": "2026-09-20T10:05:00+00:00",
        "mean_abs_zscore": 23.38
      },
      "triggers": [
        {
          "index": 24,
          "timestamp": "2026-09-20T10:00:00+00:00",
          "value": 13.8,
          "zscore": 16.86,
          "baseline_median": 10.05,
          "deviation": 3.75,
          "direction": "上升",
          "reason": "稳健 z 分数 +16.86 的绝对值超过阈值 3：观测值 13.8 相对滑动基线中位数 10.05 上升偏离 3.75（1.4826·MAD 稳健标准差 0.2224）"
        }
      ],
      "explanation": "区间 10:00 ~ 10:10（3 个点，其中 3 个点触发）：..."
    }
  ],
  "points": [
    {
      "index": 24,
      "timestamp": "...",
      "value": 13.8,
      "zscore": 16.86,
      "is_anomaly": true,
      "baseline_generation": 0,
      "baseline_reset": false
    }
  ]
}
```

逐点注解中的代次字段：

- `baseline_generation`：评分该点时所用基线的代次。初始为 `0`；guarded 模式每重建一次基线，其后点的代次 +1。rolling 模式恒为 `0`。
- `baseline_reset`：该点是否为重建触发点（连续异常达到 `rebase_after`）。触发点本身仍为异常，且其 `baseline_generation` 是**重建前**的代次（它是被旧基线判为异常的）。rolling 模式恒为 `false`。
- 摘要中的 `baseline_rebuild_count` 为本序列基线重建总次数；`rebase_after` 为实际生效的重建阈值（guarded 未传参时回显 `min_window`；rolling 恒为 `null`）。

`dispersion_method` 取值：

- `mad`：常规稳健尺度 `1.4826 × MAD`；
- `std_fallback`：窗口内 MAD 为 0（如小窗口落在离散 / 多峰信号上），回退为样本标准差；
- `constant`：MAD 与标准差都为 0（基线完全恒定），此时任何偏离基线的值按 `z=999` 的最强突变处理。

## 算法说明

1. 对每个索引 `i ≥ min_window`，取 `values[max(0, i-window_size):i]` 作为基线窗口（严格历史窗口，不使用当前点与未来点）。
2. 计算窗口中位数 `med` 与 MAD；稳健尺度 `σ = 1.4826 × MAD`，MAD=0 时回退样本标准差。
3. `z_i = (x_i − med) / σ`，`|z_i| ≥ threshold` 标记为触发点；`σ = 0` 且 `x_i ≠ med` 时 `z_i = 999`。
4. 扫描触发点序列：若相邻两个触发点索引差 `≤ merge_gap + 1`，则把中间正常点桥接，合并成一个异常区间。
5. 每段输出：首个触发点的基线窗口、段内最大/平均 |z| 与峰值时刻、全部触发点（含各自窗口基线与原因）、整段中文解释。

### rolling 与 guarded 的差异

- **rolling**：第 1 步的基线窗口就是原序列的 trailing window，异常点也会随窗口滑入。
- **guarded**：维护一个“基线池”（容量上限 `window_size`，初始为前 `min_window` 个点）：
  1. 每个点用当前基线池评分；判为**正常**才追加进池（异常点永不入池，故孤立尖刺不污染其后的中位数与离散度）。
  2. 维护当前连续异常段；其长度达到 `rebase_after`（默认 `min_window`，且不允许更小）时，用该异常段**最近 `min_window` 个点**重建基线池，`baseline_generation += 1`，并在触发点上标记 `baseline_reset=true`。
  3. 触发点不重新判定（仍为异常），异常区间按统一的 `merge_gap` 规则合并，重建不会拆分区间；重建后的点用新代次基线评分。

与基于均值/标准差的方案相比，中位数 + MAD 对窗口内少量离群点不敏感，能在尖刺和水平偏移共存时给出稳定基线；guarded 策略进一步避免异常点本身参与基线，适合存在真实水平切换的长序列。

## 运行测试

```bash
pip install -r requirements.txt httpx
python scripts/smoke_test.py
```

测试覆盖：单点突变、持续水平偏移的识别与合并、恒定基线退化处理、无异常数据、rolling 与 guarded 两种基线策略（尖刺防污染、连续异常重建与代次递增、触发点保持异常且区间不拆分）、接口提交/查询/列表/404/参数校验。

## 备注

- SQLite 文件随命名卷持久化；删除卷即清空数据：`docker compose down -v`。
- 算法时间复杂度 `O(n · window_size)`，适合单次万级点数以内的离线/准实时检测。
