"""演示数据：造一个已完成任务（子任务/信源/笔记/报告），供报告阅读页验收。

数据形态与引擎真实落库一致：笔记正文的 [N] 锚点是信源库 id（persister
重写后的格式），报告 markdown 的 [N] 是展示编号，二者通过 citation_map
映射。运行：uv run python scripts/seed_report_demo.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Note, Report, ResearchTask, Source, SubTask, SubTaskStatus, TaskStatus
from app.db.base import SessionLocal

SUBS = [
    "医疗大模型监管政策与三类证审批进展",
    "医学影像与辅助诊断场景落地情况",
    "临床决策支持与电子病历智能化",
    "药企研发与蛋白质结构预测应用",
    "医疗大模型市场格局与商业化模式",
    "数据合规与医疗隐私计算挑战",
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

2025 年中国医疗大模型市场规模预计 82 亿元，头部厂商合计份额接近七成，商业模式以「按年订阅 + 调用计费」混合为主[5]。付费意愿两级分化明显：三级医院贡献约 75% 的收入，基层机构仍以财政项目制为主[5]。

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


async def main() -> None:
    async with SessionLocal() as session:
        task = ResearchTask(
            question="2025 年国产大模型在医疗行业的落地情况",
            status=TaskStatus.done,
            depth="std",
            token_budget=80_000,
            token_used=52_318,
            cost_cny=0.47,
        )
        session.add(task)
        await session.flush()

        sub_ids = []
        for title in SUBS:
            sub = SubTask(task_id=task.id, title=title, status=SubTaskStatus.done, round_no=1)
            session.add(sub)
            await session.flush()
            sub_ids.append(sub.id)

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
        session.add(
            Report(
                task_id=task.id,
                version=1,
                markdown=REPORT,
                citation_map=citation_map,
                chart_specs=CHARTS,
                token_total=9_864,
            )
        )
        await session.commit()
        print(f"seeded done-task id={task.id} with {len(src_ids)} sources, {len(CHARTS)} charts")


if __name__ == "__main__":
    asyncio.run(main())
