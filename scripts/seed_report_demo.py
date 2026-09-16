"""演示数据：造一个已完成任务（子任务/信源/笔记/报告/事件流/追问）。

数据形态与引擎真实落库一致：笔记正文的 [N] 锚点是信源库 id（persister
重写后的格式），报告 markdown 的 [N] 是展示编号，二者通过 citation_map
映射。agent_events 覆盖 plan/search/fetch/degrade/note/reflect/synthesize/
budget/chat 全链路：LLM 事件带 model/prompt/completion 计费拆分，检索与
读页事件含 cache 命中，task 的 token_used / cost_cny 由事件推算，保证成
本看板对账一致。运行：uv run python scripts/seed_report_demo.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import (
    AgentEvent,
    ChatMessage,
    ChatRole,
    EventType,
    Note,
    Report,
    ResearchTask,
    Source,
    SubTask,
    SubTaskStatus,
    TaskStatus,
)
from app.db.base import SessionLocal
from app.engine.budget import budget_payload
from app.engine.persister import cost_cny

SUBS = [
    "医疗大模型监管政策与三类证审批进展",
    "医学影像与辅助诊断场景落地情况",
    "临床决策支持与电子病历智能化",
    "药企研发与蛋白质结构预测应用",
    "医疗大模型市场格局与商业化模式",
    "数据合规与医疗隐私计算挑战",
]
SUB_R2 = "欧盟 MDR 与中国 NMPA 的 AI 医疗器械审批路径对比"

KEYWORDS = [
    "AI 辅助诊断 三类证 审批通道",
    "医学影像 大模型 三甲医院 采购",
    "临床决策支持 电子病历 质控",
    "蛋白质结构预测 大模型 药企",
    "医疗大模型 市场规模 商业化模式",
    "医疗数据合规 隐私计算 分类分级",
]

SOURCES = [
    (
        "https://www.nmpa.gov.cn/xxgk/zhcexx/20250312.htm",
        "国家药监局：AI 辅助诊断软件优先审批通道公告",
        "nmpa.gov.cn",
        5,
        0.95,
    ),
    (
        "https://www.cn-healthcare.com/articlewm/20250601/content-1632101.html",
        "2025 医疗 AI 三类证审批盘点：47 张证背后的提速逻辑",
        "cn-healthcare.com",
        4,
        0.85,
    ),
    (
        "https://www.36kr.com/p/3182764501",
        "医学影像大模型：头部三甲医院的采购清单变了",
        "36kr.com",
        3,
        0.7,
    ),
    (
        "https://www.jiqizhixin.com/articles/6f3e9a2b",
        "临床决策支持系统进入大模型时代：一场安静的重构",
        "jiqizhixin.com",
        4,
        0.75,
    ),
    (
        "https://www.vcbeat.top/51c9f8e2",
        "医疗大模型商业化报告：市场规模与厂商份额 2025",
        "vcbeat.top",
        4,
        0.8,
    ),
    (
        "https://www.phirda.com/articles/28f1a3d0",
        "药企引入大模型做蛋白质结构预测的得与失",
        "phirda.com",
        3,
        0.65,
    ),
    (
        "https://www.gov.cn/zhengce/202504/content_7021.htm",
        "国务院办公厅：促进和规范健康医疗大数据应用发展的指导意见",
        "gov.cn",
        5,
        0.9,
    ),
    (
        "https://www.caict.ac.cn/kxyj/qwfb/202505/t20250520_403210.html",
        "中国信通院：医疗大模型数据合规白皮书（2025）",
        "caict.ac.cn",
        4,
        0.85,
    ),
]

NOTES = {
    0: "监管层在 2025 年 3 月将 AI 辅助诊断软件纳入优先审批通道，三类证平均审批周期从 26 个月压缩至 16 个月[{{s0}}]。截至 6 月底，已有 47 张医疗 AI 三类证获批，其中 11 张为大模型驱动产品[{{s1}}]。",
    1: "头部三甲医院 2025 年影像科预算向大模型辅助诊断倾斜，肺结节与眼底两类场景渗透率最快[{{s2}}]。某省会城市三甲医院采购清单显示，传统 CAD 软件续费率下降 40%，大模型方案进入招标短名单[{{s2}}]。",
    2: "临床决策支持系统正在被大模型重构：从规则引擎匹配转为长病历理解与鉴别诊断建议[{{s3}}]。电子病历内涵质控是当前落地最快的子场景，头部厂商中标价约 120-180 万元/院[{{s3}}]。",
    3: "药企侧蛋白质结构预测引入国产大模型后，部分靶点筛选周期从 6 周缩短到 9 天，但晶型预测的准确率仍低于国际闭源方案[{{s5}}]。国内已有 14 家创新药企公开确认采购相关服务[{{s5}}]。",
    4: "2025 年中国医疗大模型市场规模预计 82 亿元，头部厂商合计份额接近七成，商业模式以「按年订阅 + 调用计费」混合为主[{{s4}}]。医院端付费意愿显著分化，三级医院贡献了约 75% 的收入[{{s4}}]。",
    5: "医疗数据合规是最大落地摩擦：训练数据需完成脱敏与授权双链条，跨院数据流通依赖隐私计算平台[{{s6}}]。国家层面已明确健康医疗数据的分类分级管理要求，院内数据出域仍受严格限制[{{s7}}]。",
    6: "欧盟 MDR 将 AI 医疗器械纳入全生命周期证据链管理，与 NMPA 的优先审批通道相比，欧盟路径平均多 4-6 个月但临床证据要求更前置[{{s0}}]。境外厂商进入中国同样必须取得 NMPA 三类证，进口产品可走优先审批通道[{{s1}}]。多国对照显示，中国的审批提速幅度最大[{{s7}}]。",
}

REPORT = """# 2025 年国产大模型在医疗行业的落地情况

