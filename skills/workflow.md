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
- 每个问题输出 **2 张竖屏长图**（全局图 + 细节图）
- 输出: `screenshots/` 目录 + `screenshot_manifest.json`

#### 8 步截图策略（每个 jank 帧执行一次）

```
1. Reset:         UnpinAll → CollapseAll
2. 建立节点:      ExpandAllGroups → CollapseAllGroups
                  （使所有 track 节点进入 DOM，解决虚拟化滚动导致节点缺失的问题）
3. Pin 精简管线:  surfaceflinger {tid} + composer-servic
                  （仅 pin 2 条顶层 track，保持 pinned 区域紧凑）
4. Expand 目标:   ExpandTracksByRegex(进程名) — 展开目标进程组
                  ExpandTracksByRegex("Expected Timeline|Actual Timeline") — 展开 Frame Timeline 子组
5. Collapse 噪声: CollapseTracksByRegex("CPU Scheduling|CPU Frequency|Ftrace|GPU|Scheduler|System|Kernel")
6. Force-hide UI: ToggleLeftSidebar（隐藏侧栏）+ ToggleDrawer（隐藏 Current Selection 面板）
                  + CSS 注入隐藏 cookie/cookie-consent 横幅
7. 全局图:        setVisibleWindow(trace_start, trace_end) → omnibox 搜索 "DrawFrames"
                  （导航到 Frame Timeline 子组内的 RenderThread，避免虚拟化滚动导致轨道不可见）
                  → 全视口截图，保存当前滚动位置
8. 细节图:        setVisibleWindow(target_ts ± window) → 从全局图恢复滚动位置
                  （zoom 会重置滚动，必须从全局步骤保存的位置还原）
                  → page.mouse.click 在 target_ts 点选 slice → Pillow 叠加标注
```

#### 关键发现（可复现性说明）

| 发现 | 影响 | 解决方案 |
|------|------|----------|
| `PinTracksByRegex` 只移动**顶层** track 到 pinned 区域 | 进程内部的 main thread / RenderThread 无法通过 pin 显示在顶部 | 改为 pin `surfaceflinger {tid}` + `composer-servic` 2 条顶层 track；App 层通过 Expand 展开 |
| RenderThread 在进程的 **Frame Timeline 子组**内，不在普通线程列表里 | `ExpandTracksByRegex(进程名)` 只展开常规 50+ 线程，看不到 RenderThread | 额外 `ExpandTracksByRegex("Expected Timeline\|Actual Timeline")` 展开 Frame Timeline 子组 |
| Perfetto 使用**虚拟化滚动** — 屏幕外 track 无 DOM 节点 | 直接 pin 进程内部 track 时找不到节点 | `ExpandAllGroups → CollapseAllGroups` 强制建立所有 track 的 DOM 节点 |
| omnibox 搜索 "DrawFrames" 导航到 Frame Timeline 内的 RenderThread | 可以跳转到正确的 y 位置 | 全局图步骤用 omnibox 搜索代替手动滚动 |
| zoom 操作会重置滚动位置 | detail 图 zoom 后滚动位置丢失 | 全局图截图后保存 `scrollTop`，detail 步骤 zoom 后还原 |

#### 7 层画面组成（从上到下）

| 层 | Track | 说明 |
|----|-------|------|
| Pinned-L5 | `surfaceflinger {tid}` | SF 主线程，顶层 track |
| Pinned-L7 | `composer-servic` | HWC 硬件合成器，顶层 track |
| Middle | `Expected Timeline` | 帧 jank 绿色/红色标记 |
| Middle | `Actual Timeline` | 实际帧完成标记 |
| Bottom-L1 | App 主线程 | UI Thread（doFrame / performTraversals） |
| Bottom-L2 | `RenderThread` | HWUI 渲染线程（DrawFrames / flush commands） |
| Bottom-L3 | GPU completion | GPU 完成信号 |

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

## Pin 策略（v4.1 - 精简顶层 Pin + Expand Frame Timeline）

`PinTracksByRegex` **只能把顶层 track 移到 pinned 区域**，进程内部线程（main thread / RenderThread）无法通过 pin 显示在顶部。因此当前策略改为：只 pin 2 条顶层 track，App 层 track 通过 Expand 展开可见。

| 步骤 | 操作 | 说明 |
|------|------|------|
| Pin L5 | `surfaceflinger {tid}` | SF 主线程，顶层 track，可被 pin |
| Pin L7 | `composer-servic` | HWC 硬件合成器，顶层 track，可被 pin |
| Expand | 目标进程名 regex | 展开目标 App 进程组（常规线程） |
| Expand | `Expected Timeline\|Actual Timeline` | 展开 Frame Timeline 子组（含 RenderThread DrawFrames） |
| Collapse | CPU/GPU/Ftrace/Kernel 等噪声 track | 减少画面干扰 |

App 层的 RenderThread 在 **Frame Timeline 子组**内（非常规线程列表），必须单独 ExpandTracksByRegex("Expected Timeline|Actual Timeline") 才能展开。

> **注意：** `thread_map.json` 的 `pin_patterns` 字段仍保留完整管线（11 条）用于报告元数据，
> 实际截图只 pin 2 条（SF + HWC）。
