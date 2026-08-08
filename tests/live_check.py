"""真实 LLM 联调检查（消耗少量 API 额度）。

用法：
  python tests\live_check.py ["对小助说的话"]

**永远 spawn 独立后端**：隔离临时数据目录 + 独立端口（18773），
绝不探测/复用可能在跑的真实后端（18771 开发 / 18772 release），
防止测试消息污染真实数据。
校验 /state 的 mode=="llm"（若是 mock 立刻报错退出，防止把 mock 误判成真实联调成功）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 18773
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get("PLANNER_PYTHON") or shutil.which("python") or "python"
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
    data_root = Path(tempfile.mkdtemp(prefix="planner_live_check_"))
    env = dict(os.environ)
    env["PLANNER_DATA_ROOT"] = str(data_root)   # 隔离数据，绝不碰真实 data/
    env["PLANNER_PORT"] = str(PORT)
    env.pop("PLANNER_MOCK_LLM", None)           # 联调必须真实 LLM
    env.pop("PLANNER_URL", None)
    proc = subprocess.Popen(
        [PYTHON, "-m", "planner"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                data = get("/state")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print(f"后端 {PORT} 40 秒内未启动")
            return 1

        if data["state"]["mode"] != "llm":
            print(f"后端是 mock 模式（mode={data['state']['mode']}），拒绝继续")
            return 1

        print(f"后端: 真实 LLM 模式（隔离数据 {data_root}）")
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
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
