import unittest

from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache


class TestSessionCacheClientConversation(unittest.TestCase):
    def test_client_conversation_maps_to_session(self) -> None:
        cache = SessionCache()
        proxy_key = ProxyKey("", "", "fp-1", False)

        cache.put("web-session-1", proxy_key, "claude", "fp-1:acc")
        cache.bind_client_conversation("claude", "chat-1", "web-session-1")

        self.assertEqual(
            cache.get_by_client_conversation("claude", "chat-1"),
            "web-session-1",
        )
        self.assertIsNone(cache.get_by_client_conversation("tongji", "chat-1"))

    def test_client_conversation_binding_is_removed_when_session_deleted(self) -> None:
        cache = SessionCache()
        proxy_key = ProxyKey("", "", "fp-1", False)

        cache.put("web-session-1", proxy_key, "claude", "fp-1:acc")
        cache.bind_client_conversation("claude", "chat-1", "web-session-1")
        cache.delete("web-session-1")

        self.assertIsNone(cache.get_by_client_conversation("claude", "chat-1"))

    def test_stale_client_conversation_binding_is_ignored(self) -> None:
        cache = SessionCache()

        cache.bind_client_conversation("claude", "chat-1", "missing-session")

        self.assertIsNone(cache.get_by_client_conversation("claude", "chat-1"))


if __name__ == "__main__":
    unittest.main()
