import re

from app.engine.budget import (
    DEGRADE_AT,
    STAGE_QUOTAS,
    budget_payload,
    disclaimer_md,
    is_degraded,
    stage_quota,
)


def test_stage_quotas_sum_to_one():
    assert abs(sum(STAGE_QUOTAS.values()) - 1.0) < 1e-9


def test_stage_quota_matches_percentages():
    assert stage_quota(100_000, "plan") == 5_000
    assert stage_quota(100_000, "execute") == 70_000
    assert stage_quota(100_000, "reflect") == 10_000
    assert stage_quota(100_000, "synthesize") == 15_000


def test_is_degraded_thresholds():
    assert DEGRADE_AT == 0.80
    assert not is_degraded(79_999, 100_000)
    assert is_degraded(80_000, 100_000)  # 恰好阈值也算
    assert is_degraded(150_000, 100_000)  # 超支


def test_is_degraded_zero_budget_never_degrades():
    assert not is_degraded(999_999, 0)
    assert not is_degraded(999_999, -1)


def test_budget_payload_shape():
    p = budget_payload("execute", 85_000, 100_000)
    assert p == {
        "stage": "execute",
        "used": 85_000,
        "budget": 100_000,
        "quota": 70_000,
        "degraded": True,
    }


def test_disclaimer_contains_usage_numbers():
    md = disclaimer_md(85_000, 100_000)
    assert md.startswith("\n\n---")
    assert "85,000" in md
    assert "100,000" in md
    assert "预算受限说明" in md
    assert not re.search(r"\[\d+\]", md)  # 无引用锚点，追加不破坏重编号
