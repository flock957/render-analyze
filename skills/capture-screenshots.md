---
name: capture-screenshots
description: Phase 2 - Perfetto UI 竖屏长图（全局+细节）截图，覆盖完整渲染管线并输出截图复盘
type: skill
script: scripts/capture_screenshots.py
---

# Capture Screenshots

Headless Chromium 打开 Perfetto UI，自动 pin 完整渲染管线、zoom 并截图。

## 使用

```bash
python3 scripts/capture_screenshots.py \
  --trace /path/to/trace.perfetto-trace \
  --analysis-dir /path/to/output \
  [--output-dir /path/to/output/screenshots] \
  [--trace-processor /path/to/trace_processor]
```

## 前置条件
- Phase 1 的 JSON 输出（app_jank.json, target_process.json, thread_map.json）
- `playwright` + `chromium` 已安装（`pip install playwright && playwright install chromium`）
- `Pillow>=10.0`（用于 detail 图标注，`pip install Pillow`）
- *可选*：`trace_processor` 二进制在 `$PATH` 或通过 `--trace-processor` 指定。
  没有时自动 fallback 到 file upload 模式，功能等价、稍慢几秒。

## 关键设计

### 竖屏长图
- Viewport: `1072×1598`（竖屏长比例）
- `device_scale_factor=2.0` → 实际像素 `2144×3196`，提升文字与 slice 细节清晰度
- 自动裁剪：检测 pinned tracks 底部边界，裁掉空白区域

### Pin 策略（v4.0）

按渲染管线顺序 pin，**主线程 (pos 2) 和 RenderThread (pos 3) 始终紧跟 Timeline 之后**，hwuiTask0/1 与 GPU completion 动态发现后在 pos 4-5：

| 位置 | Track | 说明 |
|------|-------|------|
| 1 | Expected/Actual Timeline | 帧 jank 红绿标记 |
| 2 | App 主线程 | UI Thread，始终 |
| 3 | App RenderThread | 始终 |
| 4 | App hwuiTask0/1 | 动态发现 |
| 5 | App GPU completion | 动态发现 |
| 6 | SF 主线程 | surfaceflinger |
| 7 | SF RenderEngine | GPU 合成 |
| 8 | SF GPU completion | |
| 9 | SF binder | 最活跃的 |
| 10 | HWC/Composer | |
| 11 | CrtcCommit | 显示提交 |

### 两类截图（每个问题固定两张）

| 类型 | Zoom 范围 | CollapseAll | 额外操作 | 目的 |
|------|-----------|-------------|----------|------|
| 全局图 global | `setVisibleWindow(trace_start, trace_dur)` | 是（保证整洁） | — | 看整段 Trace 的全局分布与上下文 |
| 细节图 detail | `target_ts ± window` | 否 | App Deadline / Buffer Stuffing 时 `ExpandTracksByRegex(RenderThread)` | 点选证据 slice，打出故障点 |

### Pillow 标注（detail 图）

细节图在截图后由 Python Pillow 进行二次标注：
- **红色半透明高亮列**：覆盖 `target_ts` 对应的 x 坐标区域
- **顶部标题条**：显示 `问题类型 | 证据 slice 名 | 耗时`

标注位置基于 `target_ts` 相对于 visible window 的比例计算，**100% 确定性**，不依赖 Perfetto UI 坐标系。

## Perfetto API 使用

| 操作 | API |
|------|-----|
| 执行命令 | `app.commands.runCommand(cmdId, ...args)` |
| Pin tracks | `dev.perfetto.PinTracksByRegex` |
| Unpin all | `dev.perfetto.UnpinAllTracks` |
| Collapse | `dev.perfetto.CollapseAllGroups` |
| Expand | `dev.perfetto.ExpandAllGroups` / `ExpandTracksByRegex` |
| Sidebar | `dev.perfetto.ToggleLeftSidebar` |
| Drawer | `dev.perfetto.ToggleDrawer` |
| Zoom | `app._activeTrace.timeline.setVisibleWindow(HPTS(HPT(BigInt(ns)), dur_ns))` |

## 每个问题的截图流程

1. UnpinAll + CollapseAll + CloseDrawer + ClearSearch
2. ExpandTracksByRegex 展开目标进程组 + SF 进程组
3. Pin 渲染管线 tracks（来自 thread_map.json 的 pin_patterns，含 hwuiTask + GPU completion）
4. CollapseAll（pinned tracks 保持在顶部）
5. **全局图**: `setVisibleWindow(trace_start, trace_dur)`，关闭 drawer，截图
6. **细节图**: 收敛到 `target_ts ± window`；若 jank 类型为 App Deadline Missed 或 Buffer Stuffing，先 `ExpandTracksByRegex(RenderThread)` 展开 RT 子轨道；执行 `clickSliceAt(target_ts)` 点选证据 slice；截图后用 Pillow 标注

