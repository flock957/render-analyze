---
name: analyze-jank-types
type: knowledge
triggers:
  - jank类型
  - jank distribution
---

# Jank 类型识别

统计 trace 中所有 Jank 类型的分布，包括帧数、平均耗时和严重程度。

## 前置条件

- trace_processor 已启动（Phase 1）
- Jank 指标已初始化（Phase 3）
- 环境变量 `RENDER_OUTPUT` 已设置（run_analysis.py 自动处理）

## 脚本

```bash
python3 scripts/analyze_jank_types.py --port 9001
```

## 参数

- `--port`: trace_processor 端口（默认 9001）

## 输出

输出到 `$RENDER_OUTPUT/jank_types.json`：

- `total_frames`: 总帧数
- `jank_frame_count`: Jank 帧数
- `jank_rate_pct`: Jank 率百分比
- `detected_types`: 检测到的 Jank 类型列表（空格分隔格式，如 "App Deadline Missed"）
- `jank_types`: 每种类型的详细统计（帧数、平均耗时、严重度）
