---
name: analyze-app-jank
type: knowledge
triggers:
  - app jank
  - 应用层卡顿
  - doFrame
  - buffer stuffing
---

# 应用层 Jank 分析

分析应用层的两类 Jank：APP_DEADLINE_MISSED（doFrame 超时、DrawFrames、GPU wait）和 BUFFER_STUFFING（dequeueBuffer 阻塞、buffer queue 溢出）。

## 前置条件

- trace_processor 已启动（Phase 1）
- Jank 指标已初始化（Phase 3）
- 环境变量 `RENDER_OUTPUT` 已设置（run_analysis.py 自动处理）

## 脚本

```bash
python3 scripts/analyze_app_jank.py \
  --jank-types "App Deadline Missed,Buffer Stuffing" \
  --port 9001
```

## 参数

- `--jank-types`: 逗号分隔的 Jank 类型列表（使用空格分隔格式，与 trace_processor 输出一致）
- `--port`: trace_processor 端口（默认 9001）

## 输出

输出到 `$RENDER_OUTPUT/app_jank.json`：

- `has_issue`: 是否存在问题
- `severity`: 严重程度（high/medium/low/normal）
- `app_deadline_missed`: doFrame/DrawFrames/GPU wait 分析结果
- `buffer_stuffing`: dequeueBuffer/buffer queue 分析结果
- `issue_regions`: 精确的问题帧时间戳（用于截图定位）
