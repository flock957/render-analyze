#!/usr/bin/env python3
"""Phase 3: Generate HTML report matching v3fix style.

Dark theme, framework analysis with call chains, source code refs,
trace diagnosis guides, root causes, and optimization suggestions.
"""
import argparse
import base64
import json
from datetime import datetime
from pathlib import Path

# ─── Framework knowledge base per jank type ───────────────────────────

FRAMEWORK_KB = {
    "App Deadline Missed": {
        "cn_name": "应用侧超时",
        "call_chain": [
            "VSYNC-app 信号到达",
            "Choreographer.doFrame()",
            "  → INPUT callbacks (处理触摸/按键事件)",
            "  → ANIMATION callbacks (属性动画/过渡动画)",
            "  → TRAVERSAL: ViewRootImpl.performTraversals()",
            "    → performMeasure() → performLayout() → performDraw()",
            "  → ThreadedRenderer.draw() → syncFrameState",
            "RenderThread: nSyncAndDrawFrame → issueDrawCommands",
            "RenderThread: queueBuffer → 提交给 SurfaceFlinger",
        ],
        "source_refs": [
            {
                "file": "Choreographer.java",
                "path": "frameworks/base/core/java/android/view/Choreographer.java",
                "desc": "doFrame() 接收 VSYNC-app 信号后依次分发 INPUT → ANIMATION → TRAVERSAL 回调。帧起点 = doFrame 开始，终点 = max(GPU完成时间, queueBuffer时间)。如果总时间超过 VSYNC 间隔 (16.6ms@60Hz / 11.1ms@90Hz)，标记为 JANK_APP_DEADLINE_MISSED。",
            },
            {
                "file": "ViewRootImpl.java",
                "path": "frameworks/base/core/java/android/view/ViewRootImpl.java",
                "desc": "performTraversals() 是帧渲染主入口，依次执行 measure → layout → draw。Trace 中看 'performTraversals' slice 内部哪个阶段耗时最长即为瓶颈。常见: measure/layout 慢 → View 层级问题; draw 慢 → Canvas 绘制过重。",
            },
            {
                "file": "ThreadedRenderer.java",
                "path": "frameworks/base/core/java/android/view/ThreadedRenderer.java",
                "desc": "draw() 将 DisplayList 同步到 RenderThread (syncFrameState)，然后 RenderThread 执行 nSyncAndDrawFrame 提交 GPU 指令。Trace 中看 'syncFrameState' 耗时 → 主线程和 RenderThread 的同步开销。",
            },
        ],
        "trace_guide": [
            "在 Perfetto 中定位 Actual Timeline 的红色帧，查看对应的 `Choreographer#doFrame` slice",
            "展开 doFrame 内部: 检查 input/animation/traversal 各阶段耗时占比",
            "检查 `performTraversals` 内 measure vs layout vs draw 哪个最长",
            "检查 RenderThread 的 `DrawFrame` / `syncFrameState` 耗时",
            "检查主线程是否有 `Binder.transact`、`GC`、`JIT compiling` 等阻塞 slice",
            "检查线程状态: Running (绿色) vs Sleeping (蓝色) vs Runnable (白色) vs Uninterruptible (橙色)",
        ],
        "root_causes": [
            "**Measure/Layout 过重**: View 层级深、RelativeLayout 嵌套、RecyclerView 多类型 item",
            "**Draw 过重**: Canvas.drawBitmap/drawPath 指令多, 自定义 View onDraw 复杂",
            "**Input/Animation 回调耗时**: 触摸事件处理或动画计算占用了大部分帧时间",
            "**主线程 I/O 阻塞**: SharedPreferences.commit()、数据库查询、文件读写",
            "**主线程 Binder 调用**: 同步 IPC 等待远端进程响应 (ContentProvider/Service)",
            "**GC / JIT**: 运行时垃圾回收暂停，JIT 编译暂停",
            "**锁竞争**: synchronized/ReentrantLock 等待其他线程释放锁",
        ],
        "optimizations": [
            "使用 `ConstraintLayout` 减少嵌套层级，避免 `RelativeLayout` 嵌套导致双 measure",
            "RecyclerView: `setHasFixedSize(true)` + DiffUtil + 预创建 ViewHolder",
            "将耗时 Bitmap 解码移到子线程，使用 Glide/Coil 异步加载",
            "主线程 I/O: SharedPreferences.commit() → apply(), 数据库操作移到子线程",
            "使用 `ViewPropertyAnimator` 或 `RenderThread` 动画替代主线程动画",
            "减少 `Canvas.saveLayer()` 调用（触发 offscreen buffer 分配）",
            "使用 Systrace/Perfetto 标记 `Trace.beginSection()` 定位业务代码瓶颈",
        ],
    },
    "Display HAL": {
        "cn_name": "显示 HAL 延迟",
        "call_chain": [
            "SurfaceFlinger.onMessageRefresh()",
            "  → commit() → composite() → presentDisplay()",
            "HWComposer.presentAndGetReleaseFences()",
            "HWC HAL: presentDisplay() → 提交帧到显示控制器",
            "Kernel: DRM/KMS → Display Controller → Panel",
            "返回 presentFence → SF 在下一帧等待此 fence",
        ],
        "source_refs": [
            {
                "file": "HWComposer.cpp",
                "path": "frameworks/native/services/surfaceflinger/DisplayHardware/HWComposer.cpp",
                "desc": "presentAndGetReleaseFences() 调用 HWC HAL 的 presentDisplay()，HAL 返回一个 presentFence。SF 在下一帧开始时等待这个 fence 信号。Trace 中关键 slice: 'waiting for presentFence NNN'。",
            },
            {
                "file": "SurfaceFlinger.cpp",
                "path": "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
                "desc": "postComposition() 中检查 presentFence。正常 presentFence 等待 < 1ms。如果持续 > 16ms，说明显示硬件未能在一个 VSYNC 内完成帧呈现。",
            },
        ],
        "trace_guide": [
            "在 SF 进程中搜索 'waiting for presentFence' slice，检查耗时（正常 < 1ms）",
            "检查 SF Actual Timeline 帧颜色: 红色 = SF 导致的 jank",
            "检查 'HWC::presentDisplay' 或 'hwc_commit' slice 耗时",
            "检查 SF commit/composite 总耗时是否正常（正常 < 5ms）",
            "如果 commit/composite 正常但 presentFence 慢 → 硬件问题",
            "如果 commit/composite 也慢 → 可能是 Layer 太多导致 HWC 回退 GPU",
        ],
        "root_causes": [
            "**HWC overlay 回退**: Layer 类型/数量超出 HWC 能力，回退到 GPU 合成",
            "**DDR 带宽竞争**: 显示控制器读 framebuffer 与 CPU/GPU 内存访问竞争",
            "**Panel 刷新率切换**: 60→90→120Hz 切换导致 VSYNC 间隔不稳定",
            "**Thermal 降频**: 高温导致 GPU/Display 频率降低，帧呈现变慢",
            "**HWC 驱动 bug**: 厂商 HWC HAL 实现问题（常见于低端机/旧驱动）",
        ],
        "optimizations": [
            "检查 HWC 合成方式: `dumpsys SurfaceFlinger --comp-type` 确认是否 GPU 回退",
            "减少 overlay layer 数量，确保关键 layer 走 HWC 硬件合成",
            "检查 DDR 频率: `cat /sys/class/devfreq/*/cur_freq`",
            "排查 thermal: `dumpsys thermalservice` 看是否触发降频",
            "联系硬件厂商确认 HWC 驱动是否有已知问题",
        ],
    },
    "SurfaceFlinger CPU Deadline Missed": {
        "cn_name": "SF 合成超时",
        "call_chain": [
            "VSYNC-sf 信号到达",
            "SurfaceFlinger.onMessageRefresh()",
            "  → handleTransaction(): 处理 App 提交的 Surface 状态变更",
            "  → handleComposition(): 计算 Layer 可见区域/混合模式/变换矩阵",
            "  → composite(): GPU 或 HWC 合成",
            "  → postComposition(): fence 管理、帧统计",
        ],
        "source_refs": [
            {
                "file": "SurfaceFlinger.cpp",
                "path": "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
                "desc": "onMessageRefresh() 是 SF 的主帧循环，对应 VSYNC-sf 信号。当总处理时间超过 VSYNC 间隔时标记为 SF_CPU_DEADLINE_MISSED。Trace 中看 'onMessageRefresh' 或 'commit' + 'composite' slice 总时长。",
            },
            {
                "file": "CompositionEngine.cpp",
                "path": "frameworks/native/services/surfaceflinger/CompositionEngine/",
                "desc": "handleComposition 计算每个 Layer 的可见区域、混合模式。Layer 数量是核心因子 — 每多一个 Layer 约增加 0.1-0.5ms。Trace 中检查 'composite layers' 或 'RenderEngine' 相关 slice。",
            },
        ],
        "trace_guide": [
            "在 SF 进程找 'onMessageRefresh' / 'commit' / 'composite' slice",
            "检查 'handleTransaction' 耗时 — Layer 状态变更处理",
            "检查 'composite layers' 耗时 — 合成计算",
            "检查 SF 线程状态: 是否有 Runnable (排队等 CPU) 或 Uninterruptible (等 I/O)",
            "检查 SF Binder 线程的 'setTransactionState' — 频繁事务导致锁竞争",
            "Layer 数量: `dumpsys SurfaceFlinger --list` 查看当前 Layer 列表",
        ],
        "root_causes": [
            "**Layer 数量过多**: App 大量独立 Surface (多窗口/画中画/浮窗/SurfaceView)",
            "**锁竞争**: SF 主线程等待 mStateLock，被 Binder 线程 setTransactionState 阻塞",
            "**CPU 调度**: SF 线程被高优先级中断或 RT 任务抢占，处于 Runnable 状态",
            "**GPU 合成回退**: HWC 无法合成某些 Layer，回退到 GPU (RenderEngine) 合成",
        ],
        "optimizations": [
            "减少 Layer: 合并不必要的 SurfaceView，使用 TextureView 替代",
            "检查 SF 调度: `ps -eT -o pid,tid,cls,rtprio,comm | grep surfaceflinger`",
            "减少 Binder 事务频率（减少 setTransactionState 调用）",
            "检查 GPU 合成: `dumpsys SurfaceFlinger --comp-type` 看是否有 CLIENT (GPU) 合成",
        ],
    },
    "Buffer Stuffing": {
        "cn_name": "Buffer 塞满",
        "call_chain": [
            "App RenderThread: nSyncAndDrawFrame → issueDrawCommands",
            "App RenderThread: Surface.queueBuffer() → 提交 buffer 给 BufferQueue",
            "App RenderThread: Surface.dequeueBuffer() → 尝试获取下一个空 buffer",
            "  → BufferQueueProducer.dequeueBuffer() 阻塞（所有 slot 被占）",
            "SF: onMessageRefresh → acquireBuffer() → latchBuffer() → 消费 buffer",
        ],
        "source_refs": [
            {
                "file": "BufferQueueProducer.cpp",
                "path": "frameworks/native/libs/gui/BufferQueueProducer.cpp",
                "desc": "dequeueBuffer() 在 BufferQueue 所有 slot 被占用时阻塞。Triple buffering (3 buffers) 下，如果 SF 合成慢导致前两帧还没消费，第三帧 dequeue 就会阻塞 App 的 RenderThread。Trace 中表现为 'dequeueBuffer' slice 持续 > 5ms。",
            },
            {
                "file": "BufferLayerConsumer.cpp",
                "path": "frameworks/native/libs/gui/BufferLayerConsumer.cpp",
                "desc": "SurfaceFlinger 在 onMessageRefresh 中调用 acquireBuffer 消费 buffer。如果 SF 侧合成延迟，消费速度跟不上生产速度。",
            },
        ],
        "trace_guide": [
            "检查 RenderThread 的 `dequeueBuffer` slice 耗时（正常 < 1ms，阻塞时 > 5ms）",
            "检查 Actual Timeline: 帧是否标记为 'Late Present' 但 on_time_finish=true",
            "检查 SF Actual Timeline 是否有对应的延迟",
            "检查 SF 的 `onMessageRefresh` / `commit` / `composite` 总耗时",
            "通常与 Display HAL / SF Stuffing 同时出现，需要联合分析",
        ],
        "root_causes": [
            "**SurfaceFlinger 消费慢**: SF 合成时间长，buffer 消费速度低于生产速度",
            "**Display HAL 级联**: presentFence 延迟 → buffer 无法释放 → dequeueBuffer 阻塞",
            "**App 连续快速渲染**: fling/动画场景下 App 渲染速度 > SF 消费速度",
            "**GPU 合成回退**: HWC 无法处理某些 Layer，回退到 GPU 合成导致 SF 耗时增加",
        ],
        "optimizations": [
            "优先排查 Display HAL / SF 侧延迟 — Buffer Stuffing 通常是下游问题的级联",
            "减少 Layer 数量降低 SF 合成时间",
            "检查 `dumpsys SurfaceFlinger --comp-type` 确认是否有 GPU 合成回退",
            "如果 App 侧无 deadline missed，问题主要在 SF/Display 侧",
        ],
    },
    "Prediction Error": {
        "cn_name": "VSync 预测偏差",
        "call_chain": [
            "VSyncPredictor.nextAnticipatedVSyncTimeFrom()",
            "  → 线性回归模型预测下一个 VSYNC 时间",
            "FrameTimeline: 比较 expectedVsync vs actualPresent",
            "  → 偏差超过阈值 → 标记 PredictionError",
        ],
        "source_refs": [
            {
                "file": "VSyncPredictor.cpp",
                "path": "frameworks/native/services/surfaceflinger/Scheduler/VSyncPredictor.cpp",
                "desc": "VSyncPredictor 使用线性回归模型基于历史 VSYNC 时间戳预测下一个 VSYNC。当实际 present 时间与预测偏差超过 half-VSYNC 时，标记为 PredictionError。模型需要几帧来适应刷新率变化。",
            },
            {
                "file": "Scheduler.cpp",
                "path": "frameworks/native/services/surfaceflinger/Scheduler/Scheduler.cpp",
                "desc": "Scheduler 管理 VSYNC-app 和 VSYNC-sf 的 phase offset。当刷新率切换时，phase offset 需要重新计算，过渡期容易出现预测错误。",
            },
        ],
        "trace_guide": [
            "检查 Expected Timeline vs Actual Timeline: 预期时间窗口和实际时间是否偏差大",
            "检查是否有刷新率切换事件 (60→90→120Hz)",
            "检查 VSYNC 信号间隔是否稳定",
            "PredictionError 帧通常在 Actual Timeline 显示为浅绿色",
            "通常是系统级问题，App 侧无法直接修复",
        ],
        "root_causes": [
            "**刷新率切换**: 60↔90↔120Hz 变化导致 VSYNC 间隔突变，预测模型来不及适应",
            "**不规则帧提交**: App 帧提交间隔不均匀，VSYNC phase offset 不准",
            "**Thermal 降频**: GPU/Display 频率变化影响 VSYNC 节奏",
            "**多进程干扰**: 多个 App 同时渲染导致 VSYNC 调度混乱",
        ],
        "optimizations": [
            "检查刷新率: `dumpsys SurfaceFlinger | grep 'active mode'`",
            "锁定刷新率避免频繁切换: `Surface.setFrameRate()`",
            "PredictionError 通常是系统级问题，App 侧可通过稳定帧率间接改善",
        ],
    },
    "SurfaceFlinger Scheduling": {
        "cn_name": "SF 调度延迟",
        "call_chain": [
            "VSYNC-sf 信号到达",
            "SF 主线程处于 Runnable 状态（等待 CPU 调度）",
            "CPU 调度器将 SF 线程调度到 CPU 上",
            "SurfaceFlinger.onMessageRefresh() 延迟开始",
        ],
        "source_refs": [
            {
                "file": "SurfaceFlinger.cpp",
                "path": "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
                "desc": "SF 收到 VSYNC-sf 后等待被调度执行。如果 CPU 负载高或 SF 线程优先级被抢占，onMessageRefresh 开始时间会晚于 VSYNC 信号。",
            },
        ],
        "trace_guide": [
            "检查 SF 主线程在 VSYNC-sf 后的线程状态",
            "Runnable（白色）时间长 → CPU 调度延迟",
            "检查同一 CPU 上是否有高优先级任务抢占",
            "检查 CPU 频率是否处于低频状态",
        ],
        "root_causes": [
            "**CPU 负载高**: 其他进程占用 CPU 导致 SF 调度延迟",
            "**SF 线程未绑定大核**: SF 跑在小核上导致性能不足",
            "**RT 任务抢占**: 实时优先级任务抢占 SF 的 CPU 时间",
        ],
        "optimizations": [
            "检查 SF 线程 CPU 亲和性: `taskset -p <sf_pid>`",
            "确保 SF 线程运行在大核上",
            "减少系统整体 CPU 负载",
        ],
    },
}


