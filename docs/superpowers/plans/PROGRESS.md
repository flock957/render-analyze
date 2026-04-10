# RenderThread Focus Plan — Execution Progress

> 2026-04-10 截图重写基本完成，RenderThread 在抖音 trace 的全局图和细节图中均可见

## 分支: feat/portrait-longshot @ b5d9718 (16 commits ahead of v4-stable)

| Task | 描述 | 状态 | Commit |
|------|------|------|--------|
| 1 | 目标进程选择改用 jank 帧数 | done | `ed30d2d` |
| 2 | Pin 策略: main+RT 置顶 + hwuiTask | done | `b133c43` |
| 3 | 证据 SQL 限定 target 进程 | done | `e0f709f` |
| 4 | Detail 截图展开 RenderThread | done | `a94da43` |
| 5 | KEYWORDS 全 7 层扩充 | done | `78112a9` |
| 6 | FRAMEWORK_KB 补全 + e2e 验证 | done | `7379193` |
| 6b | Skill 文档同步 | done | `8fc9bdb` |
| 7 | 截图重写: clip-based + Current Selection 隐藏 | done | `56cbed7` |
| **8** | **RenderThread 导航: omnibox 搜索 + scroll 保存/恢复** | **done** | `f081747` |

## 关键发现

1. **Perfetto PinTracksByRegex 限制**: 进程内 track 不会移到顶部 pinned 区域
2. **RenderThread 在 Frame Timeline 子组里**: 不在普通线程列表中。普通 ExpandTracksByRegex 展开进程只显示 50+ 普通线程，不显示 RenderThread
3. **omnibox 搜索 "Choreographer"** 可导航到 Frame Timeline 子组（全局图有效）
4. **scroll 保存/恢复**: 全局图的滚动位置可以传递给细节图（zoom 会重置滚动）

## 三个真实 trace 验证

| Trace | Global 显示 RT | Detail 显示 RT | 报告 |
|-------|-------------|--------------|------|
| 抖音 (1103 jank) | ✅ | ✅ | `/home/wq/render_output_douyin/render_report.html` |
| 设置 (619 jank) | ✅ | ❌ 命中其他进程 | `/home/wq/render_output_settings/render_report.html` |
| 浏览器 (1147 jank) | ✅ | 未验证 | `/home/wq/render_output_browser/render_report.html` |

## 待优化
- [ ] 设置/浏览器 trace 的 detail 截图导航到错误进程（search "Choreographer" 命中了非 target 进程）
- [ ] 需要更精确的搜索词或搜索后验证目标进程
