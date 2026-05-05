import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import constants


class TestChromiumResolution(unittest.TestCase):
    def test_configured_path_wins(self) -> None:
        self.assertEqual(
            constants.resolve_chromium_bin("/custom/chromium"),
            "/custom/chromium",
        )

    def test_env_path_is_used_when_config_is_blank(self) -> None:
        with mock.patch.dict(
            os.environ,
            {constants.CHROMIUM_BIN_ENV_KEY: "/env/chromium"},
            clear=False,
        ):
            self.assertEqual(constants.resolve_chromium_bin(""), "/env/chromium")

    def test_path_command_is_resolved_for_configured_value(self) -> None:
        with mock.patch("shutil.which", return_value="/usr/bin/chromium"):
            self.assertEqual(
                constants.resolve_chromium_bin("chromium"),
                "/usr/bin/chromium",
            )

    def test_common_executable_is_detected_before_platform_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            chrome = Path(tmpdir) / "chrome"
            chrome.write_text("#!/bin/sh\n", encoding="utf-8")
            chrome.chmod(0o755)
            with mock.patch.object(
                constants,
                "chromium_bin_candidates",
                return_value=[str(chrome), "/missing/chromium"],
            ):
                self.assertEqual(constants.resolve_chromium_bin(""), str(chrome))

    def test_blank_config_on_linux_does_not_fallback_to_macos_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {constants.CHROMIUM_BIN_ENV_KEY: ""},
            clear=False,
        ):
            with mock.patch("platform.system", return_value="Linux"):
                with mock.patch("shutil.which", return_value=None):
                    with mock.patch.object(
                        constants,
                        "is_chromium_executable",
                        return_value=False,
                    ):
                        resolved = constants.resolve_chromium_bin("")

        self.assertNotEqual(resolved, constants.MACOS_CHROMIUM_BIN)
        self.assertEqual(resolved, constants.LINUX_FINGERPRINT_CHROMIUM_BIN)


if __name__ == "__main__":
    unittest.main()
