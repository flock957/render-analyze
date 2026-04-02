# Android 渲染性能分析知识库

## 参考资料

### 官方文档
- [Perfetto FrameTimeline](https://perfetto.dev/docs/data-sources/frametimeline) — FrameTimeline 数据源说明，jank 分类定义
- [Perfetto UI 文档](https://perfetto.dev/docs/visualization/perfetto-ui) — UI 操作、命令面板、Track 管理
- [Perfetto Deep Link](https://perfetto.dev/docs/visualization/deep-linking-to-perfetto-ui) — URL 参数、startupCommands、自动化控制
- [Android Graphics Architecture](https://source.android.com/docs/core/graphics) — SurfaceFlinger、BufferQueue、HWComposer 架构

### 社区实践
- [cnblogs - 性能优化trace分析实战](https://www.cnblogs.com/yangykaifa/p/19343145) — Perfetto 分析实战流程
- [掘金 - Perfetto 渲染性能分析](https://juejin.cn/post/7543421087759400970) — Android 渲染 jank 分析方法
- [技术栈 - Perfetto FrameTimeline 详解](https://jishuzhan.net/article/1981250637624049666) — FrameTimeline jank 类型详解
- [TodoAndroid - Perfetto 延迟和卡顿指南](https://zh-cn.todoandroid.es/) — 完整的延迟/卡顿诊断指南

---

## Android 渲染管线

### 帧渲染流程（一帧的生命周期）

```
VSYNC-app 信号到达
    │
    ▼
┌─────────────────────────── App 主线程 ───────────────────────────┐
│ Choreographer.doFrame()                                          │
│   ├── INPUT callbacks     (处理触摸/按键事件)                     │
│   ├── ANIMATION callbacks (ValueAnimator/ObjectAnimator)          │
│   └── TRAVERSAL callback                                         │
│        └── ViewRootImpl.performTraversals()                       │
│             ├── performMeasure()  → 递归测量 View 树              │
│             ├── performLayout()   → 递归布局 View 树              │
│             └── performDraw()     → 构建 DisplayList              │
│                  └── ThreadedRenderer.draw()                      │
│                       └── syncFrameState → 同步到 RenderThread    │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────── RenderThread ──────────────────────────┐
│ nSyncAndDrawFrame()                                               │
│   ├── issueDrawCommands → 提交 GPU 绘制指令 (OpenGL/Vulkan)       │
│   └── queueBuffer → 将渲染完成的 buffer 放入 BufferQueue          │
│        └── Surface.dequeueBuffer() → 获取下一个空 buffer           │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────── BufferQueue ────────────────────────────┐
│ Triple Buffering: 3 个 buffer slot                                │
│   ├── App produces → queueBuffer()                                │
│   └── SF consumes  → acquireBuffer()                              │
│ 如果所有 buffer 都被占用 → dequeueBuffer() 阻塞 (Buffer Stuffing) │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼ VSYNC-sf 信号到达
┌──────────────────────── SurfaceFlinger ───────────────────────────┐
│ onMessageRefresh()                                                │
│   ├── commit()                                                    │
│   │    ├── handleTransaction() → 处理 Surface 状态变更            │
│   │    └── latchBuffer()       → 从 BufferQueue 获取最新 buffer   │
│   ├── composite()                                                 │
│   │    ├── HWC 合成 (overlay)  → 高效、无 GPU 开销                │
│   │    └── GPU 合成 (fallback) → RenderEngine 绘制、GPU fence     │
│   └── presentDisplay()                                            │
│        └── HWComposer.present() → 提交给 Display HAL              │
│             └── 返回 presentFence                                  │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────── Display HAL ─────────────────────────────┐
│ HWC HAL: presentDisplay()                                         │
│   ├── DRM/KMS → Display Controller → Panel                       │
│   └── presentFence signal → 帧已经上屏                            │
│ 如果 fence 延迟 > 1 VSYNC → Display HAL Jank                     │
└──────────────────────────────────────────────────────────────────┘
```

### VSYNC 机制

- **VSYNC-app**: 驱动 App 开始渲染下一帧（Choreographer 响应）
- **VSYNC-sf**: 驱动 SurfaceFlinger 开始合成/呈现
- **Phase Offset**: VSYNC-app 和 VSYNC-sf 之间的时间偏移，让 App 先渲染完再由 SF 合成
- **VSyncPredictor**: 线性回归模型预测下一个 VSYNC 时间，刷新率切换时可能预测错误

---

## FrameTimeline Jank 分类

### 帧颜色编码（Actual Timeline）

| 颜色 | 含义 |
|------|------|
| 绿色 | 正常帧，无 jank |
| 浅绿色 | 高延迟帧（帧率平滑但呈现延迟，增加输入延迟） |
| 红色 | Jank 帧，由该进程导致 |
| 黄色 | Jank 帧，由 SurfaceFlinger 导致（App 按时完成） |
| 蓝色 | Dropped Frame，帧被丢弃 |

### Jank 类型详解

#### 1. App Deadline Missed（应用侧超时）

**定义**: 帧总耗时 = doFrame开始 → max(GPU完成, queueBuffer时间) > VSYNC 间隔

**子分类（基于 Trace 分析）**:
| 子类型 | Trace 特征 | 根因 |
|--------|-----------|------|
| Measure/Layout 超时 | `performTraversals` 内 measure/layout > 8ms | View 层级深、RelativeLayout 嵌套 |
| Draw 超时 | `performDraw` > 8ms | Canvas 绘制指令过多、Bitmap 过大 |
| Input 阻塞 | INPUT callback 占比 > 50% | 触摸事件处理有同步操作 |
| Animation 超时 | ANIMATION callback > 8ms | 复杂属性动画/过渡动画 |
| Sync 超时 | `syncFrameState` > 4ms | DisplayList 过大、RenderThread 忙 |
| 主线程阻塞 | 主线程 Running 时间占比低 | Binder/GC/I/O/锁竞争 |

#### 2. Buffer Stuffing（BufferQueue 塞满）

**定义**: App 渲染的帧按时完成（on_time_finish=true），但因 BufferQueue 满而呈现延迟

**关键 Trace 特征**:
- RenderThread `dequeueBuffer` slice > 5ms（正常 < 1ms）
- Actual Timeline 帧为浅绿色（Late Present）
- 通常伴随 Display HAL / SF Stuffing

#### 3. Display HAL（显示硬件延迟）

**定义**: SF 按时完成合成，但 HWC/Display 未能在目标 VSYNC 前呈现帧

**关键 Trace 特征**:
- SF 进程 `waiting for presentFence NNN` slice > 16ms（正常 < 1ms）
- SF commit/composite 耗时正常（< 5ms）
- SF Actual Timeline 帧为红色

#### 4. SF CPU Deadline Missed（SF 主线程超时）

**定义**: SF 主线程的 commit+composite 总耗时 > VSYNC 间隔

**关键 Trace 特征**:
- SF `onMessageRefresh` 或 `commit` + `composite` 总时长 > 16ms
- Layer 数量大（> 20）
- 可能伴随锁竞争（`lock`/`mutex` slice）

#### 5. SF GPU Deadline Missed（SF GPU 合成超时）

**定义**: SF CPU 部分按时完成，但 GPU 合成 fence 未在 deadline 前 signal

**关键 Trace 特征**:
- SF 的 `RenderEngine` / `GLES` / `drawLayers` slice 时间长
- GPU completion track fence signal 延迟
- `validateDisplay` 显示多个 Layer 走 CLIENT (GPU) 合成

#### 6. Prediction Error（VSync 预测错误）

**定义**: VSyncPredictor 预测的下一个 VSYNC 时间与实际偏差超过阈值

**关键 Trace 特征**:
- Expected Timeline 和 Actual Timeline 时间窗口偏差大
- 通常在刷新率切换时出现
- Actual Timeline 帧为浅绿色

#### 7. SF Stuffing（SF 帧堆积）

**定义**: SF 上一帧还未完成，新帧被迫排队

**关键 Trace 特征**:
- SF Actual Timeline 连续多帧 > 1 VSYNC 间隔
- 通常是 Display HAL 或 SF CPU 的级联效应

#### 8. Dropped Frame（帧丢弃）

**定义**: 帧错过目标 VSYNC 且有更新帧可用，被 SF 丢弃

**关键 Trace 特征**:
- Actual Timeline 蓝色帧
- 通常 App doFrame > 2 VSYNC 间隔
- 最严重的 jank 类型

---

## Perfetto UI 操作指南

### 关键快捷键

| 快捷键 | 功能 |
|--------|------|
| W / S | 放大 / 缩小 |
| A / D | 左移 / 右移 |
| F | 聚焦选中事件 |
| Q | 切换底部面板 |
| Ctrl+P | Track 搜索 |
| Ctrl+Shift+P | 命令面板 |
| / | 搜索 slice |
| . / , | 跳到相邻 slice |

### Pin Track 命令 API

```javascript
// 通过命令 API pin track（自动化可用）
app.commands.runCommand('dev.perfetto.PinTracksByRegex', 'RenderThread');
app.commands.runCommand('dev.perfetto.PinTracksByRegex', 'surfaceflinger');
app.commands.runCommand('dev.perfetto.ExpandTracksByRegex', 'com\\.example\\.app');

// 通过 URL 参数（加载 trace 时自动执行）
// startupCommands=[{"id":"dev.perfetto.PinTracksByRegex","args":["RenderThread"]}]
```

### 时间导航 API

```javascript
// 跳转到指定时间范围
app.trace.scrollTo({
    time: { start: 1234567890n, end: 1234567900n, behavior: 'focus' }
});
```

---

## 诊断工作流

### 标准分析流程

1. **概览**: 打开 Actual Timeline，查看帧颜色分布，定位红色/蓝色帧
2. **定位**: 选中 jank 帧，查看 flow arrows 连接到 SF DisplayFrame
3. **App 侧**: 展开 App 主线程，检查 doFrame 内部各阶段耗时
4. **RenderThread**: 检查 GPU 工作时长、dequeueBuffer 阻塞
5. **SF 侧**: 检查 SF commit/composite/presentFence 耗时
6. **系统**: 检查 CPU 调度（线程状态）、thermal（频率）

### 常用 SQL 查询

```sql
-- 查找所有 jank 帧
SELECT * FROM android_jank_cuj
WHERE jank_type != 'None'
ORDER BY dur DESC;

-- 检查 doFrame 超时
SELECT ts, dur, dur/1000000.0 AS ms
FROM slice WHERE name = 'Choreographer#doFrame'
AND dur > 16600000
ORDER BY dur DESC LIMIT 20;

-- 检查 presentFence 等待
SELECT ts, dur, name, dur/1000000.0 AS ms
FROM slice WHERE name LIKE 'waiting for presentFence%'
AND dur > 5000000
ORDER BY dur DESC LIMIT 20;

-- 检查线程状态分布
SELECT state, SUM(dur)/1000000.0 AS total_ms
FROM thread_state
WHERE utid IN (SELECT utid FROM thread WHERE name = 'main')
GROUP BY state ORDER BY total_ms DESC;
```

---

## AOSP 源码索引

| 组件 | 关键文件 | 关键函数 |
|------|---------|---------|
| Choreographer | `frameworks/base/core/java/android/view/Choreographer.java` | `doFrame()`, `doCallbacks()` |
| ViewRootImpl | `frameworks/base/core/java/android/view/ViewRootImpl.java` | `performTraversals()`, `performMeasure/Layout/Draw()` |
| ThreadedRenderer | `frameworks/base/core/java/android/view/ThreadedRenderer.java` | `draw()`, `syncFrameState()` |
| SurfaceFlinger | `frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp` | `onMessageRefresh()`, `commit()`, `composite()` |
| HWComposer | `frameworks/native/services/surfaceflinger/DisplayHardware/HWComposer.cpp` | `presentAndGetReleaseFences()`, `validateDisplay()` |
| BufferQueue | `frameworks/native/libs/gui/BufferQueueProducer.cpp` | `dequeueBuffer()`, `queueBuffer()` |
| FrameTimeline | `frameworks/native/services/surfaceflinger/FrameTimeline/FrameTimeline.cpp` | `classifyJankLocked()` |
| VSyncPredictor | `frameworks/native/services/surfaceflinger/Scheduler/VSyncPredictor.cpp` | `nextAnticipatedVSyncTimeFrom()` |
| RenderEngine | `frameworks/native/libs/renderengine/` | `drawLayers()` |
| CompositionEngine | `frameworks/native/services/surfaceflinger/CompositionEngine/` | `present()` |
