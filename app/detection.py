"""稳健滑动窗口异常检测核心算法。

思路：
1. 对每个点，取其之前 ``window_size`` 个点（严格 trailing window）计算
   稳健基线：中位数 median 与 MAD（median absolute deviation）。
2. 以 MAD * 1.4826 作为稳健标准差估计，计算稳健 z 分数：
       z = (x - median) / (1.4826 * MAD)
   当 |z| >= threshold 时标记为异常点（突变 / 水平偏移）。
3. 允许把相邻异常点之间至多 ``merge_gap`` 个正常点桥接，合并成异常区间。
4. 每个异常区间给出基线、分数与触发点的人类可读解释。

基线策略（``baseline_policy``）：
- ``rolling``（默认）：原始滑动窗口，异常点也会随窗口滑入后续基线，
  结果与早期版本完全一致。
- ``guarded``：只有判为正常的点才允许进入后续滑动基线，孤立尖刺不会
  污染其后的中位数与离散度；当连续异常点数达到 ``rebase_after``（不小于
  ``min_window``）时，认为发生了真实水平切换，用该异常段最近
  ``min_window`` 个点重建基线并把基线代次（generation）加一。重建触发点
  仍保持异常，重建也不会拆分原异常区间。
"""

from __future__ import annotations

import math
from collections import deque
from statistics import median
from typing import Any

# 正态分布下 MAD 与标准差的换算系数
MAD_SCALE = 1.4826
# 基线恒定时（MAD=0 且标准差=0）又与基线不一致，使用的有限大 z 分数哨兵值
ZERO_MAD_ZSCORE = 999.0

# 支持的基线策略
POLICY_ROLLING = "rolling"
POLICY_GUARDED = "guarded"


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
    baseline_policy: str = POLICY_ROLLING,
    rebase_after: int | None = None,
) -> dict[str, Any]:
    """执行检测并返回可 JSON 序列化的完整结果。

    :param timestamps: 已排序、已去重的 ISO-8601 时间戳字符串
    :param values: 与 timestamps 对齐的数值
    :param baseline_policy: ``rolling``（默认）或 ``guarded``
    :param rebase_after: guarded 模式下连续异常达到该点数即重建基线；
           默认取 ``min_window``；必须不小于 ``min_window``。rolling 模式忽略。
    """
    n = len(values)
    # 每个被评估点的注解；前 min_window 个点没有足够基线，不参与评估
    annotations: list[dict[str, Any] | None] = [None] * n
    is_anomaly = [False] * n
    zscores: list[float | None] = [None] * n
    # 每个被评估点实际使用的基线窗口索引（区间解释时复用，保证与逐点评分一致）
    baseline_refs: list[list[int] | None] = [None] * n

    evaluated_start = min_window

    if baseline_policy == POLICY_GUARDED:
        effective_rebase = min_window if rebase_after is None else rebase_after
        rebuild_count = _run_guarded(
            timestamps=timestamps,
            values=values,
            window_size=window_size,
            min_window=min_window,
            threshold=threshold,
            rebase_after=effective_rebase,
            annotations=annotations,
            is_anomaly=is_anomaly,
            zscores=zscores,
            baseline_refs=baseline_refs,
        )
        summary_rebase_after: int | None = effective_rebase
    else:
        _run_rolling(
            timestamps=timestamps,
            values=values,
            window_size=window_size,
            min_window=min_window,
            threshold=threshold,
            annotations=annotations,
            is_anomaly=is_anomaly,
            zscores=zscores,
            baseline_refs=baseline_refs,
        )
        rebuild_count = 0
        summary_rebase_after = None

    segments = _build_segments(
        timestamps=timestamps,
        values=values,
        is_anomaly=is_anomaly,
        zscores=zscores,
        annotations=annotations,
        baseline_refs=baseline_refs,
        window_size=window_size,
        threshold=threshold,
        merge_gap=merge_gap,
        evaluated_start=evaluated_start,
        baseline_policy=baseline_policy,
        rebase_after=summary_rebase_after,
    )

    return {
        "point_count": n,
        "window_size": window_size,
        "min_window": min_window,
        "threshold": threshold,
        "merge_gap": merge_gap,
        "baseline_policy": baseline_policy,
        "rebase_after": summary_rebase_after,
        "baseline_rebuild_count": rebuild_count,
        "evaluated_start_index": evaluated_start,
        "evaluated_point_count": max(0, n - evaluated_start),
        "anomaly_point_count": sum(1 for i in range(evaluated_start, n) if is_anomaly[i]),
        "anomaly_segment_count": len(segments),
        "segments": segments,
        "points": [annotations[i] for i in range(evaluated_start, n)],
    }


