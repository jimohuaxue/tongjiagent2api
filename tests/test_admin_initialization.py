import tempfile
import unittest
from pathlib import Path

import yaml

from core.api import auth
from core.config import settings


class TestAdminInitialization(unittest.TestCase):
    def setUp(self) -> None:
        self._original_config_path = settings._CONFIG_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        settings._CONFIG_PATH = Path(self._tmpdir.name) / "config.yaml"
        settings.reset_cache()

    def tearDown(self) -> None:
        settings._CONFIG_PATH = self._original_config_path
        settings.reset_cache()
        self._tmpdir.cleanup()

    def test_blank_api_key_uses_default_key(self) -> None:
        settings._CONFIG_PATH.write_text("auth:\n  api_key: ''\n", encoding="utf-8")
        settings.reset_cache()

        self.assertEqual(auth.configured_api_keys(), [auth.DEFAULT_API_KEY])
        self.assertEqual(auth.configured_api_key_text(), auth.DEFAULT_API_KEY)

    def test_set_api_key_writes_default_when_blank(self) -> None:
        settings._CONFIG_PATH.write_text("server:\n  port: 9000\n", encoding="utf-8")
        settings.reset_cache()

        self.assertEqual(auth.set_api_key(""), auth.DEFAULT_API_KEY)
        payload = yaml.safe_load(settings._CONFIG_PATH.read_text(encoding="utf-8"))

        self.assertEqual(payload["auth"]["api_key"], auth.DEFAULT_API_KEY)
        self.assertEqual(auth.configured_api_keys(), [auth.DEFAULT_API_KEY])

    def test_set_config_secret_initializes_login_with_hash(self) -> None:
        settings._CONFIG_PATH.write_text("auth:\n  config_secret: ''\n", encoding="utf-8")
        settings.reset_cache()

        encoded = auth.set_config_secret("local-secret")
        payload = yaml.safe_load(settings._CONFIG_PATH.read_text(encoding="utf-8"))

        self.assertTrue(encoded.startswith(f"{auth.CONFIG_SECRET_PREFIX}$"))
        self.assertEqual(payload["auth"]["config_secret"], encoded)
        self.assertTrue(auth.config_login_enabled())
        self.assertTrue(auth.verify_config_secret("local-secret", encoded))


if __name__ == "__main__":
    unittest.main()
