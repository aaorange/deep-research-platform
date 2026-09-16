"""D12 真实链路冒烟：SSE 事件流（Redis pub/sub 实时 + 心跳 + Last-Event-ID 断线重连增量补发）。

用法：uv run python scripts/smoke_sse.py
前置：uvicorn (8000) 与 arq worker 已启动。

验收：断开重连后按序号增量补发，界面不丢动作——
两段连接合计覆盖 seq 1..N 无缺口、无重复；观察到心跳行；收到终态 end 事件。
"""

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000/api/research/tasks"
QUESTION = "2025年国内新能源汽车出海欧洲的市场格局与政策风险"

HEARTBEAT_TIMEOUT = 30.0


def parse_event(lines: list[str]) -> dict | None:
    """把一组 SSE 行（id/event/data）解析为事件 dict；非 data 块返回 None。"""
    for ln in lines:
        if ln.startswith("data: "):
            try:
                return json.loads(ln[6:])
            except json.JSONDecodeError:
                return None
    return None


def read_stream(
    url: str,
    headers: dict | None = None,
    stop_at: int | None = None,
    want_end: bool = False,
    deadline: float = 120.0,
) -> tuple[list[int], bool, bool]:
    """读 SSE 流。返回（seq 列表, 是否见 end, 是否见心跳）。

    stop_at：收到该条数含 seq 的 data 事件后主动断开（模拟断线）。
    want_end：读到 event: end 后结束（正常完成路径）。
    """
    seqs: list[int] = []
    saw_end = False
    saw_hb = False
    t0 = time.monotonic()

    with (
        httpx.Client(timeout=HEARTBEAT_TIMEOUT) as client,
        client.stream("GET", url, headers=headers or {}) as resp,
    ):
        if resp.status_code != 200:
            print(f"FAIL: SSE 状态码 {resp.status_code}")
            sys.exit(1)
        buf: list[str] = []
        for line in resp.iter_lines():
            if time.monotonic() - t0 > deadline:
                print("FAIL: 读流超时")
                sys.exit(1)
            if line == "":
                if buf:
                    if any(ln == "event: end" for ln in buf):
                        saw_end = True
                        buf = []
                        if want_end:
                            return seqs, True, saw_hb
                    ev = parse_event(buf)
                    if ev and "seq" in ev:
                        seqs.append(ev["seq"])
                        if stop_at is not None and len(seqs) >= stop_at:
                            return seqs, saw_end, saw_hb
                    buf = []
                continue
            if line == ": heartbeat":
                saw_hb = True
            buf.append(line)
    return seqs, saw_end, saw_hb


def main() -> int:
    body = {"question": QUESTION, "depth": "quick"}
    with httpx.Client(timeout=30) as client:
        task = client.post(BASE, json=body).json()
    task_id = task["id"]
    print(f"task {task_id} created (status={task['status']})")

    # ---- 阶段 1：实时收流，收满 8 条事件后主动断开 ----
    stream_url = f"{BASE}/{task_id}/events/stream"
    seqs1, _, _ = read_stream(stream_url, stop_at=8, deadline=90)
    last_seq = seqs1[-1]
    print(f"[连接1] 断开前收到 {len(seqs1)} 条事件，last_seq={last_seq}")

    # ---- 阶段 2：模拟断线 15s（事件继续产生并落库） ----
    print("断线 15s（任务继续执行）...")
    time.sleep(15)

    # ---- 阶段 3：Last-Event-ID 重连，增量补发 + 持续到终态 ----
    seqs2, saw_end, saw_hb = read_stream(
        stream_url, headers={"Last-Event-ID": str(last_seq)}, want_end=True, deadline=180
    )
    print(f"[连接2] 重连补发+实时收到 {len(seqs2)} 条，见 end={saw_end}，见心跳={saw_hb}")

    if saw_end is False:
        print("FAIL: 未收到终态 end 事件")
        return 1

    all_seqs = seqs1 + seqs2
    n = len(all_seqs)
    if sorted(all_seqs) != list(range(1, n + 1)):
        print(f"FAIL: seq 不连续或有重复：{all_seqs}")
        return 1
    if seqs2 and seqs2[0] <= last_seq:
        print(f"FAIL: 重连未做增量补发（首条 {seqs2[0]} <= last_seq {last_seq}）")
        return 1
    if not saw_hb:
        print("WARN: 未观察到心跳行（事件密集时可能无心跳，非硬性失败）")

    with httpx.Client(timeout=30) as client:
        detail = client.get(f"{BASE}/{task_id}").json()
    print(f"final status={detail['status']}  events_total={n}  token_used={detail['token_used']:,}")
    if detail["status"] != "done":
        print(f"FAIL: 任务终态异常 {detail['status']}: {detail.get('error_msg')}")
        return 1

    print(f"SMOKE PASS: SSE 实时流 + Last-Event-ID 增量补发（1..{n} 无缺口无重复）+ 终态 end 事件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
