"""Клиент REST API Яндекс Диска"""

import os
from time import sleep
from urllib.parse import urlsplit

import requests

from .local_files import open_regular_file, verify_unchanged
from .storage import CloudConfigError, CloudError, RemoteFile


def valid_hash(value, length):
    """Принимает только контрольную сумму в шестнадцатеричном формате"""
    if not isinstance(value, str) or len(value) != length:
        return None
    if any(char not in "0123456789abcdefABCDEF" for char in value):
        return None
    return value.lower()


def check_filename(name):
    """Проверяет, что имя обозначает один ресурс внутри выбранной папки"""
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise CloudError("Некорректное имя файла для Яндекс Диска")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise CloudError("Имя файла не кодируется в UTF-8") from None


def response_object(response):
    """Читает JSON-объект из ответа на запрос метаданных"""
    try:
        value = response.json()
    except ValueError:
        raise CloudError("Яндекс Диск вернул некорректный JSON") from None
    if not isinstance(value, dict):
        raise CloudError("Яндекс Диск вернул ответ неизвестного формата")
    return value


def response_link(response):
    """Извлекает безопасную HTTPS-ссылку из ответа API"""
    href = response_object(response).get("href")
    if not isinstance(href, str):
        raise CloudError("Яндекс Диск не вернул ссылку на операцию")
    try:
        url = urlsplit(href)
        secure = (
            url.scheme == "https" and url.hostname and url.username is None
        )
    except ValueError:
        secure = False
    if not secure:
        raise CloudError("Яндекс Диск вернул некорректную HTTPS-ссылку")
    return href


