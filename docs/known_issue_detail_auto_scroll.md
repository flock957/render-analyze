# Known issue — `_click_slice_at` drifts detail-shot scroll position (Apr 2026)

**Status**: not fixed (deferred).
**Affected**: any detail screenshot whose `target_ts` lands on a small, clickable
Perfetto slice (tight zoom window). Reliably reproduces on
`SurfaceFlinger GPU Deadline Missed` frames in the douyin reference trace.
Intermittently affects other issues whose detail window is narrow (~160 ms).

## Symptom

In the detail shot for a `SurfaceFlinger GPU Deadline Missed` frame
(`04_SurfaceFlinger_GPU_Deadline_Missed_detail.png`) the App process group
jumps straight from the `com.ss.android.ugc.aweme` process header to
`RenderThread 5324`. The rows that should sit between them —
`Expected Timeline` and the two `droid.aweme 5126 [main thread]` rows — are
gone. Every other detail shot (00–03) in the same run shows them fine.

Pristine, `GPU completion visible` patched, and the post-Apr-13 verify run
all reproduce it; this is **not** introduced by the `scrollBy(0, 160)`
global-shot scroll patch.

## Root cause

`scripts/capture_screenshots.py`, detail-shot branch:

```python
# ── DETAIL screenshot ─────────────────────────────
_zoom_to(page, detail_start, detail_end)
# restore scroll position saved from the global step
page.evaluate(f"... p.scrollTop = {saved_scroll['top']} ...")
time.sleep(1.5)

_click_slice_at(page, target_ts, detail_start, detail_end)   # <── culprit
time.sleep(0.4)
_force_hide_ui_noise(page)
page.screenshot(path=str(output / detail_file))
```

`_click_slice_at` issues a real Playwright `page.mouse.click(x, y)` on the
Perfetto canvas, with:

```python
click_x = canvas.x + canvas.w * ratio    # derived from target_ts
click_y = canvas.y + canvas.h * 0.35     # fixed 35% from canvas top
# then a second click at (click_x, click_y + 40)
```

When the click lands on an actual Perfetto slice (canvas hit-test succeeds),
Perfetto:

1. opens the Current Selection drawer (the bottom panel), and
2. **auto-scrolls the main track view so the selected slice is visible**.

The auto-scroll moves `scrollTop` away from the value we just restored from
`saved_scroll`. `_close_drawer` / `_force_hide_ui_noise` only hide DOM via
CSS — they cannot undo the scroll that already happened. The screenshot is
taken with a drifted scroll, and whichever rows used to live just above the
selected slice get pushed out of the viewport.

## Why only issue #4

The click-y is fixed at `canvas.h * 0.35` — roughly the Actual Timeline row
of the target process group. Whether that click lands in a slice or in
whitespace depends on the **zoom window width**:

| issue | jank_type                              | dur    | detail window | click outcome                      |
| ----- | -------------------------------------- | ------ | ------------- | ---------------------------------- |
| 00    | App Deadline Missed                    | 131 ms | ±524 ms       | too wide → empty space → no select |
| 01    | App Deadline, Buffer Stuffing          | 48 ms  | ±160 ms       | usually empty space                |
| 02    | Buffer Stuffing                        | 43 ms  | ±160 ms       | usually empty space                |
| 03    | Display HAL                            | 32 ms  | ±160 ms       | partial hit — drops one main row   |
| **04**| **SurfaceFlinger GPU Deadline Missed** | **32 ms** | **±160 ms** | **clean hit on Actual Timeline doFrame → auto-scroll fires → Expected Timeline + both main-thread rows pushed off-screen** |

Issue 04 has the narrowest window *and* a `focus_track` of `Actual Timeline`,
which is exactly the row `click_y = 35%` tends to fall on.

## Fix candidates

### Option A — force-restore scroll after the click (minimal change)

```python
_click_slice_at(page, target_ts, detail_start, detail_end)
time.sleep(0.4)

# click may have triggered Perfetto auto-scroll; force it back
page.evaluate(f"""(() => {{
    const panels = document.querySelectorAll(
        '[class*="scroll"], [class*="panel-container"], [class*="viewer"]'
    );
    for (const p of panels) {{
        if (p.scrollHeight > p.clientHeight && p.clientHeight > 200) {{
            p.scrollTop = {saved_scroll.get('top', 0)};
            return;
        }}
    }}
}})()""")
time.sleep(0.3)

_close_drawer(page)
_force_hide_ui_noise(page)
page.screenshot(...)
```

Pros: preserves whatever benefit the click was meant to provide (Perfetto
may still draw an orange selection outline around the slice).

Cons: still fragile if Perfetto has pending layout animations; two-phase
scroll (drift then snap back) is visible in debug runs.

### Option B — drop `_click_slice_at` entirely (recommended)

```python
# _click_slice_at(page, target_ts, detail_start, detail_end)   ← remove
# time.sleep(0.4)                                               ← remove
_force_hide_ui_noise(page)
page.screenshot(path=str(output / detail_file))
```

Pros: removes the side-effect at its source. Detail-shot scroll is 100%
deterministic and matches `saved_scroll` exactly. All five issues render
with the same row layout.

Cons: loses Perfetto's orange slice-selection outline. In practice this
outline is hard to see in a 2144×3196 portrait shot and fully redundant
with `_annotate_detail`'s red highlight rectangle and title bar. Visual
information is not lost.

## Recommended

**Option B.** The click was a nice-to-have that backfires on exactly the
frames where a clean capture matters most (SF GPU-side jank, where the
diagnosis hinges on seeing both the App main thread timing and the SF
compositor timing in one shot).

Implementation plan when this is prioritized:

1. Remove lines 308–309 in `scripts/capture_screenshots.py`
   (`_click_slice_at(...)` + its `time.sleep(0.4)`).
2. Keep `_force_hide_ui_noise(page)` call before `page.screenshot` — still
   needed to hide sidebar / cookie banners.
3. Re-run the douyin reference trace. Verify:
   - `04_SurfaceFlinger_GPU_Deadline_Missed_detail.png` shows
     `Expected Timeline`, two `droid.aweme [main thread]` rows, and two
     `RenderThread 5324` rows in that order;
   - `00–03` detail shots look identical to pre-fix (no regression);
   - `_annotate_detail` still lands the red box at the right X (unaffected
     — it uses absolute `target_ts` / visible window math, not the click).
4. Commit: `fix(capture): drop slice click to stop Perfetto auto-scroll drifting detail shots`.

## Side observation — collapse-noise rule is incomplete

While inspecting the pristine `00_App_Deadline_Missed_detail.png`, six
system track rows (`CPU Scheduling`, `CPU Frequency`, `Ftrace Events`,
`GPU`, `Scheduler`, `System`) were visible above the process group. The
loop at capture_screenshots.py ~228 calls
`CollapseTracksByRegex` for each of these names but the folds didn't take
effect on that run. This is independent of the auto-scroll bug but worth
tracking: a detail shot that should be locked to the Frame Timeline area
wastes ~6 rows on kernel/system noise.

Candidate hardening (not implemented):

- retry each collapse once after a short delay;
- or do a single `CollapseAllGroups` first then re-expand the target
  process + Frame Timeline, rather than collapsing individual noise names.
