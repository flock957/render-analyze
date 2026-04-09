---
name: capture-screenshots
description: Phase 2 - Perfetto UI headless 长截图，覆盖完整渲染管线
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
- *可选*：`trace_processor` 二进制在 `$PATH` 或通过 `--trace-processor` 指定。
  没有时自动 fallback 到 file upload 模式，功能等价、稍慢几秒。

## 关键设计

### 长截图
- Viewport: 1920x2400（比默认 1080 高 2.2 倍）
- 更多 pinned tracks 可同时显示
- 自动裁剪：检测 pinned tracks 底部边界，裁掉空白区域

### Pin 策略
Pin 9 条渲染管线 track（顺序 = 从上到下）：
1. Expected/Actual Timeline — 帧 jank 红绿标记
2. App 主线程 — UI Thread
3. App RenderThread — 如果存在
4. SF 主线程 — surfaceflinger
5. SF RenderEngine — GPU 合成
6. SF GPU completion
7. SF binder — 最活跃的
8. HWC/Composer
9. CrtcCommit — 显示提交

### 两类截图
| 类型 | Zoom 范围 | 目的 |
|------|-----------|------|
| 概览图 | jank 帧 ±500ms（或 ±3×dur） | 看 jank 分布模式，前后帧对比 |
| 详情图 | jank 帧 ±50ms（或 ±0.5×dur） | Slice 文字可读，定位具体卡顿环节 |

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
3. Pin 9 条渲染管线 tracks（来自 thread_map.json 的 pin_patterns）
4. CollapseAll（pinned tracks 保持在顶部）
5. **概览图**: zoom ±500ms，关闭 drawer，隐藏非 pinned 内容，裁剪截图
6. **详情图**: zoom ±50ms，关闭 drawer，隐藏非 pinned 内容，裁剪截图

## 依赖
- `playwright`（`pip install playwright && playwright install chromium`）
