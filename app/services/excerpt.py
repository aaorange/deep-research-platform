"""信源摘录提取：报告阅读页信源卡的「摘录」数据来源。

笔记正文中的 [N] 锚点已被 persister 重写为信源库 id（如 [37]）；对给定
信源 id，从全部笔记中抽取包含该锚点的句子作为摘录——即「Agent 从这个
信源记录下的内容」，比原始网页开头更有信息量。句子内的其他锚点剥除，
重复句子去重，单信源最多 3 句、共 240 字。
"""

import re

ANCHOR_RE = re.compile(r"\[(\d+)\]")

MAX_SENTENCES = 3
MAX_CHARS = 240

# 中文句读断句：句号/叹号/问号/分号/换行后切分，保留分隔符
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _clean(sentence: str) -> str:
    return ANCHOR_RE.sub("", sentence).replace("  ", " ").strip(" ，,；;")


def extract_excerpts(note_contents: list[str], source_id: int) -> list[str]:
    """从笔记正文中提取引用了 source_id 的句子（去锚点、去重、限量）。"""
    anchor = f"[{source_id}]"
    excerpts: list[str] = []
    seen: set[str] = set()
    total = 0
    for content in note_contents:
        if anchor not in content:
            continue
        for sentence in _SENTENCE_SPLIT.split(content):
            if anchor not in sentence:
                continue
            text = _clean(sentence)
            if len(text) < 4 or text in seen:
                continue
            seen.add(text)
            if total + len(text) > MAX_CHARS and excerpts:
                break
            excerpts.append(text)
            total += len(text)
            if len(excerpts) >= MAX_SENTENCES:
                break
        if len(excerpts) >= MAX_SENTENCES:
            break
    return excerpts
