"""Проверки синхронизации на временных файлах"""

import hashlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from file_sync.storage import CloudError, RemoteFile
from file_sync.sync import synchronize


class SyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = temp.name
        self.cloud = Mock()
        self.cloud.get_info.return_value = {}
        self.logger = Mock()

    def write(self, name, data):
        path = os.path.join(self.folder, name)
        with open(path, "wb") as file:
            file.write(data)
        return path

    def test_reconciles_all_direct_files(self):
        new = self.write("new.txt", b"new")
        changed = self.write("changed.txt", b"new")
        self.write("same.txt", b"same")
        self.cloud.get_info.return_value = {
            "same.txt": RemoteFile(sha256=hashlib.sha256(b"same").hexdigest()),
            "changed.txt": RemoteFile(
                sha256=hashlib.sha256(b"old").hexdigest()
            ),
            "extra.txt": RemoteFile(
                sha256=hashlib.sha256(b"extra").hexdigest()
            ),
        }

        self.assertTrue(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_called_once_with(new)
        self.cloud.reload.assert_called_once_with(changed)
        self.cloud.delete.assert_called_once_with("extra.txt")

    def test_bad_local_listing_prohibits_removal(self):
        self.cloud.get_info.return_value = {
            "extra.txt": RemoteFile(sha256="a" * 64)
        }
        with patch("file_sync.sync.list_names", side_effect=PermissionError):
            self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.delete.assert_not_called()

    def test_incomplete_cloud_listing_prohibits_writes(self):
        self.write("new.txt", b"data")
        self.cloud.get_info.side_effect = CloudError("Разрыв соединения")
        self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_not_called()
        self.cloud.delete.assert_not_called()

    def test_failure_does_not_skip_next_file_or_next_cycle(self):
        bad = self.write("a.txt", b"first")
        good = self.write("b.txt", b"second")
        self.cloud.load.side_effect = [CloudError("Интернет недоступен"), None]
        self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.assertEqual(
            [call.args[0] for call in self.cloud.load.call_args_list],
            [bad, good],
        )
        self.cloud.load.side_effect = None
        self.cloud.get_info.return_value = {
            "b.txt": RemoteFile(sha256=hashlib.sha256(b"second").hexdigest()),
        }
        self.assertTrue(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_called_with(bad)

    def test_subdirectory_and_symlink_are_ignored(self):
        self.write("target.txt", b"target")
        os.mkdir(os.path.join(self.folder, "directory"))
        os.symlink("target.txt", os.path.join(self.folder, "link.txt"))
        self.cloud.get_info.return_value = {
            "link.txt": RemoteFile(sha256="a" * 64),
            "directory": RemoteFile(sha256="b" * 64),
        }

        self.assertTrue(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_called_once_with(
            os.path.join(self.folder, "target.txt")
        )
        self.cloud.reload.assert_not_called()
        self.cloud.delete.assert_not_called()

    def test_unreadable_file_is_kept_and_other_files_are_processed(self):
        self.write("a.txt", b"local")
        good = self.write("b.txt", b"other")
        self.cloud.get_info.return_value = {
            "a.txt": RemoteFile(sha256="a" * 64),
        }
        with patch("file_sync.sync.file_digest", side_effect=PermissionError):
            self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.reload.assert_not_called()
        self.cloud.delete.assert_not_called()
        self.cloud.load.assert_called_once_with(good)

    def test_disappearing_source_folder_does_not_empty_cloud_folder(self):
        self.cloud.get_info.return_value = {
            "old.txt": RemoteFile(sha256="a" * 64),
        }

        def remove_local_directory():
            os.rmdir(self.folder)
            return self.cloud.get_info.return_value

        self.cloud.get_info.side_effect = remove_local_directory
        self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.delete.assert_not_called()

    def test_failed_upload_preserves_cloud_copy_of_renamed_file(self):
        new_path = self.write("renamed.txt", b"contents")
        self.cloud.get_info.return_value = {
            "old.txt": RemoteFile(sha256="a" * 64),
        }
        self.cloud.load.side_effect = CloudError("Недостаточно места")
        self.assertFalse(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_called_once_with(new_path)
        self.cloud.delete.assert_not_called()

        self.cloud.load.side_effect = None
        self.assertTrue(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.delete.assert_called_once_with("old.txt")

    def test_case_alias_does_not_keep_old_cloud_spelling(self):
        local = self.write("Report.txt", b"contents")
        self.cloud.get_info.return_value = {
            "report.txt": RemoteFile(sha256="a" * 64),
        }
        original_stat = os.lstat

        def case_insensitive_stat(path):
            if path == os.path.join(self.folder, "report.txt"):
                return original_stat(local)
            return original_stat(path)

        with patch(
            "file_sync.sync.os.lstat", side_effect=case_insensitive_stat
        ):
            self.assertTrue(synchronize(self.folder, self.cloud, self.logger))
        self.cloud.load.assert_called_once_with(local)
        self.cloud.delete.assert_called_once_with("report.txt")
