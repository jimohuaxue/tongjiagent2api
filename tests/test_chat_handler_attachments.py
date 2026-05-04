import unittest

from core.api.chat_handler import _select_attachments_for_request
from core.api.schemas import InputAttachment, OpenAIChatRequest, OpenAIMessage


def _attachment(name: str) -> InputAttachment:
    return InputAttachment(filename=name, mime_type="image/png", data=name.encode())


class TestChatHandlerAttachments(unittest.TestCase):
    def test_full_history_prefers_last_user_attachment_when_present(self) -> None:
        req = OpenAIChatRequest(
            model="qwen3-vl-235b",
            messages=[OpenAIMessage(role="user", content="这是什么")],
            attachment_files_last_user=[_attachment("banana.png")],
            attachment_files_all_users=[
                _attachment("anime.png"),
                _attachment("banana.png"),
            ],
        )

        attachments, source = _select_attachments_for_request(req, full_history=True)

        self.assertEqual(source, "last_user")
        self.assertEqual([att.filename for att in attachments], ["banana.png"])

    def test_full_history_uses_all_attachments_only_when_last_user_has_none(self) -> None:
        req = OpenAIChatRequest(
            model="qwen3-vl-235b",
            messages=[OpenAIMessage(role="user", content="继续")],
            attachment_files_all_users=[_attachment("anime.png")],
        )

        attachments, source = _select_attachments_for_request(req, full_history=True)

        self.assertEqual(source, "all_users")
        self.assertEqual([att.filename for att in attachments], ["anime.png"])

    def test_reused_session_without_last_user_attachment_sends_none(self) -> None:
        req = OpenAIChatRequest(
            model="qwen3-vl-235b",
            messages=[OpenAIMessage(role="user", content="继续")],
            attachment_files_all_users=[_attachment("anime.png")],
        )

        attachments, source = _select_attachments_for_request(req, full_history=False)

        self.assertEqual(source, "none")
        self.assertEqual(attachments, [])