def _find_kb(jank_type):
    """Find matching knowledge base entry for a jank type (may be composite)."""
    # Try exact match first
    if jank_type in FRAMEWORK_KB:
        return FRAMEWORK_KB[jank_type]
    # Try matching the first component of composite types
    for key in FRAMEWORK_KB:
        if key in jank_type:
            return FRAMEWORK_KB[key]
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    analysis = Path(args.analysis_dir)
    output = Path(args.output)

    print(f"[Phase 3] Generating report...")

    app_jank = _load(analysis / "app_jank.json")
    target = _load(analysis / "target_process.json")
    tp_state = _load(analysis / "tp_state.json")
    thread_map = _load(analysis / "thread_map.json")

    screenshots_dir = analysis / "screenshots"
    manifest = None
    if (screenshots_dir / "screenshot_manifest.json").exists():
        manifest = _load(screenshots_dir / "screenshot_manifest.json")

    top_frames = app_jank.get("top_frames", [])[:5]
    total = app_jank.get("total_frames", 0)
    jank_n = app_jank.get("jank_frames", 0)
    jank_rate = app_jank.get("jank_rate", 0)
    severity = app_jank.get("severity", "unknown")
    type_summary = app_jank.get("jank_type_summary", {})
    type_details = app_jank.get("jank_type_details", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = _CSS_HEADER.format(
        process=target['process_name'],
        time=now,
        total=total,
        jank_n=jank_n,
        jank_rate=f"{jank_rate*100:.1f}",
        type_count=len(type_summary),
    )

    # Jank type distribution table
    html += '<div class="card"><h3>Jank 类型分布 (Top)</h3><table>\n'
    html += '<tr><th>类型</th><th>帧数</th><th>平均耗时</th><th>严重程度</th></tr>\n'
    for jt, cnt in sorted(type_summary.items(), key=lambda x: -x[1]):
        detail = type_details.get(jt, {})
        avg = detail.get("avg_dur_ms", 0)
        kb = _find_kb(jt)
        cn = kb["cn_name"] if kb else jt
        sev_color = "#ff4444" if avg > 30 else "#ffaa00" if avg > 10 else "#3fb950"
        sev_text = "严重" if avg > 30 else "中等" if avg > 10 else "轻微"
        html += f'<tr><td>{cn} <code style="color:#484f58;font-size:11px">{jt}</code></td>'
        html += f'<td>{cnt}</td><td>{avg:.1f} ms</td>'
        html += f'<td><span class="badge" style="background:{sev_color}">{sev_text}</span></td></tr>\n'
    html += '</table></div>\n'

    # Top 5 issues
    html += '<h2>Top 5 重点问题分析</h2>\n'

    for i, frame in enumerate(top_frames):
        jt = frame["jank_type"]
        dur = frame["actual_dur_ms"]
        detail = type_details.get(jt, {})
        affected = detail.get("count", 0)
        max_dur = detail.get("max_dur_ms", dur)
        sev_color = "#ff4444" if dur > 30 else "#ffaa00"
        sev_text = "严重" if dur > 30 else "中等"
        kb = _find_kb(jt)
        cn = kb["cn_name"] if kb else jt

        html += '<div class="card">\n<div class="card-header">\n'
        html += f'    <h3><span class="issue-num">{i+1}</span>{cn} ({jt}) '
        html += f'<span class="badge" style="background:{sev_color}">{sev_text}</span></h3>\n'
        html += f'    <span>{affected} 帧受影响 | 最长 {max_dur:.1f}ms</span>\n'
        html += '</div>\n'

        # Top frames table
        top3 = detail.get("top_frames", [frame])[:3]
        if top3:
            html += '<h4>Top 问题帧</h4><table>\n'
            html += '<tr><th>Frame ID</th><th>耗时</th><th>类型</th></tr>\n'
            for f in top3:
                html += f'<tr><td>#{f["id"]}</td><td>{f["actual_dur_ms"]:.1f}ms</td><td>{f["jank_type"]}</td></tr>\n'
            html += '</table>\n'

        # Screenshots — supports both legacy (overview/detail) and new grouped format
        if manifest and i < len(manifest.get("screenshots", [])):
            ss = manifest["screenshots"][i]

            # New grouped format: ss["screenshots"] dict with keys like overview, app, sf, hal
            if "screenshots" in ss and isinstance(ss["screenshots"], dict):
                group_labels = {
                    "overview": "概览图（宽上下文）",
                    "app": "App + Frame Timeline 层",
                    "sf": "SurfaceFlinger 渲染管线",
                    "hal": "Display HAL + 内核层",
                }
                # Render in fixed order: overview first, then app, sf, hal
                for key in ["overview", "app", "sf", "hal"]:
                    if key not in ss["screenshots"]:
                        continue
                    fname = ss["screenshots"][key]
                    img_path = screenshots_dir / fname
                    if img_path.exists():
                        b64 = base64.b64encode(img_path.read_bytes()).decode()
                        label = group_labels.get(key, key)
                        html += f'''<div class="screenshot">
    <img src="data:image/png;base64,{b64}" alt="{fname}"
         onclick="this.classList.toggle('expanded')"
         title="点击查看大图 / Click to enlarge" />
    <p class="screenshot-label">Perfetto 截图: {label} - {fname}</p>
</div>\n'''
            else:
                # Legacy format: overview + detail keys at top level
                for key, label in [("overview", "概览图"), ("detail", "详情图")]:
                    if key not in ss:
                        continue
                    img_path = screenshots_dir / ss[key]
                    if img_path.exists():
                        b64 = base64.b64encode(img_path.read_bytes()).decode()
                        html += f'''<div class="screenshot">
    <img src="data:image/png;base64,{b64}" alt="{ss[key]}"
         onclick="this.classList.toggle('expanded')"
         title="点击查看大图 / Click to enlarge" />
    <p class="screenshot-label">Perfetto 截图: {label} - {ss[key]}</p>
</div>\n'''

        # Framework analysis
        if kb:
            html += '<div class="framework-analysis"><h4>Android Framework 根因分析</h4>\n'

            # Call chain
            html += '<div class="call-chain"><h5>调用链路</h5><div class="chain">\n'
            for j, step in enumerate(kb["call_chain"]):
                if j > 0:
                    html += '<span class="chain-arrow">→</span>'
                html += f'<span class="chain-step">{step}</span>'
            html += '</div></div>\n'

            # Source refs
            html += '<div class="source-refs"><h5>源码分析</h5>\n'
            for ref in kb["source_refs"]:
                html += f'''<div class="source-ref">
    <div class="source-file"><code>{ref["file"]}</code>
        <span class="source-path">{ref["path"]}</span>
    </div>
    <p>{ref["desc"]}</p>
</div>\n'''
            html += '</div>\n'

            # Trace diagnosis
            html += '<div class="trace-diagnosis"><h5>Perfetto Trace 诊断指南</h5><ul>\n'
            for tip in kb["trace_guide"]:
                html += f'<li>{tip}</li>\n'
            html += '</ul></div>\n'

            # Root causes
            html += '<div class="root-causes"><h5>可能的根因</h5><ul>\n'
            for cause in kb["root_causes"]:
                html += f'<li>{cause}</li>\n'
            html += '</ul></div>\n'

            # Optimizations
            html += '<div class="optimizations"><h5>优化建议</h5><ul class="opt-list">\n'
            for opt in kb["optimizations"]:
                html += f'<li>{opt}</li>\n'
            html += '</ul></div>\n'

            html += '</div>\n'  # framework-analysis

        html += '</div>\n'  # card

    # Footer
    html += f'''</div>
<footer>
    Generated by render-jank-analysis workflow | {now}
</footer>
</body>
</html>'''

    output.write_text(html)
    size_kb = output.stat().st_size / 1024
    print(f"[Phase 3] Complete: {output} ({size_kb:.0f}KB)")


# ─── CSS + Header Template ────────────────────────────────────────────

_CSS_HEADER = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android 渲染性能分析报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; background: #0d1117; color: #e6edf3; line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 28px; margin-bottom: 8px; color: #fff; }}
h2 {{ font-size: 22px; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 1px solid #30363d; color: #58a6ff; }}
h3 {{ font-size: 18px; margin: 24px 0 12px; color: #e6edf3; }}
h4 {{ font-size: 16px; margin: 20px 0 10px; color: #79c0ff; }}
h5 {{ font-size: 14px; margin: 14px 0 8px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
.subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; color: #fff; margin-left: 8px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
.card-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
th {{ background: #21262d; padding: 10px 12px; text-align: left; color: #8b949e; font-weight: 600; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
tr:hover td {{ background: #161b22; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }}
.stat-item {{ background: #21262d; border-radius: 8px; padding: 16px; text-align: center; }}
.stat-value {{ font-size: 32px; font-weight: 700; color: #58a6ff; }}
.stat-label {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}
.screenshot {{ text-align: center; margin: 16px 0; }}
.screenshot img {{
    max-width: 100%; border: 1px solid #30363d; border-radius: 8px;
    cursor: pointer; transition: all 0.3s ease;
}}
.screenshot img:hover {{ border-color: #58a6ff; box-shadow: 0 0 12px rgba(88,166,255,0.3); }}
.screenshot img.expanded {{
    position: fixed; top: 2vh; left: 2vw; width: 96vw; height: 96vh;
    object-fit: contain; z-index: 9999; background: rgba(0,0,0,0.95);
    border-radius: 12px; border: 2px solid #58a6ff;
}}
.screenshot-label {{ font-size: 12px; color: #8b949e; margin-top: 6px; }}
.framework-analysis {{
    background: #0d1117; border: 1px solid #1f3a5f; border-radius: 8px;
    padding: 16px; margin: 16px 0;
}}
.call-chain {{ margin: 12px 0; }}
.chain {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; padding: 8px; background: #161b22; border-radius: 6px; }}
.chain-step {{ background: #1a2332; padding: 4px 10px; border-radius: 4px; font-family: monospace; font-size: 13px; color: #79c0ff; white-space: nowrap; }}
.chain-arrow {{ color: #484f58; font-weight: bold; }}
.source-refs {{ margin: 12px 0; }}
.source-ref {{ margin: 10px 0; padding: 10px; background: #161b22; border-radius: 6px; border-left: 3px solid #1f6feb; }}
.source-file {{ margin-bottom: 6px; }}
.source-file code {{ color: #79c0ff; font-weight: 600; font-size: 14px; }}
.source-path {{ color: #484f58; font-size: 12px; margin-left: 8px; }}
.source-ref p {{ font-size: 13px; line-height: 1.5; color: #c9d1d9; }}
.trace-diagnosis {{ margin: 12px 0; }}
.trace-diagnosis ul {{ list-style: none; padding: 0; }}
.trace-diagnosis li {{ padding: 5px 0 5px 20px; font-size: 13px; color: #c9d1d9; position: relative; border-bottom: 1px solid #1a2332; }}
.trace-diagnosis li::before {{ content: ">>"; position: absolute; left: 0; color: #58a6ff; font-family: monospace; }}
.root-causes ul {{ list-style: none; padding: 0; }}
.root-causes li {{ padding: 6px 0; font-size: 14px; border-bottom: 1px solid #21262d; }}
.root-causes li:last-child {{ border-bottom: none; }}
.optimizations .opt-list {{ list-style: none; padding: 0; }}
.optimizations .opt-list li {{ padding: 6px 0 6px 20px; font-size: 14px; position: relative; }}
.optimizations .opt-list li::before {{ content: ">>"; position: absolute; left: 0; color: #3fb950; }}
.issue-num {{ display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; background: #1f6feb; color: #fff; font-weight: 700; font-size: 14px; margin-right: 8px; }}
footer {{ text-align: center; padding: 32px 0; color: #484f58; font-size: 13px; border-top: 1px solid #21262d; margin-top: 40px; }}
</style>
</head>
<body>
<div class="container">
<h1>Android 渲染性能分析报告</h1>
<p class="subtitle">生成时间: {time} | 目标进程: {process}</p>

<h2>概览</h2>
<div class="stat-grid">
    <div class="stat-item">
        <div class="stat-value">{total}</div>
        <div class="stat-label">总帧数</div>
    </div>
    <div class="stat-item">
        <div class="stat-value" style="color:#ff4444">{jank_n}</div>
        <div class="stat-label">Jank 帧数</div>
    </div>
    <div class="stat-item">
        <div class="stat-value" style="color:#ff4444">{jank_rate}%</div>
        <div class="stat-label">Jank 率</div>
    </div>
    <div class="stat-item">
        <div class="stat-value">{type_count}</div>
        <div class="stat-label">Jank 类型数</div>
    </div>
</div>
'''


def _load(path):
    return json.loads(Path(path).read_text())


if __name__ == "__main__":
    main()
