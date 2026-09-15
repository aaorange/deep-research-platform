"""域名信誉度表：预置域名分级 + 后缀/TLD 规则 + 兜底默认值。

分级语义（写入 sources.credibility，1-5）：
  5 政府/监管/顶级学术：一手权威
  4 主流财经与综合媒体：编辑把关
  3 科技媒体/研究机构/官方技术博客：专业但可能有立场
  2 门户转载/UGC 社区：需交叉验证
  1 内容农场/自媒体聚合：仅作线索不作依据
"""

from dataclasses import dataclass
from urllib.parse import urlparse

DOMAIN_CREDIBILITY: dict[str, int] = {
    # 5 — 政府/监管/顶级学术
    "gov.cn": 5,
    "edu.cn": 5,
    "ac.cn": 5,
    "nature.com": 5,
    "science.org": 5,
    "arxiv.org": 5,
    "ieee.org": 5,
    "acm.org": 5,
    "who.int": 5,
    "imf.org": 5,
    "worldbank.org": 5,
    "oecd.org": 5,
    "nejm.org": 5,
    "thelancet.com": 5,
    "bmj.com": 5,
    "pnas.org": 5,
    # 4 — 主流财经/综合媒体
    "xinhuanet.com": 4,
    "news.cn": 4,
    "people.com.cn": 4,
    "cctv.com": 4,
    "chinadaily.com.cn": 4,
    "caixin.com": 4,
    "thepaper.cn": 4,
    "yicai.com": 4,
    "21jingji.com": 4,
    "ce.cn": 4,
    "stcn.com": 4,
    "cs.com.cn": 4,
    "reuters.com": 4,
    "bloomberg.com": 4,
    "ft.com": 4,
    "wsj.com": 4,
    "nytimes.com": 4,
    "economist.com": 4,
    "bbc.com": 4,
    "apnews.com": 4,
    "nikkei.com": 4,
    # 3 — 科技媒体/研究机构/官方技术博客
    "36kr.com": 3,
    "jiqizhixin.com": 3,
    "infoq.cn": 3,
    "ssrn.com": 3,
    "mckinsey.com": 3,
    "bcg.com": 3,
    "gartner.com": 3,
    "idc.com": 3,
    "deloitte.com": 3,
    "openai.com": 3,
    "anthropic.com": 3,
    "deepmind.google": 3,
    "huggingface.co": 3,
    "github.com": 3,
    "github.io": 3,
    "stackoverflow.com": 3,
    "techcrunch.com": 3,
    "theverge.com": 3,
    "wired.com": 3,
    "arstechnica.com": 3,
    "technologyreview.com": 3,
    "cloud.tencent.com": 3,
    "aliyun.com": 3,
    "huaweicloud.com": 3,
    "aws.amazon.com": 3,
    "azure.microsoft.com": 3,
    "developer.mozilla.org": 3,
    "python.org": 3,
    # 2 — 门户转载/UGC 社区
    "sina.com.cn": 2,
    "163.com": 2,
    "sohu.com": 2,
    "qq.com": 2,
    "ifeng.com": 2,
    "eastmoney.com": 2,
    "xueqiu.com": 2,
    "zhihu.com": 2,
    "csdn.net": 2,
    "cnblogs.com": 2,
    "jianshu.com": 2,
    "woshipm.com": 2,
    "sspai.com": 2,
    # 1 — 内容农场/自媒体聚合
    "baijiahao.baidu.com": 1,
    "weibo.com": 1,
    "weibo.cn": 1,
    "toutiao.com": 1,
    "360doc.com": 1,
}

# TLD 后缀规则：命中直接给满分（例：*.gov / *.edu.cn / *.gov.cn）
TLD_RULES: list[tuple[str, int]] = [
    (".gov.cn", 5),
    (".edu.cn", 5),
    (".ac.cn", 5),
    (".gov", 5),
    (".edu", 5),
    (".mil", 5),
]

DEFAULT_CREDIBILITY = 2


@dataclass
class DomainCredibility:
    domain: str
    score: int
    rule: str  # exact / suffix / tld / default


def credibility_for_url(url: str) -> DomainCredibility:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return DomainCredibility("", DEFAULT_CREDIBILITY, "default")

    best: tuple[str, int] | None = None
    for domain, score in DOMAIN_CREDIBILITY.items():
        if (host == domain or host.endswith("." + domain)) and (
            best is None or len(domain) > len(best[0])
        ):
            best = (domain, score)
    if best is not None:
        rule = "exact" if host == best[0] else "suffix"
        return DomainCredibility(best[0], best[1], rule)

    for suffix, score in TLD_RULES:
        if host.endswith(suffix):
            return DomainCredibility(host, score, "tld")

    return DomainCredibility(host, DEFAULT_CREDIBILITY, "default")
