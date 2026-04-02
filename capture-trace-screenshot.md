---
name: capture-trace-screenshot
type: knowledge
triggers:
  - 截图
  - screenshot
  - perfetto截图
  - trace截图
---

# Perfetto Trace 截图

从分析结果中提取 Top N 最严重的问题，在 Perfetto UI 中自动截图。每个问题截取一张图，聚焦到故障帧的时间范围和相关线程轨道。

## 脚本

```bash
python3 scripts/capture_trace_screenshot.py \
  --trace <trace文件路径> \
  --analysis-dir <分析输出目录> \
  --output-dir <截图输出目录> \
  --process-name <目标进程名> \
  --top-n 5
```

## 参数

- `--trace`: .perfetto-trace 文件路径
- `--analysis-dir`: 包含 app_jank.json / sf_jank.json / jank_types.json 的目录
- `--output-dir`: 截图 PNG 输出目录（默认 analysis-dir/screenshots）
- `--process-name`: 目标进程名（如 com.ss.android.ugc.aweme），支持自动检测
- `--top-n`: 截取 Top N 个问题（默认 5）
- `--force`: 跳过内存检查

## 截图策略

### Top N 筛选
1. 从 app_jank.json 和 sf_jank.json 的 `issue_regions` 中提取所有问题
2. 按 jank 类型去重（每类保留最严重的一个）
3. 按严重度 + 耗时排序，取 Top N

### 时间定位
- 使用 `app.trace.scrollTo()` API 精确导航到故障帧时间范围
- padding = max(帧耗时, 5ms)，保证帧在上下文中可见

### 线程轨道导航
根据 jank 类型自动滚动到不同的线程区域：

| Jank 类型 | 搜索的线程 | 说明 |
|-----------|-----------|------|
| App Deadline Missed | RenderThread, Choreographer, 主进程 | 应用渲染线程 |
| Buffer Stuffing | RenderThread, 主进程 | 应用 + buffer 状态 |
| Display HAL | SurfaceFlinger, HWC, VSYNC | 显示硬件层 |
| SF CPU/GPU | SurfaceFlinger, Binder | SF 合成线程 |
| SF Stuffing | SurfaceFlinger, HWC | SF 帧堆积 |
| Prediction Error | SurfaceFlinger, VSYNC | VSync 预测 |
| Dropped Frame | RenderThread, Choreographer | 帧丢弃上下文 |

### Perfetto 搜索导航
使用 Perfetto omnibox 搜索相关 slice 名称（如 `Choreographer#doFrame`、`onMessageRefresh`、`dequeueBuffer`），让 UI 自动滚动到对应进程轨道。

## 依赖

- **playwright**: Python 浏览器自动化
- **Chromium**: 无头浏览器（通过 playwright 或系统安装的 google-chrome）
- **网络**: 需要访问 https://ui.perfetto.dev 加载 Perfetto UI

如果 playwright/chromium 不可用，脚本会优雅降级（输出 `skipped_reason`），不影响后续流程。

## 输出

- `screenshots/00_<问题名>.png` ... `04_<问题名>.png`: Top N 截图
- `screenshots/screenshot_manifest.json`: 截图清单

```json
{
  "trace_file": "xxx.perfetto-trace",
  "total_issues": 24,
  "captured": 5,
  "skipped": 0,
  "screenshots": [
    {"name": "App Jank Frame #2254", "file": "00_App Jank Frame #2254.png", "success": true}
  ]
}
```

## 后续优化方向

- 实现 Perfetto UI track pin（将关键线程固定到顶部）
- 支持本地 Perfetto UI（避免网络依赖）
- 截图中标注故障帧的时间范围和耗时
