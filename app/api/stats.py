from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.schemas.task import StatsOut
from app.services.stats import collect_stats

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=StatsOut)
async def get_stats(
    days: int = Query(default=7, ge=0, le=365, description="时间范围天数，0 = 全部"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """成本看板：以 agent_events 聚合为对账基准（总成本/缓存节省/Token/模型拆分/趋势）。"""
    return await collect_stats(session, days)
