# UNISI — Charts

> Developer guide for both ways to put a chart on screen: projecting a
> `Table` into a line chart with `view=`, or rendering a `Chart` unit from
> a native ECharts `option`.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Table-Projection Chart (`view=`)](#2-table-projection-chart-view)
3. [Native ECharts Chart (`Chart`)](#3-native-echarts-chart-chart)
   - [3.1 Basic example](#31-basic-example)
   - [3.2 `value`, `changed`, and what a click sends](#32-value-changed-and-what-a-click-sends)
   - [3.3 More chart types](#33-more-chart-types)
   - [3.4 Updating a chart from the server](#34-updating-a-chart-from-the-server)
   - [3.5 Persistence](#35-persistence)
4. [Autotest Validation](#4-autotest-validation)
5. [Choosing Between Them](#5-choosing-between-them)
6. [Reference](#6-reference)

---

## 1. Overview

UNISI gives you two independent ways to put a chart on screen, chosen
entirely by which constructor built the unit:

- **`Table(..., view=...)`** turns row/column data you already have into
  a line chart, computed automatically. You never touch a charting
  library.
- **`Chart(name, option, changed?)`** takes a native
  [Apache ECharts](https://echarts.apache.org/en/option.html) `option`
  object directly. Any chart type ECharts supports — bar, pie, scatter,
  radar, heatmap, gauge, candlestick, combined charts — works, at the
  cost of writing that option yourself.

Both end up as a unit with `type == "chart"` sent to the browser, and
both are rendered by the same bundled ECharts-based client component.
Neither is "built on top of" the other — `Chart` is a small, independent
`Unit`, not a `Table` subclass.

Short answer: start with `view=` if your data is already a table and a
line chart is enough; reach for `Chart` the moment you need a different
chart type or full control. §5 has the detailed comparison.

## 2. Table-Projection Chart (`view=`)

Add a `view=` string to any `Table` and UNISI can display it as a chart
instead of (or in addition to) a table.

**`view` format:** `"{x column index}-{y column index}[,{y column index}...]"`

- `"0-1,2,3"` — x-axis values come from column 0; columns 1, 2, and 3
  each become their own line series.
- `"i-1,2"` — the letter `i` in the x slot means "use each row's own
  index as the x value" instead of a column.

```python
from unisi import *
import random

def on_changed(table, value):
    print("selected row:", value)

table = Table(
    "Audios", 1, on_changed,
    type="chart",
    headers=["Audio", "Duration,sec", "Stars"],
    multimode=True,
    rows=[[f"sync{i}.mp3", round(random.random() * 15000) / 100, random.randint(1, 50)]
          for i in range(100)],
    view="i-1,2",
)
```

*(adapted from `test_apps/blocks/blocks/tblock.py`)*

- `type="chart"` renders as a chart immediately on load. Leave `type` at
  its default (`"table"`) and the widget starts as a normal table, but —
  as long as `view` is set — a chart icon appears in the table header;
  clicking it switches to chart mode. A matching icon in the chart's own
  toolbar switches back.
- Selection works exactly like a plain `Table`: `value` is the selected
  row index (or a list of indices under `multimode=True`), and `changed`
  fires the same way — clicking a point on the chart marks/unmarks that
  row, same as clicking a table row would.
- `view` is parsed entirely on the client; UNISI's Python side only
  checks that it's present when `type="chart"` (§4), not that it's
  well-formed. A typo in the column indices fails silently in the
  browser, not with a Python exception.
- This mode always draws a line chart. There's no way to get a bar or
  pie chart out of `view=` — for that, use `Chart` (§3).

## 3. Native ECharts Chart (`Chart`)

### 3.1 Basic example

```python
from unisi import *

sales = Chart(
    "Monthly Sales",
    {
        "xAxis": {"type": "category", "data": ["Jan", "Feb", "Mar", "Apr", "May"]},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": [120, 200, 150, 80, 70]}],
    },
)
```

That's the whole thing — `option` is handed to ECharts' `setOption()`
essentially as-is. UNISI does not parse, validate, or understand what's
inside it; [ECharts' own option reference](https://echarts.apache.org/en/option.html)
is the authoritative source for what you can put there, not this
document.

### 3.2 `value`, `changed`, and what a click sends

`Chart`'s `value` is **not** the chart definition — that's `option`.
`value` is the current *selection*, the same idea as `Table`'s `value`,
just sourced from ECharts instead of row indices:

```python
def on_point_clicked(chart, value):
    print("clicked:", value)   # whatever ECharts' click event reported

sales = Chart("Monthly Sales", {...}, on_point_clicked)
```

- `value` starts as `None`.
- Clicking anything on the chart sends ECharts' click-event `params.value`
  back to the server and calls `changed` with it (or, with no `changed`
  handler, auto-accepts it into `.value` the same as any other `Unit`).
- The *shape* of that value depends on your own `series[].data`, not on
  UNISI: a plain number for a simple line/bar point, a `{name, value}`
  pair for a pie slice, an `[x, y]` pair for some scatter data shapes,
  and so on. If you're not sure what a given series type reports, log it
  once from `changed` and check.
- Only clicks are reported to the server. Zoom, pan, hovering, and
  toggling legend entries all stay client-side — UNISI doesn't see them
  and `changed` doesn't fire for them.

### 3.3 More chart types

The whole point of `Chart` is that it isn't limited to lines. A few short
examples:

**Pie:**
```python
Chart("Traffic Sources", {
    "series": [{
        "type": "pie",
        "radius": "60%",
        "data": [
            {"value": 1048, "name": "Search"},
            {"value": 735, "name": "Direct"},
            {"value": 580, "name": "Referral"},
        ],
    }],
})
```

**Scatter:**
```python
Chart("Height vs Weight", {
    "xAxis": {"name": "Height (cm)"},
    "yAxis": {"name": "Weight (kg)"},
    "series": [{"type": "scatter", "data": [[172, 68], [180, 75], [165, 58]]}],
})
```

**Gauge:**
```python
Chart("CPU Load", {
    "series": [{"type": "gauge", "data": [{"value": 72, "name": "Load %"}]}],
})
```

Combined charts (a line and a bar sharing one `xAxis`, dual y-axes,
`dataZoom`, custom `tooltip` formatters, themes...) all work the same
way — write the `option` ECharts expects for that.

### 3.4 Updating a chart from the server

Mutate `.option` on a live `Chart` instance from a handler, a background
task, or anywhere else with a reference to it, and UNISI pushes the
update automatically — the same change-tracking every `Unit` gets:

```python
history = [70]

def add_point(chart, value):
    history.append(history[-1] + random.randint(-10, 15))
    chart.option = {
        "xAxis": {"type": "category", "data": list(range(len(history)))},
        "yAxis": {"type": "value"},
        "series": [{"type": "line", "data": history}],
    }

sales = Chart("Live Value", {
    "xAxis": {"type": "category", "data": [0]},
    "yAxis": {"type": "value"},
    "series": [{"type": "line", "data": history}],
})
refresh = Button("Add a point", add_point)
```

Each `option` push is a **full replacement** of what the chart shows, not
a merge — if a later push drops a series, or a field the previous one
had, that series or field disappears rather than lingering on screen.
Practically, this means: always send the *complete* option you want
displayed, not a partial diff. There's no server-side way to say "just
append to this series" — recompute and resend the whole `option` (as
`add_point` above does).

### 3.5 Persistence

`persist=True` (or a key-function) works on `Chart` the same as on any
`Unit` — but it persists the last-clicked `value`, not `option`. `option`
isn't something UNISI reconstructs for you across sessions; supply it
fresh on every screen load from your own data source, the same way you'd
supply `rows`/`headers` to a `Table`.

## 4. Autotest Validation

If `config.autotest` is enabled, UNISI's block-structure check rejects a
`type="chart"` unit that has neither `view` nor `option` set — you'll see
an error naming the block and the unit at startup rather than a silent
blank chart. A `Chart()` built with no argument still passes this check
(it defaults to `option={}` — an empty chart, not a missing one), so this
check mainly catches a `Table` where `type="chart"` was set but `view`
was forgotten.

## 5. Choosing Between Them

| | `Table(..., view=...)` | `Chart(name, option, ...)` |
|---|---|---|
| Chart types | Line only | Any ECharts type |
| Where the data comes from | Existing `rows`/`headers` | An ECharts `option` you build |
| Table ↔ chart toggle | Built in | No — chart only |
| Selection (`value`) | Row index / indices | Raw ECharts click `params.value` |
| ECharts knowledge needed | None | Yes |
| Validated server-side | `view` presence only | `option` presence only |

Use `Table(..., view=...)` when a line chart over data you already have
in a table is enough, especially if you also want the table view of the
same data one click away. Reach for `Chart` as soon as you need a
different chart type, specific styling, multiple series types combined,
or data that was never row/column-shaped in the first place.

## 6. Reference

- [Apache ECharts — option reference](https://echarts.apache.org/en/option.html) —
  the full schema for `Chart`'s `option`.
- `unisi/units.py`, `class Chart(Unit)` — the Python side, about a dozen
  lines.
- `unisi/autotest.py` — the `view`/`option` presence check described in
  §4.
- `unisi-programming-spec.md` §13 — the formal constructor spec.
- `UNISI skill.md` §18 — verified internals and a couple of
  easy-to-misassume gotchas (what the constructor's positional args
  actually become, what happens with no arguments at all).
- `test_apps/blocks/blocks/tblock.py` — the `view=` example in §2 is
  adapted from here.
