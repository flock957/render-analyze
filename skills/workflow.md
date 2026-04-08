---
name: render-jank-workflow
description: 完整渲染 Jank 分析工作流 - 从 trace 分析到截图报告
type: workflow
version: 3.0
---

# 渲染 Jank 分析工作流

## 概述
自动化分析 Android 渲染 jank 问题的完整流水线。

## 一键执行

```bash
# 完整流程（需要用 miniforge3 的 python，已安装 perfetto + playwright）
/home/wq/miniforge3/bin/python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /path/to/output

# 只分析不截图（快速）
/home/wq/miniforge3/bin/python3 scripts/run_workflow.py \
  --trace /path/to/trace --output-dir /out --no-screenshots
```

## 阶段说明

### Phase 1: Analyze Jank (`analyze_jank.py`)
- SQL 查询 jank 帧统计
- 识别目标进程（running time 最长的 app）
- 按 jank 类型分组，每类取最严重的帧，选 top-5
- 构建完整渲染管线的 thread_map（覆盖 App → SF → HWC → CrtcCommit）
- 输出: `target_process.json`, `app_jank.json`, `sf_jank.json`, `jank_types.json`, `thread_map.json`, `tp_state.json`

### Phase 2: Capture Screenshots (`capture_screenshots.py`)
- Headless Chromium（viewport 1920x2400）打开 ui.perfetto.dev 加载 trace
- Pin 完整渲染管线 track（9 条）：
  - Expected/Actual Timeline（帧 jank 红绿标记）
  - App 主线程 + RenderThread
  - SF 主线程 + RenderEngine + GPU completion
  - SF binder（最活跃的）
  - HWC/Composer
  - CrtcCommit
- 每个问题输出 2 张截图：
  - 概览图（±500ms 宽上下文，看 jank 分布模式）
  - 详情图（紧缩 zoom，slice 文字可读）
- 自动裁剪：检测 pinned tracks 边界，裁掉底部空白
- 输出: `screenshots/` 目录 + `screenshot_manifest.json`

### Phase 3: Generate Report (`generate_report.py`)
- 生成 HTML 报告，内嵌 base64 截图
- 包含：概览统计、jank 类型分布、top-5 问题分析
- 每个问题附带：截图对比 + Framework 调用链 + 源码分析 + 诊断指南 + 根因 + 优化建议
- 输出: `render_report.html`

## Perfetto API 参考

通过诊断脚本确认的正确 API（2026-04-07）：

| 功能 | 命令/API | 备注 |
|------|----------|------|
| Pin tracks | `app.commands.runCommand('dev.perfetto.PinTracksByRegex', regex)` | 132 个注册命令 |
| Unpin all | `dev.perfetto.UnpinAllTracks` | |
| Collapse | `dev.perfetto.CollapseAllGroups` | |
| Expand | `dev.perfetto.ExpandAllGroups` / `ExpandTracksByRegex` | |
| Sidebar toggle | `dev.perfetto.ToggleLeftSidebar` | |
| Drawer toggle | `dev.perfetto.ToggleDrawer` | 关闭底部 Found Events 面板 |
| Zoom | `app._activeTrace.timeline.setVisibleWindow(HPTS(HPT(BigInt), dur))` | HPT/HPTS 从 visibleWindow 获取构造函数 |

## Pin 策略（v3.0 - 完整渲染管线）

按渲染管线分层 pin，顺序从上到下：

| 层级 | Pin 模式示例 | 说明 |
|------|-------------|------|
| 1. Frame Timeline | `Expected Timeline` / `Actual Timeline` | 帧 jank 红绿指示器 |
| 2. App 主线程 | `droid.ugc.aweme 10269` | UI Thread |
| 3. App RenderThread | `RenderThread {tid}` | 精确 tid，仅 pin 目标 App 的 |
| 4. SF 主线程 | `surfaceflinger 1388` | tid = pid |
| 5. SF RenderEngine | `RenderEngine {tid}` | GPU 合成引擎 |
| 6. SF GPU completion | `GPU completion {tid}` | GPU 完成信号 |
| 7. SF binder | `binder:1388_4` | 最活跃的 binder 线程 |
| 8. HWC/Composer | `composer-servic` 或 `HWC_UeventThrea` | 硬件合成器 |
| 9. CrtcCommit | `crtc_commit:113` | 显示提交内核线程 |

所有 pin_patterns 由 `analyze_jank.py` 的 `thread_map.json` 自动生成。
