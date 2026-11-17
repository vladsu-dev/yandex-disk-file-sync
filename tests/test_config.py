"""Проверки аргументов запуска и цикла ожидания"""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from file_sync.__main__ import run
from file_sync.config import ConfigError, read_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = folder.name
        os.mkdir(os.path.join(self.base, "files"))
        self.env = os.path.join(self.base, ".env")

    def settings(self, **overrides):
        values = {
            "LOCAL_DIR": "files",
            "REMOTE_DIR": "Задание",
            "YANDEX_DISK_TOKEN": "test-secret",
            "SYNC_INTERVAL": "0.5",
            "LOG_FILE": "logs/sync.log",
        }
        values.update(overrides)
        with open(self.env, "w", encoding="utf-8") as stream:
            stream.writelines(
                f"{key}={value}\n" for key, value in values.items()
            )
        return read_settings(self.env)

    def test_reads_relative_paths_and_hides_token(self):
        settings = self.settings()
        self.assertEqual(
            settings.local_dir, os.path.realpath(f"{self.base}/files")
        )
        self.assertEqual(settings.remote_dir, "disk:/Задание")
        self.assertEqual(settings.interval, 0.5)
        self.assertNotIn("test-secret", repr(settings))

    def test_rejects_missing_directory(self):
        with self.assertRaisesRegex(ConfigError, "LOCAL_DIR"):
            self.settings(LOCAL_DIR="missing")

    def test_rejects_cloud_root(self):
        with self.assertRaisesRegex(ConfigError, "REMOTE_DIR"):
            self.settings(REMOTE_DIR="disk:/")

    def test_rejects_log_inside_synced_directory(self):
        with self.assertRaisesRegex(ConfigError, "LOG_FILE"):
            self.settings(LOG_FILE="files/sync.log")

    def test_rejects_invalid_period(self):
        for value in ("inf", "nan", "-1", "0", "later"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigError, "SYNC_INTERVAL"),
            ):
                self.settings(SYNC_INTERVAL=value)

    def test_next_iteration_runs_after_unsuccessful_cycle(self):
        settings = self.settings()
        with (
            patch(
                "file_sync.__main__.synchronize",
                side_effect=[False, KeyboardInterrupt],
            ) as sync,
            patch("file_sync.__main__.time.sleep") as sleep,
            self.assertRaises(KeyboardInterrupt),
        ):
            run(settings, Mock(), Mock(), once=False)
        self.assertEqual(sync.call_count, 2)
        sleep.assert_called_once_with(settings.interval)

    def test_start_writes_startup_and_token_error_to_file(self):
        from pathlib import Path

        from file_sync.__main__ import start
        from file_sync.storage import CloudConfigError

        settings = self.settings()
        with patch("file_sync.__main__.YandexDisk") as cloud:
            cloud.return_value.get_info.side_effect = CloudConfigError(
                "Проверьте YANDEX_DISK_TOKEN",
            )
            self.assertEqual(start(settings, once=True), 2)
            cloud.return_value.close.assert_called_once()

        log = Path(settings.log_file).read_text(encoding="utf-8")
        self.assertIn("INFO", log)
        self.assertIn("ERROR", log)
        self.assertIn(settings.local_dir, log)
        self.assertIn("YANDEX_DISK_TOKEN", log)
        self.assertNotIn(settings.token, log)
