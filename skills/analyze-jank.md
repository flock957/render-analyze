---
name: analyze-jank
description: Phase 1 - SQL 分析 Perfetto trace 中的 jank 帧，构建完整渲染管线线程映射
type: skill
script: scripts/analyze_jank.py
---

# Analyze Jank

使用 Perfetto trace_processor SQL 分析 jank 帧。

## 使用

```bash
python3 scripts/analyze_jank.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /path/to/output
```

`python3` 必须能 `import perfetto.trace_processor` —— 见仓库根 README 的
venv 安装说明。

## 输出文件

| 文件 | 内容 |
|------|------|
| `target_process.json` | 目标进程（running time 最长的 app） |
| `app_jank.json` | Jank 帧统计 + top-5 帧（含证据增补） + 类型分布 |
| `sf_jank.json` | SurfaceFlinger 相关 jank |
| `jank_types.json` | 所有 jank 类型汇总 |
| `thread_map.json` | 完整渲染管线线程映射 + pin 模式（核心输出） |
| `tp_state.json` | Trace 时间范围等元数据 |

## 证据增补（v4.0 新增）

对 `app_jank.json` 的 `top_frames` 每一项，在 SQL 查询后自动增补以下字段：

| 字段 | 来源 | 说明 |
|------|------|------|
| `target_ts` | SQL: 帧时段内匹配关键词的最长 slice 起点 | 故障锚点时刻 |
| `focus_track` | `FOCUS_TRACK_BY_JANK_TYPE[jank_type]` | 焦点轨道 |
| `evidence_slices` | SQL: region 内匹配关键词 Top-8 slice | 含 name/thread/dur_ms/ts |
| `keywords_hit` | evidence 中实际命中的关键词集合 | 如 `["doFrame","DrawFrame"]` |
| `region_range` | `{start_ts, end_ts, window_ms}` | 检索窗口（帧 ± 2×dur, min 200ms） |
| `problem_description` | 模板拼接 | 中文问题描述 |
| `screenshot_reasoning` | 模板拼接 | 截图逻辑复盘说明 |

### 关键词集合

```python
KEYWORDS_BY_JANK_TYPE = {
    "App Deadline Missed": [
        "doFrame", "performTraversals", "DrawFrame", "DrawFrames",
        "renderFrameImpl", "flush commands", "Waiting for GPU",
        "syncFrameState", "nSyncAndDrawFrame", "eglSwapBuffers",
        "measure", "layout", "draw", "Binder", "GC", "JIT", "queueBuffer",
    ],
    "Buffer Stuffing": [
        "dequeueBuffer", "queueBuffer", "acquireBuffer", "latchBuffer",
        "DrawFrames", "renderFrameImpl", "flush commands", "Waiting for GPU",
    ],
    "SurfaceFlinger CPU Deadline Missed": [
        "onMessageRefresh", "commit", "composite", "RenderEngine",
        "handleTransaction", "handleComposition", "postComposition",
    ],
    "Display HAL": [
        "presentFence", "presentDisplay", "composer", "hwc",
        "crtc_commit", "waiting for presentFence",
    ],
    "Prediction Error": ["Expected Timeline", "Actual Timeline", "VSync"],
    "SurfaceFlinger Scheduling": ["surfaceflinger", "onMessageRefresh", "sched"],
}
```

这些全是**硬编码**字典查找，零 LLM 参与，同一 trace 产出完全相同结果。

## thread_map.json 结构（v3.0）

```json
{
  "target_process": "com.example.app",
  "target_pid": 12345,
  "app_main_thread": [{"name": "example.app", "tid": 12345}],
  "app_render_threads": [{"name": "RenderThread", "tid": 12346}],
  "sf_main_tid": 1388,
  "sf_pid": 1388,
  "sf_render_engine": [{"name": "RenderEngine", "tid": 1476}],
  "sf_gpu_completion": [{"name": "GPU completion", "tid": 2692}],
  "sf_binder_threads": [{"name": "binder:1388_4", "tid": 2625}],
  "hwc_threads": [{"name": "composer-servic", "tid": 1306}],
  "crtc_threads": [{"name": "crtc_commit:113", "tid": 808}],
  "pin_patterns": [
    "Expected Timeline", "Actual Timeline",
    "example.app 12345", "RenderThread 12346",
    "surfaceflinger 1388", "RenderEngine 1476",
    "GPU completion 2692", "binder:1388_4",
    "composer-servic", "crtc_commit:113"
  ]
}
```

## Pin 模式生成逻辑

按渲染管线顺序生成（顶部 → 底部）：
1. Expected/Actual Timeline — 帧 jank 红绿标记
2. App 主线程（tid = pid，排除 name=None 的重复行）
3. App RenderThread（精确 tid）
4. SF 主线程（tid = pid）
5. SF RenderEngine
6. SF GPU completion
7. SF binder（按 running time 排序取最活跃的 1 条）
8. HWC/Composer（取第 1 条）
9. CrtcCommit（取第 1 条）

## 依赖
- Python `perfetto` 模块（`pip install perfetto`，详见 README）
