---
name: render-jank-workflow
description: 完整渲染 Jank 分析工作流 - 从 trace 分析到竖屏长图截图报告（v4.0 portrait-longshot）
type: workflow
version: 4.0
---

# 渲染 Jank 分析工作流 (v4.0 portrait-longshot)

## 概述
自动化分析 Android 渲染 jank 问题的完整流水线。**全程无 LLM 参与**，
`python3 scripts/run_workflow.py` 一条命令跑完 3 个 Phase，任何人 clone 后
拿到的报告结构、根因分析、截图逻辑完全一致。

## 前置条件

已激活的 venv（或全局环境）已安装 `perfetto>=0.16.0`、`playwright>=1.57.0`
和 `Pillow>=10.0`（用于 detail 截图标注），并且 `playwright install chromium`
已执行过。详见仓库根 README 的 Quick start 段。

```bash
pip install -r requirements.txt && playwright install chromium
```

## 一键执行

```bash
# 完整流程
python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /path/to/output

# 只分析不截图（快速）
python3 scripts/run_workflow.py \
  --trace /path/to/trace --output-dir /out --no-screenshots
```

## 阶段说明

### Phase 1: Analyze Jank (`analyze_jank.py`)
- SQL 查询 jank 帧统计
- **识别目标进程**：按 **jank 帧数** 排名取最多的 App 进程（若进程名为 NULL 则以主线程名代替，避免 NULL 导致错误识别）
- 按 jank 类型分组，每类取最严重的帧，选 top-5
- **为每个 top frame 增补证据字段**：
  - `target_ts` — 帧内匹配关键词的最长 slice 起点（故障锚点）
  - `focus_track` — jank 类型决定的焦点轨道名
  - `evidence_slices` — Top-8 命中关键词 slice（name/thread/dur_ms/ts）；SQL 先在目标进程内查，结果 <3 条时自动 fallback 到全局查询
  - `keywords_hit` — 实际命中的关键词集合
  - `region_range` — 检索窗口 `{start_ts, end_ts, window_ms}`
  - `problem_description` — 中文问题总结（问题类型+帧号+目标时刻+证据）
  - `screenshot_reasoning` — 截图逻辑复盘说明
- 构建完整渲染管线的 thread_map（覆盖 App → SF → HWC → CrtcCommit），包含 app_hwui_threads
- 关键词覆盖 7 层渲染管线（~50 个 slice 名），见 Pin 策略 / KEYWORDS_BY_JANK_TYPE
- 输出: `target_process.json`, `app_jank.json`, `sf_jank.json`, `jank_types.json`, `thread_map.json`, `tp_state.json`

### Phase 2: Capture Screenshots (`capture_screenshots.py`)
- Headless Chromium 打开 ui.perfetto.dev 加载 trace
- **竖屏长图**：viewport `1072×1598` + `device_scale_factor=2.0`
  → 实际像素 `2144×3196`，slice 文字清晰可读
- Pin 完整渲染管线 track（最多 11 条），详见 Pin 策略
- 全局图使用 `CollapseAll` 保证画面整洁；细节图在 App Deadline Missed / Buffer Stuffing 类型时额外 `ExpandTracksByRegex(RenderThread)` 以展开 RT 子轨道
- 每个问题输出 **2 张竖屏长图**：
  - **全局图 (global)**: `setVisibleWindow(trace_start, trace_end)` — 完整 trace 时间窗
  - **局部细节图 (detail)**: `target_ts ± max(2×dur, 80ms)` 收敛时间窗 +
    `page.mouse.click` 在 target_ts 位置点选 slice 打出证据
- **detail 截图叠加 Pillow 标注**：在 target_ts 对应 x 区域画红色半透明高亮列 +
  顶部标题条（问题类型 + 证据 slice 名 + 耗时）
- 画面组成（从上到下）：pinned tracks → collapsed 进程组 → Ftrace Events 证据面板
- 输出: `screenshots/` 目录 + `screenshot_manifest.json`

