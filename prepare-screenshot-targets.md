---
name: prepare-screenshot-targets
type: knowledge
triggers:
  - 截图目标
  - screenshot targets
  - 截图准备
---

# 截图目标准备

通过 trace_processor SQL 查询为每个 jank 问题准备精确的截图参数。必须在截图之前、分析之后执行。

## 前置条件

- trace_processor 正在运行（Phase 1 启动，Phase 8 才停止）
- 分析已完成（Phase 5-6 产出 app_jank.json / sf_jank.json）

## 脚本

```bash
python3 scripts/prepare_screenshot_targets.py --port 9001 --output-dir /workspace/render_output --top-n 5
```

## 查询的数据

对每个 Top N jank 问题，查询以下信息：

| 查询项 | SQL 表 | 目的 |
|--------|--------|------|
| 故障 slice 详情 | `slice` + `thread` + `process` | 找到 doFrame/composite 等关键 slice 的精确时间戳 |
| 线程状态 | `thread_state` | Running/Sleeping/Blocked 时间分布 |
| 关联事件 | `slice` (binder/GC/lock) | 找到阻塞主线程的关联事件 |

## 输出

`screenshot_targets.json`，每个 target 包含：

- `interesting_start` / `interesting_dur`: 精确的截图时间窗口（基于实际 slice 位置）
- `description`: 从 SQL 数据生成的问题描述（如 "App帧超时: Choreographer#doFrame (24.7ms); 线程状态: RenderThread D=3.2ms"）
- `slices`: 关键 slice 列表
- `thread_states`: 线程状态分布
- `tracks_to_show`: 应该显示哪些 track

## 与截图脚本的关系

`capture_trace_screenshot.py` 读取 `screenshot_targets.json`，使用其中的精确参数而非盲猜。
