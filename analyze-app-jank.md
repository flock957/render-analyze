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

## 脚本

python3 /workspace/custom/skill_examples/render_skills/scripts/analyze_app_jank.py --jank-types "AppDeadlineMissed,BufferStuffing" --port 9001

## 参数

- --jank-types: 逗号分隔的 Jank 类型列表（来自 analyze_jank_types 的输出）
- --port: trace_processor 端口

## 输出字段

- app_deadline_missed: doFrame/DrawFrames/GPU wait 分析结果
- buffer_stuffing: dequeueBuffer/buffer queue 分析结果
- issue_regions: 精确的问题帧时间戳（用于截图）
