---
name: analyze-jank-types
type: knowledge
triggers:
  - jank类型
  - jank distribution
---

# Jank 类型识别

统计 trace 中所有 Jank 类型的分布，包括帧数、平均耗时和严重程度。

## 脚本

python3 /workspace/custom/skill_examples/render_skills/scripts/analyze_jank_types.py --port 9001

## 输出字段

- total_frames: 总帧数
- jank_frame_count: Jank 帧数
- jank_rate_pct: Jank 率百分比
- detected_types: 检测到的 Jank 类型列表
- jank_types: 每种类型的详细统计
