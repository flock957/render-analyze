---
name: render-performance-workflow
type: repo
version: 2.0.0
agent: CodeActAgent
phases:
  - key: setup
    label: 环境初始化
    desc: 安装依赖（trace_processor、playwright、chromium）
    output: null
  - key: init
    label: Trace 初始化
    desc: 加载 trace 并启动查询服务
    output: /workspace/render_output/tp_state.json
  - key: target
    label: 查找进程
    desc: 确定分析目标进程
    output: /workspace/render_output/target_process.json
  - key: jank-metric
    label: Jank 指标初始化
    desc: 运行 android_jank_cuj_init
    output: /workspace/render_output/jank_metric_init.json
  - key: jank-types
    label: Jank 类型识别
    desc: 统计各类 Jank 分布
    output: /workspace/render_output/jank_types.json
  - key: app-jank
    label: 应用层 Jank 分析
    desc: APP_DEADLINE_MISSED / BUFFER_STUFFING
    output: /workspace/render_output/app_jank.json
  - key: sf-jank
    label: SF Jank 分析
    desc: SF CPU/GPU/HAL/调度/丢帧
    output: /workspace/render_output/sf_jank.json
  - key: screenshot
    label: 截图（可选）
    desc: 捕获 Perfetto UI 问题片段截图
    output: /workspace/render_output/screenshots/screenshot_manifest.json
  - key: cleanup
    label: 清理
    desc: 停止 trace_processor
    output: null
  - key: report
    label: 生成报告
    desc: HTML 渲染性能报告（含截图和 Framework 分析）
    output: /workspace/render_output/render_report.html
---

# Android 应用绘制渲染性能分析工作流

按以下 10 个阶段顺序执行 Perfetto trace 渲染性能分析。脚本位于 `scripts/` 目录，输出保存到 `/workspace/render_output/`。

## 严格约束

1. **禁止自行编写 SQL 查询或分析代码** — 只能调用指定脚本
2. **禁止修改已有脚本**
3. **参数必须来自上一步输出** — 不要猜测
4. **遇到脚本错误立即停止并报告**

## 每步反思验证

每个脚本执行后必须检查：
- 输出 JSON 是否包含预期字段（`has_issue`, `severity`, `jank_types` 等）
- 数值是否在合理范围内
- 如果输出异常，停止并报告

## 执行步骤

| 阶段 | 脚本 | 说明 |
|------|------|------|
| 0. 环境初始化 | `setup_env.py` | 自动安装 trace_processor、playwright、chromium |
| 1. 初始化 | `trace_processor_init.py --trace <路径> --port 9001` | 加载 trace |
| 2. 查找进程 | `find_foreground_process.py --port 9001` | 确定目标进程 |
| 3. Jank 指标 | `init_render_jank_metric.py --port 9001` | 初始化分析表 |
| 4. 类型识别 | `analyze_jank_types.py --port 9001` | Jank 类型分布 |
| 5. 应用层分析 | `analyze_app_jank.py --jank-types <类型> --port 9001` | App 层 Jank |
| 6. SF 分析 | `analyze_sf_jank.py --jank-types <类型> --port 9001` | SF 层 Jank |
| 7. 截图（可选） | `capture_trace_screenshot.py --trace <路径> ...` | Perfetto 截图 |
| 8. 清理 | `trace_processor_cleanup.py` | 停止 trace_processor |
| 9. 报告 | `render_report_generator.py` | 生成 HTML 报告 |

## 阶段详情

### 阶段0: 环境初始化

```bash
python3 scripts/setup_env.py
```

自动安装所有依赖：
- **requests**: trace_processor HTTP 查询
- **playwright + Chromium**: Perfetto UI 无头浏览器截图
- **trace_processor_shell**: Perfetto SQL 查询引擎

**验证：** 输出 JSON 中 `all_ready: true`。
**注意：** 如果 Chromium 安装失败，分析流程仍可正常运行，只是报告中不含截图。

### 阶段1: Trace 初始化

```bash
python3 scripts/trace_processor_init.py \
  --trace <用户提供的trace文件路径> --port 9001
```

