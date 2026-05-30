# Slack Interactive Charts — Research

Research completed 2026-05-07. Covers Slack charting capabilities, chart generation engines, and Managed Agents integration changes needed.

---

## 1. Slack Chart Capabilities

### Block Kit Has No Native Chart Blocks

The complete set of Block Kit block types is: section, divider, image, actions, context, input, header, rich_text, video, file. There is no bar chart, line chart, pie chart, or sparkline component. Every charting approach in Slack ultimately renders an image and embeds it via the `image` block.

### Option Matrix

| Approach | Truly Interactive? | Renders In-Slack? | API Methods | Complexity | Best For |
|---|---|---|---|---|---|
| Block Kit `image` + QuickChart URL | No | Yes, inline | `chat.postMessage` | Very low | Quick, no-dependency charts |
| Server-generated PNG + file upload | No | Yes, inline | `files_upload_v2` | Low-medium | High-quality charts, custom styling |
| Block Kit buttons + image swap | Semi (button clicks) | Yes, inline | `chat.postMessage`, `chat.update`, interaction handler | Medium | Togglable views without leaving Slack |
| App Unfurling | Semi (button clicks) | Yes, inline | `chat.unfurl`, event subscriptions | Medium-high | Link-triggered previews |
| Work Objects + Flexpane | Semi (flexpane actions) | Yes, side panel | `entity.presentDetails`, `chat.unfurl` | High | Richest native Slack experience |
| App Home Tab | Semi (button clicks) | Yes, Home tab | `views.publish` | Low-medium | Persistent dashboards |
| Canvas | No | Yes, in canvas | `canvases.create` | Low | Document-style reports |
| External link to web dashboard | Yes (full) | No, opens browser | Button with `url` field | Medium-high | True interactivity required |

### Best In-Slack Experience: Image + Action Buttons

The recommended pattern combines a static chart image with Block Kit action buttons for view toggling:

```json
{
  "blocks": [
    {
      "type": "image",
      "title": {"type": "plain_text", "text": "Pipeline by Stage"},
      "image_url": "https://quickchart.io/chart?c=...",
      "alt_text": "Pipeline chart"
    },
    {
      "type": "actions",
      "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "By Stage"}, "action_id": "chart_by_stage", "style": "primary"},
        {"type": "button", "text": {"type": "plain_text", "text": "By Rep"}, "action_id": "chart_by_rep"},
        {"type": "button", "text": {"type": "plain_text", "text": "By Quarter"}, "action_id": "chart_by_quarter"},
        {"type": "button", "text": {"type": "plain_text", "text": "Open Dashboard"}, "url": "https://your-app.com/dashboard"}
      ]
    }
  ]
}
```

When a user clicks "By Rep", the app generates a new chart URL grouped by rep and calls `chat.update` to swap the image in place. Each click is a server round-trip, but the user never leaves Slack.

### Slack Canvas

`canvases.create` and `canvases.edit` accept markdown document content. You can embed images via publicly-hosted URLs or Slack-hosted file permalinks. No interactive components are supported in canvases. Useful for document-style reports but not chart interactivity.

### Work Objects + Flexpane (Newest)

GA as of October 2025. Extends link unfurling with a right-side panel (flexpane) via `entity.presentDetails`. The flexpane uses metadata schemas, not arbitrary HTML/iframe — you cannot embed interactive Chart.js in it. You can include chart image URLs. Requires Slack Marketplace submission. Bolt for Python support listed as "coming soon." High complexity, marginal benefit over the image+buttons pattern.

### App Home Tab

`views.publish` supports up to 100 Block Kit blocks. Can serve as a persistent dashboard with image blocks and action buttons. Simpler than unfurling. Users must navigate to the app's Home tab to see it.

### External Link (Only Path to True Interactivity)

The only way to get hover tooltips, zoom, pan, and click-to-drill-down is to host an interactive chart on a web page and link to it from Slack via a button with a `url` field. Plotly Dash, Streamlit, Grafana, or Metabase can serve as the hosting layer.

---

## 2. Chart Generation Engines

### QuickChart.io (Recommended)

- **Install size:** <50 KB (`quickchart-io` pip package). Zero native dependencies. Pure-Python HTTP client.
- **How it works:** Constructs a URL encoding a Chart.js config. QuickChart's server renders it to PNG. The URL can be used directly in Slack `image` blocks — no file upload needed.
- **Chart types:** Everything Chart.js supports — bar, line, pie, doughnut, radar, scatter, bubble, mixed, plus plugins (datalabels, annotation, funnel).
- **Free tier:** 100,000 images/month, 50,000 short URLs/month, 120 req/min/IP.
- **Self-hosting:** Open source Docker image (`docker run -p 3400:3400 ianw/quickchart`).

