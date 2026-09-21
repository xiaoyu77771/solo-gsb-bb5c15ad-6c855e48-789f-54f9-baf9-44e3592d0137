"""端到端冒烟测试：算法正确性 + FastAPI 接口 + SQLite 持久化。

运行（需安装 requirements）：
    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.detection import analyze_series  # noqa: E402
from app.main import app  # noqa: E402


def test_algorithm_spikes_and_shift() -> None:
    n = 40
    values = [10.0 + (0.1 if i % 2 == 0 else -0.1) for i in range(n)]
    # 单点突增
    values[25] = 20.0
    # 水平偏移：连续 5 个点明显抬升
    values[30:35] = [16.0, 16.1, 15.9, 16.2, 15.8]
    ts = [f"2026-09-20T08:{i:02d}:00Z" for i in range(n)]

    result = analyze_series(ts, values, window_size=20, min_window=5, threshold=3.0, merge_gap=2)

    assert result["anomaly_segment_count"] == 2, result["segments"]
    seg1, seg2 = result["segments"]

    # 单点突增区间
    assert seg1["start_index"] == seg1["end_index"] == 25
    assert seg1["score"]["max_abs_zscore"] >= 3.0
    assert seg1["triggers"][0]["direction"] == "上升"

    # 水平偏移区间，5 个点应被合并为一个区间
    assert seg2["start_index"] == 30 and seg2["end_index"] == 34
    assert seg2["point_count"] == 5
    assert "基线" in seg2["explanation"]

    # 逐点注解
    annotated = result["points"]
    assert len(annotated) == n - 5
    assert all("zscore" in p and "baseline_median" in p for p in annotated)
    print("✓ 算法测试通过：单点突变 + 水平偏移识别与合并正确")


def test_constant_baseline() -> None:
    values = [5.0] * 15 + [8.0, 5.0]
    ts = [f"2026-09-20T09:{i:02d}:00Z" for i in range(len(values))]
    result = analyze_series(ts, values, window_size=10, min_window=5, threshold=3.0, merge_gap=1)
    assert result["anomaly_segment_count"] == 1
    trigger = result["segments"][0]["triggers"][0]
    assert trigger["zscore"] == 999.0
    assert "基线恒定" in trigger["reason"]
    print("✓ 恒定基线（MAD=0）退化为强突变哨兵处理正确")


def test_no_anomaly() -> None:
    values = [1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.02, 0.98, 1.07, 0.93]
    ts = [f"2026-09-20T10:{i:02d}:00Z" for i in range(len(values))]
    result = analyze_series(ts, values, window_size=5, min_window=3, threshold=5.0, merge_gap=1)
    assert result["anomaly_segment_count"] == 0
    assert result["anomaly_point_count"] == 0
    print("✓ 无异常数据正确返回空区间")


def test_rolling_default_fields() -> None:
    """rolling 模式（默认）输出新增字段的默认值，且代次/重建信息恒定。"""
    values = [10.0] * 12 + [13.0]
    ts = [f"2026-09-20T11:{i:02d}:00Z" for i in range(len(values))]
    result = analyze_series(ts, values, window_size=10, min_window=5, threshold=3.0)
    assert result["baseline_policy"] == "rolling"
    assert result["rebase_after"] is None
    assert result["baseline_rebuild_count"] == 0
    assert all(p["baseline_generation"] == 0 for p in result["points"])
    assert all(p["baseline_reset"] is False for p in result["points"])
    print("✓ rolling 模式默认字段（policy=rolling/rebase_after=null/代次 0/无重建）正确")


def test_guarded_spike_not_polluting_baseline() -> None:
    """guarded 模式：孤立尖刺不得进入后续基线，基线保持恒定、可继续检出小偏离。"""
    # 恒定 10 基线；i=6 为孤立尖刺 20；i=7 仅偏离 0.1
    values = [10.0] * 6 + [20.0, 10.1] + [10.0] * 4
    ts = [f"2026-09-20T12:{i:02d}:00Z" for i in range(len(values))]

    rolling = analyze_series(ts, values, window_size=20, min_window=5, threshold=3.0)
    guarded = analyze_series(
        ts, values, window_size=20, min_window=5, threshold=3.0,
        baseline_policy="guarded",
    )

    # rolling：尖刺进入窗口撑大离散度，10.1 被压成正常（std 回退、z≈0.03）
    assert rolling["points"][7 - 5]["is_anomaly"] is False

    # guarded：尖刺被排除，基线仍恒定，10.1 按强突变检出
    p7 = guarded["points"][7 - 5]
    assert p7["is_anomaly"] is True
    assert p7["dispersion_method"] == "constant"
    assert p7["zscore"] == 999.0

    # 尖刺本身仍为异常、没有触发重建（单点 run 未达 rebase_after）
    p6 = guarded["points"][6 - 5]
    assert p6["is_anomaly"] is True and p6["baseline_reset"] is False
    assert guarded["baseline_rebuild_count"] == 0
    # 未显式传 rebase_after 时取 min_window
    assert guarded["rebase_after"] == 5
    print("✓ guarded 模式孤立尖刺不污染后续基线（中位数/离散度保持纯净）")


def test_guarded_rebuild_keeps_segment() -> None:
    """guarded 模式：连续异常达到 rebase_after 时重建基线、代次递增；触发点仍异常且区间不拆分。"""
    # 10 个正常点 10，之后永久切换到 8；min_window=5、rebase_after=5
    values = [10.0] * 10 + [8.0] * 10
    ts = [f"2026-09-20T13:{i:02d}:00Z" for i in range(len(values))]
    result = analyze_series(
        ts, values, window_size=10, min_window=5, threshold=3.0, merge_gap=1,
        baseline_policy="guarded", rebase_after=5,
    )

    # 10..14 为异常段（15 起用新基线 8 评分，恢复正常），区间不被重建拆分
    assert result["anomaly_segment_count"] == 1
    seg = result["segments"][0]
    assert seg["start_index"] == 10 and seg["end_index"] == 14

    points = result["points"]
    # 重建恰好一次；触发点 14 仍为异常，baseline_reset=True，代次仍为 0（用旧基线评分）
    assert result["baseline_rebuild_count"] == 1
    p14 = points[14 - 5]
    assert p14["baseline_reset"] is True and p14["is_anomaly"] is True
    assert p14["baseline_generation"] == 0
    # 重建之后代次为 1，基线中心迁移到 8，后续点正常
    p15 = points[15 - 5]
    assert p15["baseline_generation"] == 1
    assert p15["baseline_reset"] is False and p15["is_anomaly"] is False
    assert abs(p15["baseline_median"] - 8.0) < 1e-9
    # 触发点全部保持异常
    assert all(points[k - 5]["is_anomaly"] for k in range(10, 15))
    print("✓ guarded 模式连续异常重建：代次递增、触发点保持异常、原区间不拆分")


def test_guarded_demo_two_generations() -> None:
    """guarded 示例数据：一次孤立尖刺 + 持续抬升 + 回落到原水平，共两次重建。"""
    n = 44
    values = [10.0 + (0.1 if i % 2 == 0 else -0.1) for i in range(n)]
    values[22] = 20.0
    for i in range(26, 36):
        values[i] = 16.0 + (0.1 if i % 2 == 0 else -0.1)
    ts = [f"2026-09-20T08:{i:02d}:00Z" for i in range(n)]

    guarded = analyze_series(
        ts, values, window_size=20, min_window=5, threshold=3.0, merge_gap=2,
        baseline_policy="guarded", rebase_after=5,
    )
    assert guarded["baseline_rebuild_count"] == 2
    assert [(s["start_index"], s["end_index"]) for s in guarded["segments"]] == [(22, 22), (26, 30), (36, 40)]
    reset_points = {p["index"]: p["baseline_generation"] for p in guarded["points"] if p["baseline_reset"]}
    # 两次重建分别在索引 30（0→1）与 40（1→2）；触发点注解保留评分时代次
    assert reset_points == {30: 0, 40: 1}
    print("✓ guarded 多代次重建（抬升适配 + 回落再适配）正确")


def test_api_end_to_end(tmp_db: str = "/tmp/tsad_smoke.db") -> None:
    import os

    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    os.environ["DB_PATH"] = tmp_db

    # 重新初始化数据库（app 模块已导入，直接调用 init_db）
    from app import database

    database.DB_PATH = tmp_db
    database.init_db(tmp_db)

    with TestClient(app) as client:
        _exercise_api(client)


def _exercise_api(client: "TestClient") -> None:
    # 健康检查
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # 提交检测
    payload = {
        "series_id": "test-series",
        "window_size": 10,
        "min_window": 3,
        "threshold": 3.0,
        "merge_gap": 1,
        "include_points": True,
        "points": [
            {"timestamp": f"2026-09-20T08:{i:02d}:00", "value": 1.0 + (i % 2) * 0.05}
            for i in range(15)
        ]
        + [{"timestamp": "2026-09-20T08:15:00", "value": 9.0}],
    }
    r = client.post("/api/v1/detections", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    detection_id = body["id"]
    assert body["anomaly_segment_count"] == 1
    assert body["segments"][0]["triggers"][0]["timestamp"] == "2026-09-20T08:15:00+00:00"
    assert len(body["points"]) > 0

    # 按 ID 查询
    r = client.get(f"/api/v1/detections/{detection_id}")
    assert r.status_code == 200
    assert r.json()["id"] == detection_id
    assert r.json()["segments"][0]["baseline"]["window_size"] == 10

    # include_points=false 不返回逐点
    r = client.get(f"/api/v1/detections/{detection_id}", params={"include_points": False})
    assert r.json()["points"] is None

    # 列表 + series_id 过滤
    r = client.get("/api/v1/detections", params={"series_id": "test-series"})
    assert r.status_code == 200 and r.json()["total"] == 1

    # 404
    r = client.get("/api/v1/detections/nonexistent")
    assert r.status_code == 404

    # 参数校验：min_window > window_size
    bad = dict(payload)
    bad["min_window"], bad["window_size"] = 20, 10
    r = client.post("/api/v1/detections", json=bad)
    assert r.status_code == 422

    # 参数校验：rebase_after < min_window
    bad = dict(payload)
    bad["baseline_policy"] = "guarded"
    bad["rebase_after"] = 2
    r = client.post("/api/v1/detections", json=bad)
    assert r.status_code == 422, r.text

    # 参数校验：非法 baseline_policy
    bad = dict(payload)
    bad["baseline_policy"] = "strict"
    r = client.post("/api/v1/detections", json=bad)
    assert r.status_code == 422

    # guarded 模式经 API 提交：新字段回显、重建计数与代次注解存在
    guarded_payload = dict(payload)
    guarded_payload["series_id"] = "test-series-guarded"
    guarded_payload["baseline_policy"] = "guarded"
    guarded_payload["rebase_after"] = 3
    r = client.post("/api/v1/detections", json=guarded_payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["baseline_policy"] == "guarded"
    assert body["rebase_after"] == 3
    assert "baseline_rebuild_count" in body
    assert all("baseline_generation" in p and "baseline_reset" in p for p in body["points"])

    # 默认 rolling 模式经 API 提交：新字段默认值正确
    r = client.post("/api/v1/detections", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["baseline_policy"] == "rolling"
    assert body["rebase_after"] is None
    assert body["baseline_rebuild_count"] == 0

    print("✓ API 端到端测试通过：提交 / 查询 / 列表 / 404 / 参数校验 / 两种基线策略")


if __name__ == "__main__":
    test_algorithm_spikes_and_shift()
    test_constant_baseline()
    test_no_anomaly()
    test_rolling_default_fields()
    test_guarded_spike_not_polluting_baseline()
    test_guarded_rebuild_keeps_segment()
    test_guarded_demo_two_generations()
    test_api_end_to_end()
    print("\n全部冒烟测试通过 ✅")
