"""D10 真实链路冒烟：追加指示 → reflect 消费 → 补充轮执行 → 报告融合。

用法：uv run python scripts/smoke_reflect.py
前置：uvicorn (8000) 与 arq worker 已启动，DB 已迁移到 c8e41b7f2d90。
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api/research/tasks"
QUESTION = "2025年中国新能源汽车出口格局分析"
INSTRUCTION = "请补充 2025 年中国新能源汽车出口到欧洲市场的最新销量数据和关税政策变化"


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
    task = call("POST", "", {"question": QUESTION, "depth": "quick"})
    task_id = task["id"]
    print(f"task {task_id} created (status={task['status']})")

    instruction = None
    for _ in range(300):
        time.sleep(2)
        detail = call("GET", f"/{task_id}")
        status = detail["status"]
        if status == "running" and instruction is None:
            instruction = call("POST", f"/{task_id}/instructions", {"text": INSTRUCTION})
            print(f"instruction #{instruction['id']} queued: {INSTRUCTION[:30]}...")
        if status in ("done", "failed", "stopped"):
            break
    else:
        print("TIMEOUT after 600s")
        return 1

    print(f"final status={detail['status']}  token_used={detail['token_used']}  cost=¥{detail['cost_cny']:.4f}")
    if detail["status"] != "done":
        print(f"error_msg={detail.get('error_msg')}")
        return 1

    rounds = {}
    for st in detail["sub_tasks"]:
        rounds.setdefault(st["round_no"], []).append(st)
    print(f"sub_tasks: " + ", ".join(f"round{r}={len(v)}({sum(1 for s in v if s['status']=='done')}done)" for r, v in sorted(rounds.items())))

    supplemented = detail["sub_tasks"] and any(st["round_no"] >= 2 for st in detail["sub_tasks"])
    consumed = detail.get("instructions") or []
    print(f"instructions consumed: {[(i['id'], i['consumed_round']) for i in consumed]}")

    ok = True
    if not supplemented:
        print("FAIL: no supplementary round sub tasks (round_no>=2)")
        ok = False
    if not any(i["consumed_round"] for i in consumed):
        print("FAIL: instruction never consumed")
        ok = False
    if not ok:
        return 1
    print("SMOKE PASS: 追加指示进入补充轮并被消费")
    return 0


if __name__ == "__main__":
    sys.exit(main())
