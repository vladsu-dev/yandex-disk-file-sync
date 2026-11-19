"""Проверки запросов Яндекс Диска без сетевого доступа"""

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from requests.adapters import BaseAdapter

from file_sync.storage import CloudConfigError, CloudError, RemoteFile
from file_sync.sync import synchronize
from file_sync.yandex_disk import YandexDisk


class RecordingAdapter(BaseAdapter):
    """Подменяет транспорт, сохраняя подготовленные библиотекой HTTP-запросы"""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def send(self, request, **kwargs):
        body = request.body
        if hasattr(body, "read"):
            body = body.read()
        self.calls.append((request, body, kwargs["timeout"]))
        code, payload = next(self.responses)
        response = requests.Response()
        response.status_code = code
        response._content = json.dumps(payload).encode("utf-8")
        return response

    def close(self):
        pass


class YandexDiskTests(unittest.TestCase):
    def make_client(self, *responses):
        session = requests.Session()
        session.trust_env = False
        adapter = RecordingAdapter(responses)
        session.mount("https://", adapter)
        with patch(
            "file_sync.yandex_disk.requests.Session", return_value=session
        ):
            storage = YandexDisk("test-secret", "disk:/Резервные копии")
        self.addCleanup(storage.close)
        return storage, adapter

    def test_lists_paginated_files_and_ignores_subfolders(self):
        storage, adapter = self.make_client(
            (200, self.page([{"name": "Папка", "type": "dir"}], total=2)),
            (
                200,
                self.page(
                    [{"name": "файл.txt", "type": "file", "sha256": "a" * 64}],
                    total=2,
                ),
            ),
        )
        self.assertEqual(
            storage.get_info(), {"файл.txt": RemoteFile("a" * 64)}
        )
        request, _, timeout = adapter.calls[1]
        self.assertIn("offset=1", request.url)
        self.assertIn("limit=100", request.url)
        self.assertEqual(request.headers["Authorization"], "OAuth test-secret")
        self.assertEqual(timeout, (5, 30))

    def test_rejects_malformed_next_page(self):
        storage, _ = self.make_client(
            (200, self.page([{"name": "dir", "type": "dir"}], total=2)),
            (200, {"type": "dir", "_embedded": {"total": 2}}),
        )
        with self.assertRaises(CloudError):
            storage.get_info()

    def test_uploads_over_signed_link_without_oauth(self):
        link = {"href": "https://upload.example.test/signed-url"}
        storage, adapter = self.make_client(
            (200, link),
            (201, None),
            (200, link),
            (201, None),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "имя файла.txt")
            with open(path, "wb") as file:
                file.write(b"contents")
            storage.load(path)
            open(path, "wb").close()
            storage.reload(path)

        self.assertIn("overwrite=false", adapter.calls[0][0].url)
        self.assertIn("overwrite=true", adapter.calls[2][0].url)
        get_request = adapter.calls[0][0]
        self.assertIn(
            "%D0%B8%D0%BC%D1%8F+%D1%84%D0%B0%D0%B9%D0%BB%D0%B0.txt",
            get_request.url,
        )
        request, body, _ = adapter.calls[1]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(body, b"contents")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(adapter.calls[3][0].headers["Content-Length"], "0")

    def test_deletes_to_trash_conditionally_and_checks_operation(self):
        storage, adapter = self.make_client(
            (200, {"type": "file", "name": "old.txt", "md5": "b" * 32}),
            (
                202,
                {
                    "href": "https://cloud-api.yandex.net/v1/disk/operations?id=test"
                },
            ),
            (200, {"status": "success"}),
        )
        storage.delete("old.txt")
        deletion = adapter.calls[1][0]
        self.assertEqual(deletion.method, "DELETE")
        self.assertIn("permanently=false", deletion.url)
        self.assertIn(f"md5={'b' * 32}", deletion.url)
        self.assertIn("/operations?id=test", adapter.calls[2][0].url)

    def test_delete_refuses_directory(self):
        storage, adapter = self.make_client((200, {"type": "dir"}))
        with self.assertRaises(CloudError):
            storage.delete("folder")
        self.assertEqual(len(adapter.calls), 1)

    def test_auth_errors_are_actionable_and_hide_token(self):
        storage, _ = self.make_client((401, {"error": "test-secret"}))
        with self.assertRaises(CloudConfigError) as caught:
            storage.get_info()
        self.assertIn("YANDEX_DISK_TOKEN", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))

    def test_missing_remote_folder_is_reported(self):
        storage, _ = self.make_client((404, {}))
        with self.assertRaisesRegex(CloudConfigError, "REMOTE_DIR"):
            storage.get_info()

    def test_upload_quota_error_keeps_service_retryable(self):
        storage, _ = self.make_client((403, {}))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "new.txt")
            open(path, "wb").close()
            with self.assertRaisesRegex(CloudError, "место"):
                storage.load(path)

    def test_three_cycles_through_connector_and_real_local_files(self):
        def cloud_item(data):
            return {
                "name": "work.txt",
                "type": "file",
                "sha256": hashlib.sha256(data).hexdigest(),
                "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            }

        first = cloud_item(b"first")
        newer = cloud_item(b"newer")
        link = {"href": "https://upload.example.test/signed"}
        storage, adapter = self.make_client(
            (200, self.page([], total=0)),
            (200, link),
            (201, None),
            (200, self.page([first], total=1)),
            (200, link),
            (201, None),
            (200, self.page([newer], total=1)),
            (200, newer),
            (204, None),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "work.txt")
            log = Mock()
            with open(path, "wb") as file:
                file.write(b"first")
            self.assertTrue(synchronize(folder, storage, log))
            with open(path, "wb") as file:
                file.write(b"newer")
            self.assertTrue(synchronize(folder, storage, log))
            os.unlink(path)
            self.assertTrue(synchronize(folder, storage, log))

        self.assertEqual(adapter.calls[2][1], b"first")
        self.assertEqual(adapter.calls[5][1], b"newer")
        self.assertEqual(adapter.calls[8][0].method, "DELETE")

    @staticmethod
    def page(items, total):
        return {"type": "dir", "_embedded": {"total": total, "items": items}}

    def test_upload_does_not_send_bytes_to_unexpanded_url_template(self):
        link = {
            "href": "https://upload.example.test/{target}",
            "method": "PUT",
            "templated": True,
        }
        storage, adapter = self.make_client((200, link), (201, None))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "work.txt")
            with open(path, "wb") as file:
                file.write(b"data")
            with self.assertRaises(CloudError):
                storage.load(path)
        self.assertEqual(len(adapter.calls), 1)