## 摘要

2025 年，国产大模型在医疗行业完成了从「技术演示」到「规模化收费」的关键跨越：监管侧三类证审批显著提速[1]；场景侧医学影像[3]、临床决策支持[4]与药企研发[6]构成三大主战场；市场侧头部厂商合计拿走近七成份额[5]。但商业化仍受数据合规[8]、医院预算与幻觉风险三重约束，头部机构正以「人机协同」模式消化这些摩擦。

## 一、政策与审批：从试点走向常态化

国家药监局 3 月将 AI 辅助诊断软件纳入优先审批通道，三类证平均审批周期从 26 个月压缩至 16 个月[1]；截至 6 月底已有 47 张医疗 AI 三类证获批，其中 11 张为大模型驱动产品[2]。

<!-- chart:c1 -->

> 审批提速的直接后果是商业闭环首次跑通：厂商拿到证即可进院收费，投资人对「有证公司」的估值容忍度明显放宽[2]。

国务院办公厅 4 月印发的健康医疗大数据指导意见，进一步明确了数据的分类分级管理要求，为训练数据的合规使用划定了边界[7]。

## 二、落地场景：三个主战场与一个隐形冠军

**医学影像**是渗透最快的场景。头部三甲医院影像科预算向大模型辅助诊断倾斜，肺结节与眼底筛查两类场景渗透率领先[3]；传统 CAD 软件续费率下降约 40%，大模型方案已进入招标短名单[3]。

**临床决策支持**正在从规则引擎转向长病历理解，电子病历内涵质控成为落地最快的子场景，头部厂商中标价约 120-180 万元/院[4]。

**药企研发**侧，蛋白质结构预测引入国产大模型后，部分靶点筛选周期从 6 周缩短至 9 天；14 家创新药企公开确认采购，但晶型预测准确率仍低于国际闭源方案[6]。

<!-- chart:c2 -->

| 场景 | 代表能力 | 计费模式 | 渗透阶段 |
| --- | --- | --- | --- |
| 医学影像 | 肺结节/眼底筛查 | 按例计费 | 规模化 |
| 病历质控 | 长病历理解 | 年订阅 | 快速放量 |
| 药物研发 | 蛋白结构预测 | 项目制 | 早期 |
| 医保风控 | 异常票据识别 | 调用计费 | 试点 |

## 三、市场格局：七成份额与两级分化

2025 年中国医疗大模型市场规模预计 82 亿元，头部厂商合计份额接近七成，商业模式以「按年订阅 + 调用计费」混合为主[5]。付费意愿显著分化：三级医院贡献约 75% 的收入，基层机构仍以试点为主[5]。

