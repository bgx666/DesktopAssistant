"""回忆失败时的行为：**先查 → 查不到就照实说 → 并问一句线索**。

为什么值得钉住：
原来的提示词只有一句「不要张口就说"我不记得"」——本意是"先查再答"，
但字面效果是 discourages 承认想不起来，而且完全没有"追问线索"这条。
被禁止说不知道、又没有出口，模型最容易的路径就是**编一段听起来合理的往事**，
而编造记忆比说"我不知道"严重得多。

这个测试读**两端的提示词文件**，所以顺带能抓住"两端漂移"。
"""
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PROMPTS = {
    "pc": REPO / "src" / "planner" / "prompts" / "system.md",
    "android": REPO / "android" / "app" / "src" / "main" / "assets" / "prompts" / "system.md",
}


@pytest.fixture(params=sorted(PROMPTS))
def prompt_text(request) -> str:
    path = PROMPTS[request.param]
    assert path.exists(), f"提示词不存在：{path}"
    return path.read_text(encoding="utf-8")


def test_says_check_before_claiming_not_to_remember(prompt_text):
    """先查再答（保留原意）。"""
    assert "先看上下文里的 node 消息" in prompt_text
    assert "没查就说" in prompt_text


def test_gives_permission_to_admit_not_knowing(prompt_text):
    """**查完确实没有就照实说** —— 这是原来缺的那一半。"""
    assert "查完确实没有，就照实说" in prompt_text
    assert "这段我确实没印象" in prompt_text


def test_forbids_fabricating_memories(prompt_text):
    """明确禁止编造，并说清为什么。"""
    assert "不要为了显得记得而编" in prompt_text
    assert "编出一段没发生过的往事" in prompt_text


def test_tells_it_to_ask_for_a_clue(prompt_text):
    """追问线索 —— 原来完全没有这条。"""
    assert "问他一句能帮你定位的线索" in prompt_text
    # 线索要具体，别只说"再想想"
    for hint in ("大概什么时候", "当时在聊什么", "跟谁有关"):
        assert hint in prompt_text


def test_both_ends_share_the_same_wording():
    """两端的这三句必须**逐字相同**，否则行为会漂。"""
    pc = PROMPTS["pc"].read_text(encoding="utf-8")
    android = PROMPTS["android"].read_text(encoding="utf-8")
    for line in pc.splitlines():
        if "查完确实没有" in line or "问他一句能帮你定位的线索" in line:
            assert line in android, f"安卓侧缺少同一句：{line[:60]}"
