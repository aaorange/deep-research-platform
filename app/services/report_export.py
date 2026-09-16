"""报告导出：Markdown / HTML / PDF 三格式（D16）。

- md：markdown 原文，静态可读
- html：自包含页面——python-markdown 渲染正文（渲染前转义尖括号，防信源内容
  注入 HTML），ECharts CDN 渲染图表（内联 chart_specs），尾部信源清单
- pdf：无头 Chromium 打印 HTML（中文字体走系统字体栈，还原度最好）

图表规格契约（synthesizer 产出）：markdown 中 `<!-- chart:ID -->` 占位 +
chart_specs 数组 [{id, title, type, x, series: [{name, data}]}]。
"""

import contextlib
import html
import json
import re
from datetime import datetime
from urllib.parse import quote

import markdown as md_lib

CHART_PLACEHOLDER_RE = re.compile(r"<!--\s*chart:(c\d+)\s*-->")
ANCHOR_RE = re.compile(r"\[(\d+)\]")

ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="{echarts_cdn}"></script>
<style>
  :root {{ --accent: #2563eb; --text: #1a1f28; --text2: #5c6470; --text3: #9aa1ac;
           --border: #e8eaed; --soft: #f0f1f3; }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font: 14px/1.9 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
         color: var(--text); background: #f7f8fa; padding: 32px 0; }}
  .sheet {{ max-width: 860px; margin: 0 auto; background: #fff; border: 1px solid var(--border);
           border-radius: 10px; padding: 48px 56px 56px; }}
  .meta {{ color: var(--text3); font-size: 12px; margin-bottom: 28px; padding-bottom: 12px;
          border-bottom: 1px solid var(--soft); }}
  .meta span {{ margin-right: 18px; }}
  h1 {{ font-size: 22px; margin-bottom: 16px; letter-spacing: -0.01em; }}
  h2 {{ font-size: 17px; margin: 28px 0 12px; padding-left: 10px;
       border-left: 3px solid var(--accent); }}
  h3 {{ font-size: 15px; margin: 22px 0 8px; }}
  p {{ margin: 10px 0; }}
  ul, ol {{ padding-left: 24px; margin: 10px 0; }}
  li {{ margin: 4px 0; }}
  table {{ border-collapse: collapse; margin: 14px 0; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid var(--border); padding: 7px 12px; text-align: left; }}
  th {{ background: #f7f8fa; font-weight: 600; }}
  blockquote {{ border-left: 3px solid var(--border); background: #fafbfc; padding: 8px 14px;
               color: var(--text2); margin: 12px 0; }}
  code {{ background: #f4f5f7; border-radius: 4px; padding: 1px 6px; font-size: 12.5px; }}
  sup.cite {{ color: var(--accent); font-size: 10.5px; font-weight: 650; vertical-align: super; }}
  .chart {{ width: 100%; height: 320px; margin: 18px 0 6px; }}
  .chart-title {{ text-align: center; font-size: 13px; color: var(--text2); margin-bottom: 14px; }}
  .sources {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--soft); }}
  .sources h2 {{ border: none; padding-left: 0; font-size: 15px; }}
  .src-item {{ margin: 8px 0; font-size: 12.5px; color: var(--text2); }}
  .src-item b {{ color: var(--text); font-weight: 600; }}
  .src-item a {{ color: var(--accent); text-decoration: none; word-break: break-all; }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .sheet {{ border: none; border-radius: 0; padding: 0; max-width: none; }}
  }}
</style>
</head>
<body>
<div class="sheet">
  <div class="meta">
    <span>Deep Research 研究报告</span><span>深度：{depth}</span><span>生成于 {date}</span>
  </div>
  {body}
  <div class="sources">
    <h2>引用信源</h2>
    {sources_html}
  </div>
</div>
<script>
var CHARTS = {chart_json};
function renderCharts() {{
  document.querySelectorAll(".chart").forEach(function (el) {{
    var spec = CHARTS[el.getAttribute("data-chart")];
    if (!spec || typeof echarts === "undefined") return;
    var chart = echarts.init(el);
    var option;
    if (spec.type === "pie") {{
        option = {{
          title: {{ text: spec.title, left: "center", textStyle: {{ fontSize: 13 }} }},
          tooltip: {{ trigger: "item" }},
          series: [{{
            type: "pie", radius: "62%",
            data: spec.x.map(function (name, i) {{
              return {{ name: name, value: spec.series[0].data[i] }};
            }})
          }}]
        }};
    }} else {{
      option = {{
        title: {{ text: spec.title, left: "center", textStyle: {{ fontSize: 13 }} }},
        tooltip: {{ trigger: "axis" }},
        legend: {{ top: 26 }},
        grid: {{ left: 48, right: 24, top: 56, bottom: 32 }},
        xAxis: {{ type: "category", data: spec.x }},
        yAxis: {{ type: "value" }},
        series: spec.series.map(function (s) {{
          return {{ name: s.name, type: spec.type, data: s.data }};
        }})
      }};
    }}
    chart.setOption(option);
  }});
}}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", renderCharts);
else renderCharts();
</script>
</body>
</html>
"""


def _render_markdown_safe(markdown_text: str) -> str:
    """markdown → HTML：chart 占位符先摘出，正文转义尖括号后渲染（防 HTML 注入）。"""
    parts = CHART_PLACEHOLDER_RE.split(markdown_text)
    rendered: list[str] = [md_lib.markdown(html.escape(parts[0]), extensions=["tables"])]
    for i in range(1, len(parts), 2):
        chart_id = parts[i]
        rendered.append(f'<div class="chart" data-chart="{chart_id}"></div>')
        rendered.append(md_lib.markdown(html.escape(parts[i + 1]), extensions=["tables"]))
    return "".join(rendered)


def _cite_sup(html_text: str) -> str:
    return ANCHOR_RE.sub(lambda m: f'<sup class="cite">[{m.group(1)}]</sup>', html_text)


def build_export_html(
    question: str,
    markdown: str,
    citation_map: dict[str, int],
    sources: list[dict],
    chart_specs: list[dict],
    depth: str,
) -> str:
    """自包含 HTML：正文（角标上标 + 图表容器）+ 信源清单 + ECharts 渲染脚本。"""
    body = _cite_sup(_render_markdown_safe(markdown))
    by_no = {s["no"]: s for s in sources}
    sources_html = "\n".join(
        f'<div class="src-item"><b>[{no}]</b> {html.escape(by_no[no]["title"] or "(无标题)")} '
        f"— {html.escape(by_no[no]['domain'] or '')} · "
        f'<a href="{html.escape(by_no[no]["url"])}">{html.escape(by_no[no]["url"])}</a></div>'
        for no in sorted(by_no, key=int)
    )
    chart_json = json.dumps({c["id"]: c for c in chart_specs}, ensure_ascii=False)
    return HTML_TEMPLATE.format(
        title=html.escape(question),
        depth=depth,
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        body=body,
        sources_html=sources_html,
        chart_json=chart_json,
        echarts_cdn=ECHARTS_CDN,
    )


async def render_pdf(html_text: str) -> bytes:
    """无头 Chromium 打印 A4 PDF；CDN 图表尽力渲染（load 即打印，不无限等待）。"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html_text, wait_until="load")
            # CDN 图表渲染窗口：最多等 4 秒，失败不阻塞导出
            with contextlib.suppress(Exception):
                await page.wait_for_function(
                    "() => document.querySelectorAll('.chart canvas, .chart svg').length"
                    " === document.querySelectorAll('.chart').length"
                    " || document.querySelectorAll('.chart').length === 0",
                    timeout=4000,
                )
            return await page.pdf(
                format="A4",
                margin={"top": "18mm", "bottom": "18mm", "left": "14mm", "right": "14mm"},
                print_background=True,
            )
        finally:
            await browser.close()


def download_filename(question: str, ext: str) -> str:
    """RFC 5987 文件名：问题前 24 字符 + 日期。"""
    stem = re.sub(r"[\\/:*?\"<>|\s]+", "", question)[:24] or "report"
    return quote(f"{stem}_{datetime.now():%Y%m%d}.{ext}")


__all__ = ["build_export_html", "download_filename", "render_pdf"]
