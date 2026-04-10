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

### 8 步截图策略（v4.1 - 验证通过）

每个 jank 帧执行以下 8 步，产出全局图 + 细节图共 2 张：

```
步骤 1  Reset
        UnpinAll → CollapseAll
        清空上一帧的 pin 状态和展开状态

步骤 2  建立所有 track 的 DOM 节点
        ExpandAllGroups → CollapseAllGroups
        Perfetto 使用虚拟化滚动，屏幕外 track 无 DOM 节点；
        ExpandAll 强制把全部节点写入 DOM，CollapseAll 还原折叠状态

步骤 3  Pin 精简顶层管线
        PinTracksByRegex("surfaceflinger {tid}")
        PinTracksByRegex("composer-servic")
        【关键限制】PinTracksByRegex 只移动「顶层」track 到 pinned 区域；
        进程内部的 main thread / RenderThread 不是顶层 track，无法被 pin。
        因此只 pin 2 条可用的顶层 track（SF + HWC），保持 pinned 区域紧凑。

步骤 4  Expand 目标进程 + Frame Timeline 子组
        ExpandTracksByRegex("{进程名}")
        ExpandTracksByRegex("Expected Timeline|Actual Timeline")
        【关键发现】RenderThread 在进程的 "Frame Timeline" 子组内，
        而非普通线程列表；ExpandTracksByRegex 进程名只展开常规 50+ 线程，
        必须额外 Expand Frame Timeline 子组才能看到 RenderThread DrawFrames。

步骤 5  Collapse 噪声轨道
        CollapseTracksByRegex("CPU Scheduling|CPU Frequency|Ftrace|GPU|Scheduler|System|Kernel")
        减少画面干扰，让目标 track 占据更多垂直空间

步骤 6  隐藏 UI 面板
        ToggleLeftSidebar（隐藏左侧栏）
        ToggleDrawer（关闭底部 Current Selection 面板）
        CSS 注入隐藏 cookie/consent 横幅

步骤 7  全局图
        setVisibleWindow(trace_start, trace_end)
        omnibox 搜索 "DrawFrames"
        【目的】omnibox 会导航到 Frame Timeline 子组内的 RenderThread，
        解决虚拟化滚动导致目标 track 不在视口内的问题
        截图 → 保存当前 scrollTop（供细节图恢复）

步骤 8  细节图
        setVisibleWindow(target_ts ± max(2×dur, 80ms))
        【注意】zoom 操作会重置滚动位置，必须从步骤 7 保存的 scrollTop 还原
        restore scrollTop → page.mouse.click 在 target_ts 点选 slice
        截图后 Pillow 叠加标注
```

### Pin 策略说明（v4.1 vs v4.0 对比）

| 维度 | v4.0（旧） | v4.1（当前） |
|------|-----------|-------------|
| Pin 数量 | 最多 11 条（含 App main thread / RenderThread） | 仅 2 条（SF + HWC 顶层 track） |
| App 层显示方式 | 通过 pin 移到顶部 | 通过 Expand 展开进程组 |
| Frame Timeline 可见性 | ExpandTracksByRegex(进程名) | 额外 ExpandTracksByRegex("Expected Timeline\|Actual Timeline") |
| 原因 | 误以为 PinByRegex 能 pin 进程内部 track | PinByRegex 只对顶层 track 有效 |

### 两类截图（每个问题固定两张）

| 类型 | Zoom 范围 | 关键步骤 | 目的 |
|------|-----------|----------|------|
| **全局图 global** | `setVisibleWindow(trace_start, trace_end)` | omnibox 搜索 "DrawFrames" 导航到 RenderThread | 看整段 Trace 的全局分布与上下文 |
| **细节图 detail** | `target_ts ± max(2×dur, 80ms)` | 恢复全局图的 scrollTop → clickSliceAt(target_ts) | 点选证据 slice，打出故障点 |

### Pillow 标注（detail 图）

细节图在截图后由 Python Pillow 进行二次标注：
- **红色半透明高亮列**：覆盖 `target_ts` 对应的 x 坐标区域
- **顶部标题条**：显示 `问题类型 | 证据 slice 名 | 耗时`

标注位置基于 `target_ts` 相对于 visible window 的比例计算，**100% 确定性**，不依赖 Perfetto UI 坐标系。

### 7 层画面组成（从上到下）

| 层 | Track | 说明 |
|----|-------|------|
| Pinned-L5 | `surfaceflinger {tid}` | SF 主线程，顶层 track |
| Pinned-L7 | `composer-servic` | HWC 硬件合成器，顶层 track |
| Middle | `Expected Timeline` | 帧 jank 绿色/红色标记 |
| Middle | `Actual Timeline` | 实际帧完成时间 |
| Bottom-L1 | App 主线程 | UI Thread（doFrame / performTraversals） |
| Bottom-L2 | `RenderThread` | HWUI 渲染线程（DrawFrames / flush commands） |
| Bottom-L3 | GPU completion | GPU 完成信号 |

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

## 每个问题的截图流程（v4.1 - 8 步）

1. **Reset**: UnpinAll → CollapseAll
2. **建立节点**: ExpandAllGroups → CollapseAllGroups（解决虚拟化滚动 DOM 缺失）
3. **Pin 顶层 2 条**: `surfaceflinger {tid}` + `composer-servic`
4. **Expand**: 目标进程名（展开常规线程）+ `"Expected Timeline|Actual Timeline"`（展开 Frame Timeline 子组，使 RenderThread 可见）
5. **Collapse 噪声**: `CPU Scheduling|CPU Frequency|Ftrace|GPU|Scheduler|System|Kernel`
6. **隐藏 UI**: ToggleLeftSidebar + ToggleDrawer + CSS 注入隐藏 cookie 横幅
7. **全局图**: `setVisibleWindow(trace_start, trace_end)` → omnibox 搜索 "DrawFrames" → 截图 → 保存 scrollTop
8. **细节图**: `setVisibleWindow(target_ts ± window)` → 恢复 scrollTop → `clickSliceAt(target_ts)` → 截图 → Pillow 标注

> **为什么步骤 7 用 omnibox 搜索？** Perfetto 虚拟化滚动导致 RenderThread 在全局视图
> 下不在视口内，omnibox 搜索 "DrawFrames" 会自动导航到 Frame Timeline 子组中的
> RenderThread，无需手动滚动。
>
> **为什么步骤 8 要恢复 scrollTop？** `setVisibleWindow` 改变 zoom 后 Perfetto 会
> 重置滚动到顶部，必须手动还原步骤 7 保存的位置，否则细节图看不到目标 track。

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