**验证：** 输出包含 `"status": "ready"`。

### 阶段2: 查找进程

```bash
python3 scripts/find_foreground_process.py --port 9001
```

确定前台目标进程。
**验证：** 输出包含 `"process_name"`。
**记录：** `PROCESS_NAME` ← 输出的进程名，后续截图阶段使用。

### 阶段3: Jank 指标初始化

```bash
python3 scripts/init_render_jank_metric.py --port 9001
```

运行 `RUN_METRIC('android/jank/android_jank_cuj_init.sql')` 初始化 Jank 分析表。
**验证：** 输出 `"metric_initialized": true`。

### 阶段4: Jank 类型识别

```bash
python3 scripts/analyze_jank_types.py --port 9001
```

统计所有 jank_type 分布。输出每种 Jank 类型的帧数和平均耗时。
**验证：** `jank_types` 数组非空（如果为空说明 trace 中无 Jank）。
**记录：** `JANK_TYPES` ← 输出中出现的 jank_type 列表

### 阶段5: 应用层 Jank 分析

```bash
python3 scripts/analyze_app_jank.py \
  --jank-types "AppDeadlineMissed,BufferStuffing" --port 9001
```

**仅当 JANK_TYPES（阶段4输出）中包含 `AppDeadlineMissed` 或 `BufferStuffing` 时才执行。**
如果不包含，跳过此步骤。

分析内容：
- JANK_APP_DEADLINE_MISSED: doFrame 超时、DrawFrames 时长、GPU wait
- JANK_BUFFER_STUFFING: dequeueBuffer 阻塞、buffer queue 状态

### 阶段6: SurfaceFlinger Jank 分析

```bash
python3 scripts/analyze_sf_jank.py \
  --jank-types "SurfaceFlingerCpuDeadlineMissed,SurfaceFlingerGpuDeadlineMissed,DisplayHal,PredictionError,SurfaceFlingerScheduling,SurfaceFlingerStuffing,DroppedFrame" \
  --port 9001
```

**仅当 JANK_TYPES 中包含对应 SF 类型时才执行。**

分析内容：
- SF_CPU_DEADLINE_MISSED: SF 主线程耗时、锁竞争、Layer 更新量
- SF_GPU_DEADLINE_MISSED: GPU 合成耗时、Layer 数量
- DISPLAY_HAL: HWC/present 事件
- PREDICTION_ERROR: VSync 预测漂移
- SF_SCHEDULING: SF 调度异常
- SF_STUFFING: 前一帧耗时关联
- DROPPED: 帧丢弃

### 阶段7: 截图（可选）

```bash
python3 scripts/capture_trace_screenshot.py \
  --trace $TRACE_FILE \
  --analysis-dir /workspace/render_output \
  --output-dir /workspace/render_output/screenshots \
  --process-name "$PROCESS_NAME" \
  --top-n 5
```

自动截取 Top 5 最严重问题的 Perfetto UI 截图：
- App 类问题截图聚焦 RenderThread / 主线程轨道
- SF 类问题截图聚焦 SurfaceFlinger / HWC 轨道
- 时间范围精确到故障帧 ± padding

**重要：此步骤为可选。** 如果输出中 `skipped_reason` 不为空，跳过截图继续下一步。不要因为截图失败而停止工作流。

### 阶段8: 清理

```bash
python3 scripts/trace_processor_cleanup.py \
  --output-dir /workspace/render_output
```

### 阶段9: 生成报告

```bash
python3 scripts/render_report_generator.py \
  --output-dir /workspace/render_output \
  --top-n 5
```

生成 HTML 渲染性能报告，包含：
- 概览统计（总帧数、Jank 率、类型分布）
- Top 5 重点问题分析（每个问题含截图 + Android Framework 源码级根因分析）
- 调用链路、源码文件引用、优化建议

## 完成后

向用户汇总：
1. Jank 类型分布概览
2. 最严重的 Top 5 卡顿问题 + 根因
3. Android Framework 层面的优化建议
4. 报告文件位于 `/workspace/render_output/render_report.html`
