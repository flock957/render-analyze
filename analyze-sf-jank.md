---
name: analyze-sf-jank
type: knowledge
triggers:
  - sf jank
  - surfaceflinger
  - SF卡顿
  - display hal
  - vsync
  - 丢帧
---

# SurfaceFlinger Jank 分析

分析 SurfaceFlinger 层的 7 类 Jank：SF CPU/GPU 超时、Display HAL 延迟、VSync 预测错误、SF 调度异常、SF Stuffing、帧丢弃。

## 前置条件

- trace_processor 已启动（Phase 1）
- Jank 指标已初始化（Phase 3）
- 环境变量 `RENDER_OUTPUT` 已设置（run_analysis.py 自动处理）

## 脚本

```bash
python3 scripts/analyze_sf_jank.py \
  --jank-types "SurfaceFlinger CPU Deadline Missed,SurfaceFlinger GPU Deadline Missed,Display HAL,Prediction Error,SurfaceFlinger Scheduling,SurfaceFlinger Stuffing,Dropped Frame" \
  --port 9001
```

## 参数

- `--jank-types`: 逗号分隔的 SF Jank 类型列表（使用空格分隔格式，与 trace_processor 输出一致）
- `--port`: trace_processor 端口（默认 9001）

## 输出

输出到 `$RENDER_OUTPUT/sf_jank.json`：

- `sf_cpu` / `sf_gpu` / `display_hal` / `prediction_error` / `sf_scheduling` / `sf_stuffing` / `dropped`: 各类问题详情
- `issue_regions`: 精确的问题帧时间戳（用于截图定位）
