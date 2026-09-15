"""D11 断点续跑冒烟：执行中 kill worker → resume → 从断点继续不重复消耗。

用法（配合 shell 编排进程启停）：
  uv run python scripts/smoke_resume.py create            # 建任务，等 worker 认领
  <shell: kill 掉 arq worker 进程>
  uv run python scripts/smoke_resume.py resume <task_id>  # 残留 running → 重新入队
  <shell: 重启 arq worker>
  uv run python scripts/smoke_resume.py verify <task_id>  # 等完成并校验
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api/research/tasks"
QUESTION = "2025年折叠屏手机供应链格局分析"


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create() -> int:
    task = call("POST", "", {"question": QUESTION, "depth": "quick"})
    task_id = task["id"]
    print(f"task {task_id} created (status={task['status']})")
    for _ in range(60):
        time.sleep(1)
        detail = call("GET", f"/{task_id}")
        if detail["status"] == "running":
            print(f"task {task_id} running（worker 已认领，可 kill）")
            print(task_id)
            return 0
    print("worker 未在 60s 内认领任务")
    return 1


def resume(task_id: int) -> int:
    task = call("POST", f"/{task_id}/control", {"action": "resume"})
    print(f"resume: status={task['status']}（已重新入队）")
    return 0


def verify(task_id: int) -> int:
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

    subs = detail["sub_tasks"]
    titles = [s["title"] for s in subs]
    rounds: dict[int, list] = {}
    for st in subs:
        rounds.setdefault(st["round_no"], []).append(st)
    print(f"sub_tasks: {len(subs)} 个")
    print("rounds: " + ", ".join(f"round{r}={len(v)}" for r, v in sorted(rounds.items())))

    if len(titles) != len(set(titles)):
        print("FAIL: 子任务标题重复（plan 未幂等）")
        return 1
    if len(subs) < 3:
        print("FAIL: 子任务数量异常（plan 重复建或丢失）")
        return 1
    print("SMOKE PASS: kill 后 resume 完成任务，子任务无重复（笔记/计费经 DB 验证）")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "create":
        return create()
    if cmd == "resume":
        return resume(int(sys.argv[2]))
    if cmd == "verify":
        return verify(int(sys.argv[2]))
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
