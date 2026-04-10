# RenderThread Focus Plan — Execution Progress

> 2026-04-10 所有 Task + 截图重写完成

## 分支: feat/portrait-longshot @ c5808ec (10 commits ahead of v4-stable)

| Task | 描述 | 状态 | Commit |
|------|------|------|--------|
| 1 | 目标进程选择改用 jank 帧数 | **done** | `ed30d2d` |
| 2 | Pin 策略: main+RT 置顶 + hwuiTask | **done** | `b133c43` |
| 3 | 证据 SQL 限定 target 进程 | **done** | `e0f709f` |
| 4 | Detail 截图展开 RenderThread | **done** | `a94da43` |
| 5 | KEYWORDS 全 7 层扩充 | **done** | `78112a9` |
| 6 | FRAMEWORK_KB 补全 + e2e 验证 | **done** | `7379193` |
| 6b | Skill 文档同步 | **done** | `8fc9bdb` |
| **7** | **截图重写: clip-based 替代 pin-to-top** | **done** | `c5808ec` |

## 关键发现: Perfetto PinTracksByRegex 限制

`PinTracksByRegex` 对进程组内部的 track（main thread, RenderThread）只打内部标记，
**不会移到顶部 pinned 区域**。只有顶层 track（surfaceflinger 等）才移到顶部。

解决方案: 改用 clip-based 截图 —— `ExpandTracksByRegex` 展开目标进程 +
`_scroll_to_track` 滚动到目标区域 + `page.screenshot(clip=...)` 精确裁剪 canvas 区域。

## 三个 trace 验证结果

| Trace | Target | Jank | 报告 |
|-------|--------|------|------|
| 抖音 douyin | com.ss.android.ugc.aweme pid=5126 | 1103 | `/home/wq/render_output_douyin/render_report.html` |
| 设置 settings | ndroid.settings pid=9781 | 619 | `/home/wq/render_output_settings/render_report.html` |
| 浏览器 browser | om.vivo.browser pid=3497 | 1147 | `/home/wq/render_output_browser/render_report.html` |

全部 main thread + RenderThread 在截图中可见。

## 待优化
- [ ] 设置 trace 全局图 track 内容稀疏（可能需要更窄的 clip 或更好的 scroll 定位）
- [ ] Ftrace Events 面板仍偶尔出现在底部
- [ ] 部分 detail 截图 "Nothing selected"（slice click 未命中）
