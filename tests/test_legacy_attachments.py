import json
import threading
from pathlib import Path

import urllib.request

from planner.server import create_server
from planner.session import PlannerSession

from tests.test_attachments import PNG_1PX


def test_legacy_history_message_parses_image_and_hides_description(data_root, tmp_path):
    """v0.1.295 之前的用户消息：旧注入说明在 /history 里要变回图片，且正文不显示路径。"""
    img = tmp_path / "shot.png"
    img.write_bytes(PNG_1PX)
    s = PlannerSession(data_root, mock=True)
    try:
        content = (
            "[2026-10-22 10:52] 用户 对你说：看视频里的这个\n\n"
            "【拖入的文件】\n"
            f"- 图片 shot.png「{img}」（已发送原图给模型）"
        )
        s._receive(content, trigger=False)   # 模拟旧消息：没有 display_text/attachments
        httpd = create_server(s, port=0)
        try:
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{httpd.server_address[1]}/history",
                    timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        finally:
            httpd.shutdown()
            httpd.server_close()
        user = [m for m in data["messages"] if m["role"] == "user"][-1]
        assert user["content"] == "看视频里的这个"
        assert "【拖入的文件】" not in user["content"]
        assert user["attachments"][0]["name"] == "shot.png"
        assert user["attachments"][0]["url"].startswith("/file?path=")
        assert list((Path(data_root) / "attachments").glob("legacy-*.png"))
    finally:
        s.close()