```python
from quickchart import QuickChart

qc = QuickChart()
qc.width = 600
qc.height = 400
qc.device_pixel_ratio = 2.0
qc.config = {
    "type": "bar",
    "data": {
        "labels": ["Q1", "Q2", "Q3", "Q4"],
        "datasets": [{"label": "Pipeline $K", "data": [120, 95, 200, 150]}]
    }
}

# URL mode (for Slack image blocks, no upload needed)
url = qc.get_url()

# Bytes mode (for file upload)
img_bytes = qc.get_bytes()

# Short URL (fixed-length, expires after 3 days free / 6 months paid)
short = qc.get_short_url()
```

URL-encoded configs have a 3000-char limit in Slack `image_url`. For complex charts, use the POST endpoint or upload bytes via `files_upload_v2`.

### matplotlib

- **Install size:** ~80-100 MB (matplotlib + NumPy + Pillow + smaller deps).
- **Already installed** on the development system (v3.9.4).
- **PNG to bytes:** Native via `savefig()` to `BytesIO`.
- **Quality:** Publication-grade. Full control over every visual element.
- **Business chart ease:** Medium. Low-level API. A polished funnel or annotated trend requires 20-40 lines.
- **No URL generation.** Local rendering only.

```python
from io import BytesIO
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.bar(["Q1", "Q2", "Q3", "Q4"], [120, 95, 200, 150])
buf = BytesIO()
fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
buf.seek(0)
plt.close(fig)
```

### Plotly + Kaleido

- **Install size:** ~40-50 MB (if Chrome already present). Kaleido v1.0+ requires Chrome/Chromium on the host (~300 MB in Docker).
- **Quality:** Excellent. Modern, clean defaults. Looks like a SaaS dashboard.
- **Business chart ease:** Very high. Built-in funnel (`go.Funnel`), waterfall, gauge. Plotly Express can create most charts in 1-3 lines.
- **Interactive HTML:** Killer feature. `fig.write_html("chart.html", include_plotlyjs="cdn")` produces a standalone ~15 KB file with full hover/zoom/pan.

```python
import plotly.graph_objects as go
import plotly.io as pio

fig = go.Figure(go.Bar(x=["Q1", "Q2", "Q3", "Q4"], y=[120, 95, 200, 150]))
img_bytes = pio.to_image(fig, format="png", width=800, height=400)  # requires kaleido
fig.write_html("chart.html", include_plotlyjs="cdn")  # interactive version
```

### Altair / Vega-Lite

- **Install size:** ~50-70 MB (Altair + vl-convert-python for PNG export).
- **Quality:** Excellent for statistical/analytical charts.
- **Business chart ease:** Medium. No native funnel or gauge. 5,000-row default data limit.
- **Adds pandas** as a dependency (~30 MB).
- **Not recommended** for this use case.

### Pygal

- **Install size:** ~1 MB. SVG-focused.
- **Has funnel/gauge types** built in, but SVG output requires `cairosvg` (~5 MB + system cairo libs) for PNG conversion.
- **Slack does not support SVG** in image blocks. Not practical.

### Comparison

| Library | Install Size | PNG Bytes | URL Mode | Business Charts | Interactive HTML | Docker Friendly |
|---|---|---|---|---|---|---|
| QuickChart.io | 50 KB | Yes | Yes (primary) | High (Chart.js) | No | Excellent |
| matplotlib | ~100 MB | Yes (native) | No | Medium | No | Good |
| Plotly + Kaleido | ~50 MB + Chrome | Yes | No | Very high | Yes | Heavy |
| Altair | ~70 MB + pandas | Yes | No | Medium | Yes | Medium |
| Pygal | ~1 MB | Needs cairo | No | Medium | No | Awkward |

---

## 3. Slack Integration Mechanics

### Option A: URL in Image Block (No Upload)

```python
blocks = [{
    "type": "image",
    "image_url": "https://quickchart.io/chart?c=...",
    "alt_text": "Pipeline trend"
}]
app.client.chat_postMessage(channel=channel, blocks=blocks, text="Chart")
```

Works with QuickChart URLs. Zero file upload. Simplest path. URL must be publicly accessible.

### Option B: Upload Bytes via files_upload_v2

```python
app.client.files_upload_v2(
    channel=channel,
    content=img_bytes,       # PNG bytes from any library
    filename="pipeline.png",
    title="Pipeline Health",
    initial_comment="Here's the current pipeline breakdown:",
    thread_ts=reply_to,      # optional thread
)
```

Under the hood, `files_upload_v2` executes three API calls:
1. `files.getUploadURLExternal` — gets a presigned upload URL
2. HTTP POST to that URL with the file bytes
3. `files.completeUploadExternal` — finalizes and shares to channel

