from app.tools.credibility import credibility_for_url


def test_exact_match():
    c = credibility_for_url("https://www.caixin.com/2025/article.html")
    assert c.domain == "caixin.com"
    assert c.score == 4
    assert c.rule == "exact"


def test_suffix_match_longest_wins():
    # stats.gov.cn 走 gov.cn 后缀规则，而非任何更短的表项
    c = credibility_for_url("https://data.stats.gov.cn/easyquery.htm")
    assert c.score == 5
    assert c.rule == "suffix"

    # blog.csdn.net 命中 csdn.net
    c2 = credibility_for_url("https://blog.csdn.net/user/article/123")
    assert c2.domain == "csdn.net"
    assert c2.score == 2


def test_tld_rule():
    # edu.cn 在表内，走 suffix 命中，同为 5 分
    c = credibility_for_url("https://www.pku.edu.cn/admissions")
    assert c.score == 5
    assert c.rule == "suffix"

    # .gov 表外后缀，走 TLD 规则
    c2 = credibility_for_url("https://portal.nih.gov/research")
    assert c2.score == 5
    assert c2.rule == "tld"


def test_baijiahao_exact_over_generic():
    c = credibility_for_url("https://baijiahao.baidu.com/s?id=123")
    assert c.domain == "baijiahao.baidu.com"
    assert c.score == 1


def test_unknown_domain_defaults():
    c = credibility_for_url("https://www.some-random-site-xyz.com/page")
    assert c.score == 2
    assert c.rule == "default"
    assert c.domain == "some-random-site-xyz.com"


def test_invalid_url():
    c = credibility_for_url("not a url")
    assert c.score == 2
    assert c.domain == ""


def test_score_range():
    for url in [
        "https://arxiv.org/abs/2501.00001",
        "https://www.thepaper.cn/newsDetail_forward_123",
        "https://36kr.com/p/123",
        "https://www.zhihu.com/question/123",
        "https://weibo.com/abc/def",
    ]:
        c = credibility_for_url(url)
        assert 1 <= c.score <= 5, f"{url} -> {c.score}"
