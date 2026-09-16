from app.services.excerpt import extract_excerpts


def test_extracts_sentences_citing_source():
    notes = [
        (
            "市场规模达 120 亿元[37]，其中医疗影像占 31%[38]。"
            "头部厂商加速布局[37]。\n监管政策仍在完善[39]。"
        ),
        "另一条笔记与该信源无关[40]。",
    ]
    out = extract_excerpts(notes, 37)
    # 句读保留；句内其他信源锚点（[38]）一并剥除，卡片只留纯文本
    assert out == ["市场规模达 120 亿元，其中医疗影像占 31%。", "头部厂商加速布局。"]


def test_dedupes_and_caps_sentence_count():
    note = "结论甲[5]。结论甲[5]。结论乙[5]。结论丙[5]。结论丁[5]。"
    out = extract_excerpts([note], 5)
    assert out == ["结论甲。", "结论乙。", "结论丙。"]


def test_no_match_returns_empty():
    assert extract_excerpts(["只有 [6] 的笔记"], 7) == []
    assert extract_excerpts([], 7) == []


def test_anchor_substring_not_confused():
    """[3] 不应匹配 [30] / [13] 的句子。"""
    note = "三十号信源的内容[30]。十三号信源的内容[13]。"
    assert extract_excerpts([note], 3) == []


def test_multiline_note_splits_on_newline():
    note = "第一行讲成本下降到 0.03 元/千 token[9]\n第二行与目标无关。第二段也提到成本[9]"
    out = extract_excerpts([note], 9)
    assert len(out) == 2
    assert "0.03" in out[0]
