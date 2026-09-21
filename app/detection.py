"""稳健滑动窗口异常检测核心算法。

思路：
1. 对每个点，取其之前 ``window_size`` 个点（严格 trailing window）计算
   稳健基线：中位数 median 与 MAD（median absolute deviation）。
2. 以 MAD * 1.4826 作为稳健标准差估计，计算稳健 z 分数：
       z = (x - median) / (1.4826 * MAD)
   当 |z| >= threshold 时标记为异常点（突变 / 水平偏移）。
3. 允许把相邻异常点之间至多 ``merge_gap`` 个正常点桥接，合并成异常区间。
4. 每个异常区间给出基线、分数与触发点的人类可读解释。
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

# 正态分布下 MAD 与标准差的换算系数
MAD_SCALE = 1.4826
# 基线恒定时（MAD=0 且标准差=0）又与基线不一致，使用的有限大 z 分数哨兵值
ZERO_MAD_ZSCORE = 999.0


def _sample_std(values: list[float], mean: float) -> float:
    """样本标准差（n-1 修正）；点数不足 2 时返回 0。"""
    if len(values) < 2:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)


def _window_stats(history: list[float]) -> tuple[float, float, float, str]:
    """返回 (中位数, 原始MAD, 稳健离散度, 离散度方法)。

    优先使用 1.4826*MAD；当 MAD=0（小窗口落在多峰 / 离散信号上会误判）时，
    回退到样本标准差；两者都为 0 才视为真正退化的恒定基线。
    """
    med = median(history)
    mad = median([abs(x - med) for x in history])
    robust_sd = mad * MAD_SCALE
    if robust_sd > 0:
        return med, mad, robust_sd, "mad"
    mean = sum(history) / len(history)
    std = _sample_std(history, mean)
    if std > 0:
        return med, mad, std, "std_fallback"
    return med, mad, 0.0, "constant"


def _zscore(value: float, med: float, robust_sd: float) -> tuple[float, bool]:
    """计算稳健 z 分数；返回 (zscore, 基线是否退化)。"""
    degenerate = robust_sd == 0.0
    if degenerate:
        return (0.0 if value == med else ZERO_MAD_ZSCORE), True
    return (value - med) / robust_sd, False


def analyze_series(
    timestamps: list[str],
    values: list[float],
    window_size: int = 20,
    min_window: int = 5,
    threshold: float = 3.0,
    merge_gap: int = 2,
) -> dict[str, Any]:
    """执行检测并返回可 JSON 序列化的完整结果。

    :param timestamps: 已排序、已去重的 ISO-8601 时间戳字符串
    :param values: 与 timestamps 对齐的数值
    """
    n = len(values)
    # 每个被评估点的注解；前 min_window 个点没有足够基线，不参与评估
    annotations: list[dict[str, Any] | None] = [None] * n
    is_anomaly = [False] * n
    zscores: list[float | None] = [None] * n

    evaluated_start = min_window
    for i in range(min_window, n):
        lo = max(0, i - window_size)
        history = values[lo:i]
        med, mad, robust_sd, dispersion_method = _window_stats(history)
        z, degenerate = _zscore(values[i], med, robust_sd)
        zscores[i] = z
        is_anomaly[i] = abs(z) >= threshold

        annotations[i] = {
            "index": i,
            "timestamp": timestamps[i],
            "value": values[i],
            "baseline_median": med,
            "baseline_mad": mad,
            "baseline_robust_sd": robust_sd,
            "dispersion_method": dispersion_method,
            "zscore": z,
            "degenerate_baseline": degenerate,
            "is_anomaly": is_anomaly[i],
        }

    segments = _build_segments(
        timestamps=timestamps,
        values=values,
        is_anomaly=is_anomaly,
        zscores=zscores,
        annotations=annotations,
        window_size=window_size,
        threshold=threshold,
        merge_gap=merge_gap,
        evaluated_start=evaluated_start,
    )

    return {
        "point_count": n,
        "window_size": window_size,
        "min_window": min_window,
        "threshold": threshold,
        "merge_gap": merge_gap,
        "evaluated_start_index": evaluated_start,
        "evaluated_point_count": max(0, n - evaluated_start),
        "anomaly_point_count": sum(1 for i in range(evaluated_start, n) if is_anomaly[i]),
        "anomaly_segment_count": len(segments),
        "segments": segments,
        "points": [annotations[i] for i in range(evaluated_start, n)],
    }


def _build_segments(
    *,
    timestamps: list[str],
    values: list[float],
    is_anomaly: list[bool],
    zscores: list[float | None],
    annotations: list[dict[str, Any] | None],
    window_size: int,
    threshold: float,
    merge_gap: int,
    evaluated_start: int,
) -> list[dict[str, Any]]:
    """扫描异常布尔序列，按 merge_gap 合并相邻异常点为区间。"""
    n = len(values)
    segments: list[dict[str, Any]] = []
    i = evaluated_start

    while i < n:
        if not is_anomaly[i]:
            i += 1
            continue

        seg_start = i
        last_anomaly = i
        j = i + 1
        # 相邻两个异常点索引差 <= merge_gap + 1 即允许桥接
        while j < n and (j - last_anomaly) <= merge_gap + 1:
            if is_anomaly[j]:
                last_anomaly = j
            j += 1
        seg_end = last_anomaly

        segments.append(
            _describe_segment(
                seg_start=seg_start,
                seg_end=seg_end,
                timestamps=timestamps,
                values=values,
                is_anomaly=is_anomaly,
                zscores=zscores,
                annotations=annotations,
                window_size=window_size,
                threshold=threshold,
            )
        )
        i = seg_end + 1

    return segments


def _describe_segment(
    *,
    seg_start: int,
    seg_end: int,
    timestamps: list[str],
    values: list[float],
    is_anomaly: list[bool],
    zscores: list[float | None],
    annotations: list[dict[str, Any] | None],
    window_size: int,
    threshold: float,
) -> dict[str, Any]:
    """构造单个异常区间：基线、分数、触发点与解释。"""
    # 区间基线取第一个触发点的 trailing window（突变发生前的基线）
    base_lo = max(0, seg_start - window_size)
    base_hi = seg_start
    baseline = [values[k] for k in range(base_lo, base_hi)]
    med, mad, robust_sd, dispersion_method = _window_stats(baseline)

    trigger_indices = [k for k in range(seg_start, seg_end + 1) if is_anomaly[k]]
    triggers: list[dict[str, Any]] = []
    max_idx = trigger_indices[0]
    for k in trigger_indices:
        z = zscores[k]
        assert z is not None
        ann = annotations[k]
        assert ann is not None
        point_med = ann["baseline_median"]
        point_sd = ann["baseline_robust_sd"]
        point_method = ann["dispersion_method"]
        deviation = values[k] - point_med
        direction = "上升" if deviation > 0 else "下降"
        if ann["degenerate_baseline"]:
            reason = (
                f"基线窗口内 MAD=0 且标准差=0（基线恒定为 {point_med:g}），观测值 {values[k]:g} "
                f"与基线不一致，按最强突变处理（z={z:g}，阈值 {threshold:g}）"
            )
        elif point_method == "std_fallback":
            reason = (
                f"基线窗口 MAD=0，回退为样本标准差尺度：z 分数 {z:+.2f} 的绝对值"
                f"超过阈值 {threshold:g}：观测值 {values[k]:g} 相对滑动基线中位数 {point_med:g} "
                f"{direction}偏离 {abs(deviation):g}（样本标准差 {point_sd:.4g}）"
            )
        else:
            reason = (
                f"稳健 z 分数 {z:+.2f} 的绝对值超过阈值 {threshold:g}："
                f"观测值 {values[k]:g} 相对滑动基线中位数 {point_med:g} "
                f"{direction}偏离 {abs(deviation):g}（1.4826·MAD 稳健标准差 {point_sd:.4g}）"
            )
        triggers.append(
            {
                "index": k,
                "timestamp": timestamps[k],
                "value": values[k],
                "zscore": z,
                "baseline_median": point_med,
                "baseline_mad": ann["baseline_mad"],
                "dispersion_method": point_method,
                "deviation": deviation,
                "direction": direction,
                "reason": reason,
            }
        )
        if abs(z) > abs(zscores[max_idx] or 0.0):
            max_idx = k

    seg_z = [zscores[k] for k in range(seg_start, seg_end + 1) if zscores[k] is not None]
    mean_abs_z = sum(abs(z) for z in seg_z) / len(seg_z)
    max_z = zscores[max_idx]
    length = seg_end - seg_start + 1
    span_direction = triggers[0]["direction"]

    explanation = (
        f"区间 {timestamps[seg_start]} ~ {timestamps[seg_end]}（{length} 个点，"
        f"其中 {len(triggers)} 个点触发）：首个触发点为 {timestamps[seg_start]}，"
        f"值 {values[seg_start]:g} 相对基线中位数 {med:g} {span_direction}突变；"
        f"区间最大 |z|={abs(max_z):.2f}（{timestamps[max_idx]}，值 {values[max_idx]:g}），"
        f"平均 |z|={mean_abs_z:.2f}，判定阈值 {threshold:g}。"
    )

    return {
        "start_index": seg_start,
        "end_index": seg_end,
        "start_time": timestamps[seg_start],
        "end_time": timestamps[seg_end],
        "point_count": length,
        "anomaly_point_count": len(triggers),
        "baseline": {
            "median": med,
            "mad": mad,
            "robust_sd": robust_sd,
            "dispersion_method": dispersion_method,
            "scale": MAD_SCALE,
            "window_size": len(baseline),
            "window_start_time": timestamps[base_lo],
            "window_end_time": timestamps[base_hi - 1],
        },
        "score": {
            "max_abs_zscore": abs(max_z),
            "max_signed_zscore": max_z,
            "max_zscore_index": max_idx,
            "max_zscore_time": timestamps[max_idx],
            "mean_abs_zscore": mean_abs_z,
        },
        "triggers": triggers,
        "explanation": explanation,
    }