Requires `files:write` OAuth scope. Use `.getvalue()` on BytesIO to get raw bytes (historical issues with passing BytesIO objects directly).

### Option C: Upload + Reference in Blocks via slack_file

```python
upload = app.client.files_upload_v2(file="/tmp/chart.png", filename="chart.png")
file_url = upload["file"]["permalink"]
```

---

## 4. Managed Agents Integration

### Current Architecture

Custom tools are defined in `agents/setup_agents.py` as dicts with `type: "custom"`, `name`, `description`, and `input_schema` (JSON Schema). Four tools exist:

- `sf_query` — SOQL query execution
- `sf_query_next` — pagination
- `sf_describe` — object metadata
- `send_slack_notification` — post alerts

The dispatch lifecycle in `session_runner.py`:
1. Agent emits `agent.custom_tool_use` → orchestrator buffers in `pending_tools`
2. Session goes idle with `stop_reason.type == "requires_action"`
3. `_dispatch_tool(name, input)` routes by tool name
4. Result sent back via `user.custom_tool_result`
5. Session resumes

### Proposed `generate_chart` Tool Schema

```python
CHART_TOOL = {
    "type": "custom",
    "name": "generate_chart",
    "description": (
        "Generate a chart image and post it to Slack. The orchestrator renders "
        "the chart and uploads it to the current channel. Use for visualizing "
        "trends, comparisons, distributions, and pipeline health. Returns the "
        "chart URL. Always provide a descriptive title."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chart_type": {
                "type": "string",
                "enum": ["bar", "line", "pie", "doughnut", "stacked_bar", "radar", "funnel"],
                "description": "Type of chart to render",
            },
            "title": {
                "type": "string",
                "description": "Chart title displayed above the chart",
            },
            "data": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Category labels (x-axis for bar/line, segments for pie)",
                    },
                    "datasets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                            },
                            "required": ["label", "values"],
                        },
                        "description": "One or more data series",
                    },
                },
                "required": ["labels", "datasets"],
            },
            "options": {
                "type": "object",
                "description": "Optional Chart.js options (axis labels, colors, legend position, etc.)",
            },
            "reply_to": {
                "type": "string",
                "description": "Slack thread timestamp to post chart in (optional)",
            },
        },
        "required": ["chart_type", "title", "data"],
    },
}
```

### Files to Modify

| File | Change |
|------|--------|
| `agents/setup_agents.py` | Add `CHART_TOOL` dict, attach to Coordinator's tools list |
| `orchestrator/session_runner.py` | Add `generate_chart` branch in `_dispatch_tool`, build QuickChart URL, call `post_chart()` |
| `orchestrator/slack_bot.py` | Add `post_chart()` using Block Kit image block + action buttons, add interaction handler for `block_actions` |
| `orchestrator/requirements.txt` | Add `quickchart-io` |
| Slack app manifest | Ensure `files:write` scope (for fallback file upload) |

### No Current File Upload Capability

The bot currently uses only `chat.postMessage`. There are no calls to `files.upload`, `files_upload_v2`, or any file-related Slack API. The `files:write` scope may need to be added to the Slack app.

---

## 5. Recommended Approach

### Primary: QuickChart.io + Block Kit Image Blocks + Action Buttons

1. Agent calls `generate_chart` with structured data from Salesforce queries
2. Orchestrator builds a Chart.js config dict from the tool input
3. Uses `quickchart-io` to generate a URL
4. Posts to Slack with:
   - `image` block containing the chart URL
   - `actions` block with toggle buttons (By Stage / By Rep / By Quarter / Trend)
5. Interaction handler listens for `block_actions`, regenerates chart with different grouping, calls `chat.update` to swap the image

### Optional Enhancement: Interactive HTML Click-Through

For users who need hover/zoom/drill-down:
1. Also generate a Plotly HTML file from the same data
2. Host it on S3/Cloudflare Pages/static server
3. Add an "Open Interactive" button in the actions block with a `url` pointing to the hosted HTML

This requires a hosting target and adds the Plotly dependency. Can be deferred.

### Dual-Mode Architecture

```
Agent calls generate_chart
    ↓
Orchestrator builds Chart.js config
    ↓
┌─────────────────┐    ┌──────────────────────┐
│ QuickChart URL   │    │ Plotly HTML (optional)│
│ → image block    │    │ → hosted page         │
│ → inline in Slack│    │ → "Open" button URL   │
└─────────────────┘    └──────────────────────┘
    ↓
Post to Slack with image + action buttons
    ↓
User clicks toggle → regenerate chart → chat.update
User clicks "Open Interactive" → browser opens hosted page
```
