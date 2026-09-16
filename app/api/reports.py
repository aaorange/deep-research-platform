"""报告导出 API：Markdown / HTML / PDF 三格式（D16）。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Report, ResearchTask, Source
from app.db.base import get_session
from app.services.report_export import build_export_html, download_filename, render_pdf

router = APIRouter(prefix="/reports", tags=["reports"])

FORMATS = ("md", "html", "pdf")


def _disposition(question: str, ext: str) -> dict[str, str]:
    return {
        "Content-Disposition": f"attachment; filename*=UTF-8''{download_filename(question, ext)}"
    }


@router.get("/{report_id}/export")
async def export_report(
    report_id: int,
    format: str = Query(default="md", description="导出格式：md / html / pdf"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """下载报告：md 为 markdown 原文；html 自包含（ECharts CDN 渲染图表）；
    pdf 无头 Chromium 打印（中文走系统字体栈）。"""
    if format not in FORMATS:
        raise HTTPException(status_code=422, detail=f"format must be one of {FORMATS}")
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    task = await session.get(ResearchTask, report.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    citation_map: dict[str, int] = {
        str(n): int(sid) for n, sid in (report.citation_map or {}).items()
    }
    sources: list[dict] = []
    if citation_map:
        rows = (
            (
                await session.execute(
                    select(Source).where(
                        Source.task_id == task.id, Source.id.in_(citation_map.values())
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {s.id: s for s in rows}
        sources = [
            {"no": int(no), "title": s.title, "url": s.url, "domain": s.domain}
            for no, sid in sorted(citation_map.items(), key=lambda kv: int(kv[0]))
            if (s := by_id.get(sid)) is not None
        ]

    if format == "md":
        return Response(
            content=report.markdown,
            media_type="text/markdown; charset=utf-8",
            headers=_disposition(task.question, "md"),
        )

    html_text = build_export_html(
        task.question, report.markdown, citation_map, sources, report.chart_specs or [], task.depth
    )
    if format == "html":
        return Response(
            content=html_text,
            media_type="text/html; charset=utf-8",
            headers=_disposition(task.question, "html"),
        )

    pdf = await render_pdf(html_text)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers=_disposition(task.question, "pdf"),
    )