### Phase 3: Generate Report (`generate_report.py`)
- 生成 HTML 报告，内嵌 base64 截图
- 概览统计 + jank 类型分布
- **Top-5 问题详情**，每个问题包含：
  1. **问题帧元数据表**：问题类型、帧号、捷区范围、目标时刻、焦点轨道、命中关键词、问题描述、截图逻辑
  2. **证据 slices 表**：Top-5 slice 名+线程+耗时+起点
  3. **全局图 + 标注细节图**
  4. **截图复盘说明** callout
  5. **Framework 根因分析**：调用链+源码引用+Trace 诊断指南+根因+优化建议
- 根因分析来自 `FRAMEWORK_KB` 硬编码（6 种 jank 类型），**不依赖 LLM**；4 类已深度扩展：
  - **App Deadline Missed**：15 步调用链（完整 Skia/HWUI），+3 source_refs（CanvasContext.cpp / EglManager.cpp / ShaderCache.cpp），+6 trace_guide，+4 root_causes，+3 optimizations
  - **SF CPU Deadline Missed**：10 步调用链
  - **Display HAL**：9 步调用链（HWDeviceDRM/AtomicCommit）
  - **Buffer Stuffing**：7 步调用链（DrawFrames → presentFence）
- 输出: `render_report.html`

## 可复现性保证

| 组件 | 确定性 | 说明 |
|------|--------|------|
| Phase 1 SQL 分析 | **100% 确定** | 同一 trace 产出完全相同 JSON |
| Phase 2 截图 | **95%+** | 依赖 Perfetto UI 渲染，不同 chromium/perfetto 版本可能有微小像素差异 |
| Phase 3 报告 | **100% 确定** | 读 JSON + 拼 HTML，无随机性 |
| Framework 根因分析 | **100% 确定** | `FRAMEWORK_KB` 硬编码，不调 LLM |
| 标注框位置 | **100% 确定** | 基于 target_ts 与 visible window 比例计算 |

## Perfetto API 参考

通过诊断脚本确认的正确 API（2026-04-07）：

| 功能 | 命令/API | 备注 |
|------|----------|------|
| Pin tracks | `app.commands.runCommand('dev.perfetto.PinTracksByRegex', regex)` | 132 个注册命令 |
| Unpin all | `dev.perfetto.UnpinAllTracks` | |
| Collapse | `dev.perfetto.CollapseAllGroups` | 全局截图后必须调用 |
| Expand | `dev.perfetto.ExpandAllGroups` / `ExpandTracksByRegex` | detail 图按需展开 RT |
| Sidebar toggle | `dev.perfetto.ToggleLeftSidebar` | |
| Drawer toggle | `dev.perfetto.ToggleDrawer` | 关闭底部 Found Events 面板 |
| Zoom | `app._activeTrace.timeline.setVisibleWindow(HPTS(HPT(BigInt), dur))` | HPT/HPTS 从 visibleWindow 获取构造函数 |

## Pin 策略（v4.0 - 完整渲染管线）

按渲染管线分层 pin，顺序从上到下。**主线程 (pos 2) 和 RenderThread (pos 3) 始终紧跟 Timeline 之后被 pin**，hwuiTask0/1 和 GPU completion 动态发现后 pin 在 pos 4-5。

| 层级 | Pin 模式示例 | 说明 |
|------|-------------|------|
| 1. Frame Timeline | `Expected Timeline` / `Actual Timeline` | 帧 jank 红绿指示器 |
| 2. App 主线程 | `droid.ugc.aweme 10269` | UI Thread；始终第 2 位 |
| 3. App RenderThread | `RenderThread {tid}` | 精确 tid；始终第 3 位 |
| 4-5. hwuiTask / GPU | `hwuiTask0 {tid}` / `GPU completion {tid}` | 动态发现并 pin |
| 6. SF 主线程 | `surfaceflinger 1388` | tid = pid |
| 7. SF RenderEngine | `RenderEngine {tid}` | GPU 合成引擎 |
| 8. SF GPU completion | `GPU completion {tid}` | SF 侧 GPU 完成信号 |
| 9. SF binder | `binder:1388_4` | 最活跃的 binder 线程 |
| 10. HWC/Composer | `composer-servic` 或 `HWC_UeventThrea` | 硬件合成器 |
| 11. CrtcCommit | `crtc_commit:113` | 显示提交内核线程 |

所有 pin_patterns 由 `analyze_jank.py` 的 `thread_map.json` 自动生成。
