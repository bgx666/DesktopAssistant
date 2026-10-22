import base64
from pathlib import Path

from planner.session import PlannerSession

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_user_attachments_meta_and_display_text(data_root, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(PNG_1PX)
    txt = tmp_path / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    s = PlannerSession(data_root, mock=True)
    try:
        r = s.enqueue_player_message(
            "看附件",
            [
                {"name": "note.txt", "path": str(txt), "kind": "text",
                 "content": "hello"},
                {"name": "pic.png", "path": str(img), "kind": "image"},
            ],
            with_meta=True,
        )
        assert isinstance(r, dict) and r["msg_id"]
        atts = r["attachments"]
        assert [a["name"] for a in atts] == ["note.txt", "pic.png"]
        assert atts[0]["kind"] == "text" and atts[0]["path"] == str(txt)
        assert atts[1]["kind"] == "image"
        assert atts[1]["url"].startswith("/file?path=")
        files = list((Path(data_root) / "attachments").glob("user-*.png"))
        assert files, "用户图片应复制到 data/attachments"
        msg = next(m for m in s.recent_buffer
                   if getattr(m, "id", None) == r["msg_id"])
        assert msg.metadata["display_text"] == "看附件"
        assert msg.metadata["attachments"][1]["url"].startswith("/file?path=")
    finally:
        s.close()
