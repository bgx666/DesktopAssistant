"""真实 LLM 联调检查（消耗少量 API 额度）。

用法：
  D:\Miniconda3\python.exe tests\live_check.py ["对小助说的话"]

先探测 18771：已在跑就直接用（不 spawn），没在跑才 spawn 真实模式后端。
校验 /state 的 mode=="llm"（若是 mock 立刻报错退出，防止把 mock 误判成真实联调成功）。
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18771"
ROOT = Path(__file__).resolve().parent.parent
PYTHON = r"D:\Miniconda3\python.exe"
MESSAGE = sys.argv[1] if len(sys.argv) > 1 else "帮我安排一下最近的复习计划"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    spawned = False
    try:
        data = get("/state")
    except Exception:
        spawned = True
        proc = subprocess.Popen(
            [PYTHON, "-m", "planner"], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                data = get("/state")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("后端 40 秒内未启动")
            return 1

    if data["state"]["mode"] != "llm":
        print(f"后端是 mock 模式（mode={data['state']['mode']}），拒绝继续——请启动真实模式后端或关掉 PLANNER_MOCK_LLM")
        if spawned:
            proc.terminate()
        return 1

    print("后端: 真实 LLM 模式，mode=llm")
    post("/chat", {"message": MESSAGE})
    print(f"已发送: {MESSAGE}")
    deadline = time.time() + 120
    texts = []
    while time.time() < deadline:
        time.sleep(0.8)
        try:
            d = get("/dequeue")
        except Exception:
            continue
        for ev in d["events"]:
            if ev["type"] == "text":
                texts.append(ev["content"])
        if any("拆解" in t or "任务" in t or "安排" in t for t in texts):
            break
    print("收到的回复（按轮次）：")
    for t in texts[:5]:
        print("-", t[:120])
    st = get("/state")
    print(f"\n任务统计: {st['state']['plan']['tasks']}")
    print(f"今日计划: {st['state']['plan']['today_plan_done']}/{st['state']['plan']['today_plan_total']}")

    if spawned:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
