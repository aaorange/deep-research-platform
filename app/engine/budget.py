"""预算控制器：阶段配额 + 80% 全局降级阈值 + 报告尾部预算声明。

配额是观测口径（budget 事件 / 成本看板展示），不做阶段级硬中断——
LLM 调用无法中途取消，硬约束在节点边界生效：降级后 execute 跳过剩余
子任务（置 skipped）、reflect 跳过补充轮与评估调用，synthesize 照常
出报告并附预算受限声明。
"""

STAGE_QUOTAS: dict[str, float] = {
    "plan": 0.05,
    "execute": 0.70,
    "reflect": 0.10,
    "synthesize": 0.15,
}
DEGRADE_AT = 0.80


def stage_quota(budget: int, stage: str) -> int:
    return int(budget * STAGE_QUOTAS[stage])


def is_degraded(used: int, budget: int) -> bool:
    """预算消耗达 80% 即降级；budget<=0 视为不限额，永不降级。"""
    return budget > 0 and used >= budget * DEGRADE_AT


def budget_payload(stage: str, used: int, budget: int) -> dict:
    """EventType.budget 事件 payload：阶段配额对照 + 降级标记。"""
    return {
        "stage": stage,
        "used": used,
        "budget": budget,
        "quota": stage_quota(budget, stage),
        "degraded": is_degraded(used, budget),
    }


def disclaimer_md(used: int, budget: int) -> str:
    """降级报告尾部声明：无引用锚点，重编号后追加安全。"""
    return (
        "\n\n---\n\n> **预算受限说明**：本次研究在 token 预算消耗达到 80% 降级阈值"
        f"（已用 {used:,} / 预算 {budget:,}）后降级运行：跳过了剩余检索与补充轮，"
        "报告基于已完成的笔记材料综合，覆盖度可能受限。\n"
    )