class YandexDisk:
    """Хранилище для файлов в одной существующей папке Яндекс Диска"""

    def __init__(self, token, remote_dir):
        self._headers = {
            "Authorization": f"OAuth {token}",
            "Accept": "application/json",
        }
        self._folder = remote_dir
        self._api = "https://cloud-api.yandex.net/v1/disk"
        self._session = requests.Session()

    def close(self):
        """Освобождает HTTP-соединения"""
        self._session.close()

    def _request(self, method, url, **kwargs):
        """Выполняет один запрос с ограничением времени ожидания"""
        try:
            return self._session.request(
                method,
                url,
                timeout=(5, 30),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException:
            # Текст библиотеки может содержать служебный URL из ответа API
            raise CloudError(
                f"Сетевой сбой при запросе {method} к Яндекс Диску"
            ) from None

    def _api_request(self, method, endpoint, **params):
        """Отправляет OAuth-токен только на официальный API"""
        response = self._request(
            method,
            f"{self._api}{endpoint}",
            headers=self._headers,
            params=params,
        )
        if response.status_code == 401 or (
            response.status_code == 403 and endpoint != "/resources/upload"
        ):
            raise CloudConfigError(
                f"Яндекс Диск вернул HTTP {response.status_code}. "
                "Проверьте YANDEX_DISK_TOKEN и права чтения и записи"
            )
        return response

    def _file_path(self, filename):
        check_filename(filename)
        return f"{self._folder}/{filename}"

    def _page(self, offset):
        response = self._api_request(
            "GET",
            "/resources",
            path=self._folder,
            limit=100,
            offset=offset,
            sort="name",
            fields="type,_embedded.items.name,_embedded.items.type,"
            "_embedded.items.sha256,_embedded.items.md5,_embedded.total",
        )
        if response.status_code == 404:
            raise CloudConfigError(
                "Папка REMOTE_DIR отсутствует. Создайте её на Яндекс Диске"
            )
        if response.status_code != 200:
            raise CloudError(
                f"Ошибка чтения облачной папки, HTTP {response.status_code}"
            )
        document = response_object(response)
        if document.get("type") != "dir":
            raise CloudConfigError(
                "REMOTE_DIR должен вести к папке на Яндекс Диске"
            )
        return parse_page(document)

    def get_info(self):
        """Возвращает имена и контрольные суммы всех файлов папки"""
        entries = {}
        offset = 0
        expected_total = None
        while True:
            page, total = self._page(offset)
            if expected_total is not None and total != expected_total:
                raise CloudError("Число ресурсов изменилось во время чтения")
            expected_total = total
            if entries.keys() & page.keys():
                raise CloudError(
                    "Список Яндекс Диска изменился во время чтения"
                )
            entries.update(page)
            offset += len(page)
            if offset == total:
                break
            if not page or offset > total:
                raise CloudError("Яндекс Диск вернул неполный список файлов")

        return {
            name: file for name, file in entries.items() if file is not None
        }

    def _upload(self, path, overwrite):
        filename = os.path.basename(path)
        with open_regular_file(path) as file:
            before = os.fstat(file.fileno())
            response = self._api_request(
                "GET",
                "/resources/upload",
                path=self._file_path(filename),
                overwrite=str(overwrite).lower(),
            )
            if response.status_code in (403, 507):
                raise CloudError(
                    f"Загрузка недоступна, HTTP {response.status_code}. "
                    "Проверьте свободное место и права записи на Яндекс Диск"
                )
            if response.status_code != 200:
                raise CloudError(
                    f"Не получена ссылка загрузки, HTTP {response.status_code}"
                )
            uploaded = self._request(
                "PUT",
                response_link(response),
                data=file if before.st_size else b"",
                headers={"Content-Type": "application/octet-stream"},
            )
            if uploaded.status_code == 202:
                raise CloudError(
                    "Яндекс Диск принял файл, но ещё обрабатывает загрузку"
                )
            if uploaded.status_code == 507:
                raise CloudError(
                    "Для загрузки не хватает места на Яндекс Диске"
                )
            if uploaded.status_code != 201:
                raise CloudError(
                    f"Ошибка загрузки, HTTP {uploaded.status_code}"
                )
            verify_unchanged(file, before)

    def load(self, path):
        """Загружает новый файл при свободном имени"""
        self._upload(path, overwrite=False)

    def reload(self, path):
        """Записывает новую версию файла"""
        self._upload(path, overwrite=True)

    def delete(self, filename):
        """Перемещает файл в корзину Яндекс Диска"""
        path = self._file_path(filename)
        metadata = self._api_request(
            "GET", "/resources", path=path, fields="type,name,md5"
        )
        if metadata.status_code == 404:
            return
        if metadata.status_code != 200:
            raise CloudError(
                f"Не прочитан удаляемый файл, HTTP {metadata.status_code}"
            )
        item = response_object(metadata)
        digest = valid_hash(item.get("md5"), 32)
        if (
            item.get("name") != filename
            or item.get("type") != "file"
            or not digest
        ):
            raise CloudError(
                "Путь больше не указывает на файл с проверяемым хешем"
            )
        # MD5 ограничивает удаление проверенной версией файла
        response = self._api_request(
            "DELETE",
            "/resources",
            path=path,
            permanently="false",
            md5=digest,
        )
        if response.status_code in (204, 404):
            return
        if response.status_code == 202:
            self._wait_for_delete(response_link(response))
            return
        raise CloudError(f"Удаление не выполнено, HTTP {response.status_code}")

    def _wait_for_delete(self, url):
        parsed = urlsplit(url)
        operation_path = "/v1/disk/operations"
        known_path = parsed.path == operation_path or parsed.path.startswith(
            f"{operation_path}/"
        )
        if (
            parsed.netloc
            not in ("cloud-api.yandex.net", "cloud-api.yandex.net:443")
            or not known_path
        ):
            raise CloudError(
                "Яндекс Диск вернул неизвестный адрес операции удаления"
            )
        for _ in range(5):
            result = self._request("GET", url, headers=self._headers)
            if result.status_code != 200:
                raise CloudError(
                    f"Не получен статус удаления, HTTP {result.status_code}"
                )
            status = response_object(result).get("status")
            if status == "success":
                return
            if status != "in-progress":
                raise CloudError("Яндекс Диск не подтвердил удаление файла")
            sleep(1)
        raise CloudError("Яндекс Диск ещё обрабатывает удаление")


def parse_remote_item(item):
    """Проверяет тип ресурса и параметры сравнения файла"""
    if not isinstance(item, dict):
        raise CloudError("Яндекс Диск вернул некорректную запись о ресурсе")
    name = item.get("name")
    check_filename(name)
    if item.get("type") == "dir":
        return name, None
    if item.get("type") != "file":
        raise CloudError("Яндекс Диск вернул ресурс неизвестного типа")
    sha256 = valid_hash(item.get("sha256"), 64)
    md5 = valid_hash(item.get("md5"), 32)
    if not sha256 and not md5:
        raise CloudError(
            f"Яндекс Диск не вернул контрольную сумму для файла {name!r}"
        )
    return name, RemoteFile(sha256, md5)


def parse_page(document):
    """Возвращает проверенные ресурсы и общее число записей"""
    embedded = document.get("_embedded")
    if not isinstance(embedded, dict):
        raise CloudError("Яндекс Диск не вернул список ресурсов папки")
    items = embedded.get("items")
    total = embedded.get("total")
    if not isinstance(items, list) or type(total) is not int or total < 0:
        raise CloudError("Яндекс Диск вернул неполные метаданные папки")
    page = dict(map(parse_remote_item, items))
    if len(page) != len(items):
        raise CloudError("Яндекс Диск вернул повторяющиеся имена файлов")
    return page, total