## 截图复盘（设计原则）

为什么 **每个问题固定两张图**？为什么 **细节图必须围绕 `target_ts` 收敛而不是帧中点**？

| 截图 | 解决的问题 | 凭什么这么截 |
|------|-----------|--------------|
| **全局图** | "这次 jank 是孤立事件还是全局抖动？前后还有别的红帧吗？" | 时间窗 = `(trace_start, trace_end)`，竖屏一次性把管线轨道画在同一根时间轴上，眼睛一扫就能看出抖动密度、是否成串、是否伴随调度异常 |
| **细节图** | "这一帧到底卡在哪个 slice？是 measure/draw、Binder、GC、commit、presentFence 还是 dequeueBuffer？" | 时间窗 = `target_ts ± max(2×dur, 80ms)`；`target_ts` 由 SQL 在帧时段内挑出"匹配关键词且耗时最长"的子 slice，**它本身就是嫌疑点**；再用 `page.mouse.click` 在对应 x 真实点击，让 Perfetto 把 slice 详情面板打开，截图就把"故障点证据"自然带进画面 |

### 为什么不是"帧中点 ± 50ms"
旧版 v3 用 `frame.ts + dur/2 ± 50ms` 作为细节窗口。问题：
- 单帧 jank 可能 100ms，子 slice 只有 8ms 集中在帧首 — 中点截图直接错过证据
- App Deadline Missed 这种复合型 jank，证据可能在 RenderThread 而非 main，中点截图 y 方向也对不齐

新版用 `target_ts`（来自关键词 SQL 的最长 slice）做时间锚，再用 `focus_track` 做 y 方向锚，**两个轴都对准故障点**。

### `focus_track` 的作用
- 由 jank 类型决定（`FOCUS_TRACK_BY_JANK_TYPE`）：
  - `App Deadline Missed` → `RenderThread`（并在 detail 图中 expand RT 子轨道）
  - `Buffer Stuffing` → `dequeueBuffer`（slice 名；并在 detail 图中 expand RT）
  - `SurfaceFlinger CPU Deadline Missed` → `surfaceflinger`
  - `Display HAL` → `presentFence`
  - `Prediction Error` → `Actual Timeline`
- 主要给截图复盘提供"为什么看这条轨道"的语义注释，写进报告

### 关键词集合（7 层渲染管线，~50 个 slice 名）

| Jank 类型 | 关键词 |
|-----------|--------|
| App Deadline Missed | doFrame, performTraversals, measure, layout, draw, DrawFrame, DrawFrames, syncFrameState, nSyncAndDrawFrame, renderFrameImpl, flush commands, Waiting for GPU, eglSwapBuffers, queueBuffer, Binder, GC, JIT |
| Buffer Stuffing | dequeueBuffer, queueBuffer, acquireBuffer, latchBuffer, DrawFrames, renderFrameImpl, flush commands, Waiting for GPU |
| SF CPU Deadline Missed | onMessageRefresh, commit, composite, RenderEngine, handleTransaction, handleComposition, postComposition, prepareFrame, setClientTarget, validateDisplay |
| Display HAL | presentFence, presentDisplay, composer, hwc, crtc_commit, waiting for presentFence, AtomicCommit, HWDeviceDRM |
| Prediction Error | Expected Timeline, Actual Timeline, VSync, VSyncPredictor, VSyncDispatch |
| SF Scheduling | surfaceflinger, onMessageRefresh, sched, MessageQueue::waitForMessage |

完整集合见 `scripts/analyze_jank.py` 顶部 `KEYWORDS_BY_JANK_TYPE`。

## 透传给报告的字段

`analyze_jank.py` 在 `app_jank.json` 的每个 top frame 上写入下列字段，`generate_report.py` 直接读取并渲染到"问题帧元数据"表 + "截图复盘说明" callout：

| 字段 | 含义 |
|------|------|
| `target_ts` | 故障锚点时刻（ns）— 帧内匹配关键词的最长 slice 起点 |
| `focus_track` | 该 jank 类型的焦点轨道名 |
| `evidence_slices` | 命中关键词的 Top-8 slice（含 name/thread/dur_ms/ts）；优先目标进程，<3 条时 fallback 全局 |
| `keywords_hit` | 命中关键词集合 |
| `region_range` | `{start_ts, end_ts, window_ms}` 检索窗口 |
| `problem_description` | 一段中文问题描述 |
| `screenshot_reasoning` | 一段中文截图复盘说明 |

`capture_screenshots.py` 同时把这些字段也写入 `screenshot_manifest.json`，便于排查截图与分析的对应关系。

## 依赖
- `playwright`（`pip install playwright && playwright install chromium`）
- `Pillow>=10.0`（`pip install Pillow`，用于 detail 图 Pillow 标注）