def _run_rolling(
    *,
    timestamps: list[str],
    values: list[float],
    window_size: int,
    min_window: int,
    threshold: float,
    annotations: list[dict[str, Any] | None],
    is_anomaly: list[bool],
    zscores: list[float | None],
    baseline_refs: list[list[int] | None],
) -> None:
    """原始滑动窗口评分：所有历史点（含异常点）都随窗口进入基线。

    数值结果与历史版本完全一致；额外记录基线代次（恒为 0）/ 重建标记（恒为
    False）/ 实际基线窗口索引，仅用于统一输出结构。
    """
    n = len(values)
    for i in range(min_window, n):
        lo = max(0, i - window_size)
        refs = list(range(lo, i))
        history = [values[k] for k in refs]
        med, mad, robust_sd, dispersion_method = _window_stats(history)
        z, degenerate = _zscore(values[i], med, robust_sd)
        zscores[i] = z
        is_anomaly[i] = abs(z) >= threshold

        baseline_refs[i] = refs
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
            "baseline_generation": 0,
            "baseline_reset": False,
        }


def _run_guarded(
    *,
    timestamps: list[str],
    values: list[float],
    window_size: int,
    min_window: int,
    threshold: float,
    rebase_after: int,
    annotations: list[dict[str, Any] | None],
    is_anomaly: list[bool],
    zscores: list[float | None],
    baseline_refs: list[list[int] | None],
) -> int:
    """guarded 评分，返回基线重建次数。

    规则：
    - 基线池只纳入判为正常的点；异常点（孤立尖刺）永不进入后续基线。
    - 维护当前连续异常段；长度达到 ``rebase_after`` 时，用该段最近
      ``min_window`` 个点重建基线池，代次 +1；触发点仍标记为异常。
    """
    n = len(values)
    # 当前基线池：只含正常点（或重建时被选为新基线的异常段尾部），
    # 容量上限 window_size（重建后至少有 min_window 个点）。
    pool: deque[int] = deque(range(min_window), maxlen=window_size)
    generation = 0
    run: list[int] = []  # 当前连续异常段的索引
    rebuild_count = 0

    for i in range(min_window, n):
        # 评分前的基线窗口（重建发生在评分之后，不影响本点）
        refs = list(pool)
        history = [values[k] for k in refs]
        med, mad, robust_sd, dispersion_method = _window_stats(history)
        z, degenerate = _zscore(values[i], med, robust_sd)
        zscores[i] = z
        point_is_anomaly = abs(z) >= threshold
        is_anomaly[i] = point_is_anomaly

        # 本点使用的是重建前的代次；重建标记落在触发点上
        point_generation = generation
        reset = False

        if point_is_anomaly:
            run.append(i)
            if len(run) >= rebase_after:
                # 连续异常达到阈值：视为真实水平切换，用异常段最近
                # min_window 个点重建基线（rebase_after >= min_window）
                pool = deque(run[-min_window:], maxlen=window_size)
                generation += 1
                rebuild_count += 1
                reset = True
                run = []
        else:
            # 只有正常点才能进入后续滑动基线
            run = []
            pool.append(i)

        baseline_refs[i] = refs
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
            "is_anomaly": point_is_anomaly,
            "baseline_generation": point_generation,
            "baseline_reset": reset,
        }

    return rebuild_count


def _build_segments(
    *,
    timestamps: list[str],
    values: list[float],
    is_anomaly: list[bool],
    zscores: list[float | None],
    annotations: list[dict[str, Any] | None],
    baseline_refs: list[list[int] | None],
    window_size: int,
    threshold: float,
    merge_gap: int,
    evaluated_start: int,
    baseline_policy: str,
    rebase_after: int | None,
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
                baseline_refs=baseline_refs,
                threshold=threshold,
                baseline_policy=baseline_policy,
                rebase_after=rebase_after,
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
    baseline_refs: list[list[int] | None],
    threshold: float,
    baseline_policy: str,
    rebase_after: int | None,
) -> dict[str, Any]:
    """构造单个异常区间：基线、分数、触发点与解释。"""
    # 区间基线取第一个触发点评分时实际使用的基线窗口（突变发生前的基线；
    # guarded 模式下可能来自更早代次的正常点池）
    base_indices = baseline_refs[seg_start]
    assert base_indices is not None
    baseline = [values[k] for k in base_indices]
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

    if baseline_policy == POLICY_GUARDED:
        # guarded 模式补充基线代次 / 重建信息（不影响区间边界：重建不拆区间）
        seg_resets = [
            k
            for k in range(seg_start, seg_end + 1)
            if ann_is_reset(annotations[k])
        ]
        gen_start = ann_generation(annotations[seg_start])
        gen_end = ann_generation(annotations[seg_end])
        explanation += (
            f"guarded 模式下本区间内基线共重建 {len(seg_resets)} 次"
            f"（连续异常每达 {rebase_after} 个点触发一次，重建触发点仍计为异常），"
            f"评分基线由第 {gen_start} 代推进至第 {gen_end} 代，区间未因重建而拆分。"
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
            "window_start_time": timestamps[base_indices[0]],
            "window_end_time": timestamps[base_indices[-1]],
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


def ann_is_reset(ann: dict[str, Any] | None) -> bool:
    """读取注解上的基线重建标记（None 表示该点未参与评估）。"""
    return bool(ann and ann["baseline_reset"])


def ann_generation(ann: dict[str, Any] | None) -> int:
    """读取注解上的基线代次（None 表示该点未参与评估）。"""
    assert ann is not None
    return int(ann["baseline_generation"])
