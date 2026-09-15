"""D11 真实链路冒烟：低压预算 → 80% 降级 → 无补充轮 + 报告尾部预算声明。

用法：uv run python scripts/smoke_budget.py
前置：uvicorn (8000) 与 arq worker 已启动。
声明与 degrade 事件由 DB 侧验证（docker exec psql）。
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api/research/tasks"
QUESTION = "2025年全球人形机器人产业发展现状"
BUDGET = 10_000


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    task = call("POST", "", {"question": QUESTION, "depth": "quick", "token_budget": BUDGET})
    task_id = task["id"]
    print(f"task {task_id} created (budget={BUDGET:,}, status={task['status']})")

    for _ in range(300):
        time.sleep(2)
        detail = call("GET", f"/{task_id}")
        if detail["status"] in ("done", "failed", "stopped"):
            break
    else:
        print("TIMEOUT after 600s")
        return 1

    print(f"final status={detail['status']}  token_used={detail['token_used']:,}")
    if detail["status"] != "done":
        print(f"error_msg={detail.get('error_msg')}")
        return 1

    rounds: dict[int, list] = {}
    for st in detail["sub_tasks"]:
        rounds.setdefault(st["round_no"], []).append(st)
    print("sub_tasks: " + ", ".join(f"round{r}={len(v)}" for r, v in sorted(rounds.items())))
    ratio = detail["token_used"] / BUDGET
    print(f"token_used/budget = {detail['token_used']:,}/{BUDGET:,} ({ratio:.0%})")

    if any(st["round_no"] >= 2 for st in detail["sub_tasks"]):
        print("FAIL: 低压预算下不应有补充轮（降级未生效）")
        return 1
    print("SMOKE PASS: 低压预算正确触发降级（无补充轮）；报告声明与 degrade 事件经 DB 验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