<!-- chart:c3 -->

## 四、挑战：合规、预算与信任

数据合规是最大摩擦——训练数据需完成脱敏与授权双链条，跨院数据流通依赖隐私计算平台，院内数据出域仍受严格限制[8]。叠加医生对幻觉风险的顾虑，厂商普遍以「建议 + 医生确认」的人机协同模式消化信任问题[5][8]。

## 结论

医疗正在成为国产大模型第一个「证照齐全」的规模化行业。审批通道打开供给，影像与病历质控场景承接需求，头部厂商的份额集中趋势短期不会逆转；真正的分水岭在于谁能率先把合规成本做低，把医生信任做实[1][5][8]。
"""

CHARTS = [
    {
        "id": "c1",
        "title": "三类证平均审批周期（月）",
        "type": "bar",
        "x": ["常规通道", "优先审批通道"],
        "series": [{"name": "审批周期（月）", "data": [26, 16]}],
    },
    {
        "id": "c2",
        "title": "蛋白质靶点筛选周期（天）",
        "type": "bar",
        "x": ["传统方法", "大模型辅助"],
        "series": [{"name": "筛选周期（天）", "data": [42, 9]}],
    },
    {
        "id": "c3",
        "title": "医疗大模型收入按医院等级（%）",
        "type": "pie",
        "x": ["三级医院", "基层与其他机构"],
        "series": [{"name": "收入占比", "data": [75, 25]}],
    },
]

# 每个子任务成功读取的信源（下标, provider）：跨子任务重读同 URL 走页面缓存
FETCH_PLAN = [
    [(0, "jina"), (1, "jina"), (7, "jina")],
    [(2, "jina")],
    [(3, "crawl4ai")],
    [(5, "jina"), (7, "cache")],
    [(4, "jina"), (1, "cache")],
    [(6, "jina"), (7, "cache")],
    [(0, "cache"), (1, "cache")],  # R2 补搜：重读监管信源
]

FETCH_CHARS = [9200, 6800, 5400, 7100, 8300, 4900, 6200, 5800]

# LLM 计费拆分（prompt, completion）：tokens = 两者之和
PLAN_USAGE = (3240, 720)
NOTE_USAGE = (3600, 780)
NOTE_R2_USAGE = (3200, 690)
REFLECT_USAGE = (4100, 360)
REFLECT_R2_USAGE = (3800, 310)
SYNTH_USAGE = (8200, 9864)
CHAT_USAGE = (2050, 520)

BUDGET = 80_000


class EventLog:
    """按工作流顺序分配 seq，统一推进时间线，并累计计费口径。"""

    def __init__(self, task_id: int, t0: datetime, model_chat: str, model_reasoner: str):
        self.task_id = task_id
        self.t0 = t0
        self.model_chat = model_chat
        self.model_reasoner = model_reasoner
        self.seq = 0
        self.events: list[AgentEvent] = []
        self.usage: list[tuple[str, int, int]] = []

    def add(self, minutes: float, ev_type: EventType, payload: dict, **kw) -> None:
        self.seq += 1
        self.events.append(
            AgentEvent(
                task_id=self.task_id,
                seq=self.seq,
                type=ev_type,
                payload=payload,
                created_at=self.t0 + timedelta(minutes=minutes),
                **kw,
            )
        )

    def llm(
        self,
        minutes: float,
        ev_type: EventType,
        usage: tuple[int, int],
        reasoner: bool,
        payload: dict,
        **kw,
    ) -> None:
        model = self.model_reasoner if reasoner else self.model_chat
        prompt, completion = usage
        self.add(
            minutes,
            ev_type,
            {**payload, "model": model, "prompt_tokens": prompt, "completion_tokens": completion},
            tokens=prompt + completion,
            **kw,
        )
        self.usage.append((model, prompt, completion))

    def commit(self, session) -> None:
        session.add_all(self.events)

    def token_used(self) -> int:
        return sum(p + c for _, p, c in self.usage)

    def cost_cny(self) -> float:
        return round(sum(cost_cny(m, p, c) for m, p, c in self.usage), 2)


async def main() -> None:
    settings = get_settings()
    t0 = datetime.now(UTC) - timedelta(hours=6)

    async with SessionLocal() as session:
        task = ResearchTask(
            question="2025 年国产大模型在医疗行业的落地情况",
            status=TaskStatus.done,
            depth="std",
            token_budget=BUDGET,
        )
        session.add(task)
        await session.flush()

        sub_ids = []
        for title in SUBS:
            sub = SubTask(task_id=task.id, title=title, status=SubTaskStatus.done, round_no=1)
            session.add(sub)
            await session.flush()
            sub_ids.append(sub.id)
        r2 = SubTask(task_id=task.id, title=SUB_R2, status=SubTaskStatus.done, round_no=2)
        session.add(r2)
        await session.flush()
        sub_ids.append(r2.id)

        src_ids = []
        for url, title, domain, cred, fresh in SOURCES:
            s = Source(
                task_id=task.id,
                url=url,
                title=title,
                domain=domain,
                credibility=cred,
                freshness=fresh,
                content_hash=f"demo-{len(src_ids)}",
            )
            session.add(s)
            await session.flush()
            src_ids.append(s.id)

        for i, template in NOTES.items():
            content = template.format(**{f"s{j}": src_ids[j] for j in range(len(src_ids))})
            session.add(Note(task_id=task.id, sub_task_id=sub_ids[i], content=content))

        citation_map = {str(i + 1): src_ids[i] for i in range(len(src_ids))}
        report = Report(
            task_id=task.id,
            version=1,
            markdown=REPORT,
            citation_map=citation_map,
            chart_specs=CHARTS,
            token_total=SYNTH_USAGE[1],
        )
        session.add(report)
        await session.flush()

        log = EventLog(task.id, t0, settings.llm_model_chat, settings.llm_model_reasoner)

        log.llm(
            0.0,
            EventType.plan,
            PLAN_USAGE,
            reasoner=False,
            payload={
                "depth": "std",
                "count": len(SUBS),
                "titles": SUBS,
                "fallback": False,
            },
        )
        log.add(
            0.2, EventType.budget, budget_payload("plan", PLAN_USAGE[0] + PLAN_USAGE[1], BUDGET)
        )

        for i, (_title, keywords) in enumerate(zip(SUBS, KEYWORDS, strict=True)):
            base = 1.0 + i * 4.0
            log.add(
                base,
                EventType.search,
                {
                    "query": keywords,
                    "provider": "bocha",
                    "hits": 8,
                    "titles": [SOURCES[j][1] for j in (i, (i + 1) % 8, (i + 2) % 8)][:3],
                },
                sub_task_id=sub_ids[i],
                latency_ms=420 + i * 55,
            )
            if i == 2:  # 机器之心页面反爬，jina 降级到 crawl4ai 后成功
                log.add(
                    base + 0.3,
                    EventType.degrade,
                    {
                        "from": "jina",
                        "to": "crawl4ai",
                        "error": "jina: content too short (84 chars)",
                        "url": SOURCES[3][0],
                    },
                    sub_task_id=sub_ids[i],
                )
            for k, (src_idx, provider) in enumerate(FETCH_PLAN[i]):
                url, title_s, domain, cred, _ = SOURCES[src_idx]
                latency = 3100 if provider == "crawl4ai" else 900 + k * 350
                log.add(
                    base + 0.4 + k * 0.5,
                    EventType.fetch,
                    {
                        "url": url,
                        "provider": provider,
                        "title": title_s,
                        "chars": FETCH_CHARS[src_idx],
                        "latency_ms": latency,
                        "domain": domain,
                        "credibility": cred,
                    },
                    sub_task_id=sub_ids[i],
                    latency_ms=latency,
                )
            log.llm(
                base + 3.0,
                EventType.note,
                NOTE_USAGE,
                reasoner=False,
                payload={
                    "summary_chars": 240 + i * 15,
                    "facts": 6,
                    "gaps": [],
                    "sources_used": len(FETCH_PLAN[i]),
                },
                sub_task_id=sub_ids[i],
            )

        log.llm(
            25.0,
            EventType.reflect,
            REFLECT_USAGE,
            reasoner=False,
            payload={
                "round": 1,
                "assessment": "六大主线素材完整，但监管章节缺少欧盟 MDR 对照视角，无法回答跨境审批差异问题",
                "has_gaps": True,
                "gap_titles": [SUB_R2],
                "instructions": 0,
                "created": 1,
                "next_round": 2,
            },
        )

        # ---- R2 补搜：关键词与 R1 监管子任务相同 → 搜索命中缓存，页面重读走缓存 ----
        log.add(
            26.0,
            EventType.search,
            {
                "query": KEYWORDS[0],
                "provider": "cache",
                "hits": 8,
                "titles": [SOURCES[0][1], SOURCES[1][1], SOURCES[7][1]],
            },
            sub_task_id=sub_ids[6],
            latency_ms=35,
        )
        for k, (src_idx, provider) in enumerate(FETCH_PLAN[6]):
            url, title_s, domain, cred, _ = SOURCES[src_idx]
            log.add(
                26.4 + k * 0.5,
                EventType.fetch,
                {
                    "url": url,
                    "provider": provider,
                    "title": title_s,
                    "chars": FETCH_CHARS[src_idx],
                    "latency_ms": 120 + k * 40,
                    "domain": domain,
                    "credibility": cred,
                },
                sub_task_id=sub_ids[6],
                latency_ms=120 + k * 40,
            )
        log.llm(
            29.0,
            EventType.note,
            NOTE_R2_USAGE,
            reasoner=False,
            payload={
                "summary_chars": 330,
                "facts": 6,
                "gaps": [],
                "sources_used": len(FETCH_PLAN[6]),
            },
            sub_task_id=sub_ids[6],
        )

        log.llm(
            30.0,
            EventType.reflect,
            REFLECT_R2_USAGE,
            reasoner=False,
            payload={
                "round": 2,
                "assessment": "补充任务已完成监管对照素材，信息密度足够进入综合阶段",
                "has_gaps": False,
                "gap_titles": [],
                "instructions": 0,
                "created": 0,
                "next_round": None,
            },
        )

        log.llm(
            33.0,
            EventType.synthesize,
            SYNTH_USAGE,
            reasoner=True,
            payload={
                "report_id": report.id,
                "chars": len(REPORT),
                "citations": len(src_ids),
                "notes": len(NOTES),
                "sources": len(src_ids),
                "budget_degraded": False,
            },
        )
        log.add(33.2, EventType.budget, budget_payload("synthesize", log.token_used(), BUDGET))

        user_q = "海外医疗器械厂商的 AI 诊断产品进入中国，也要重新走三类证审批吗？"
        assistant_a = (
            f"需要。境外厂商的 AI 诊断软件同样必须取得 NMPA 三类证方可在中国上市销售，"
            f"监管要求与本土产品一致[{src_ids[0]}]。区别在于：进口产品可以申请优先审批通道，"
            f"但需额外提交原产国上市证明与境内临床评价资料，整体周期通常比本土厂商多 2-3 个月[{src_ids[1]}]。"
        )
        user_msg = ChatMessage(
            task_id=task.id,
            role=ChatRole.user,
            content=user_q,
            created_at=t0 + timedelta(minutes=50.0),
        )
        session.add(user_msg)
        await session.flush()
        assistant_msg = ChatMessage(
            task_id=task.id,
            role=ChatRole.assistant,
            content=assistant_a,
            cited_source_ids=[src_ids[0], src_ids[1]],
            created_at=t0 + timedelta(minutes=50.2),
        )
        session.add(assistant_msg)
        await session.flush()
        log.llm(
            50.2,
            EventType.chat,
            CHAT_USAGE,
            reasoner=False,
            payload={
                "message_id": assistant_msg.id,
                "question_chars": len(user_q),
                "answer_chars": len(assistant_a),
                "cited": 2,
            },
        )

        log.commit(session)

        task.token_used = log.token_used()
        task.cost_cny = log.cost_cny()
        await session.commit()
        print(
            f"seeded done-task id={task.id}: {len(src_ids)} sources, {len(CHARTS)} charts, "
            f"{log.seq} events, tokens={task.token_used}, cost=¥{task.cost_cny}"
        )


if __name__ == "__main__":
    asyncio.run(main())
