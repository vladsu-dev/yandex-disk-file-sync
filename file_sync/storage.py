"""Данные и ошибки, общие для облачных хранилищ"""

from dataclasses import dataclass


class CloudError(Exception):
    """Ошибка запроса или ответа облака с безопасным для лога текстом"""


class CloudConfigError(Exception):
    """Ошибка доступа или пути, требующая исправления настроек"""


@dataclass(frozen=True)
class RemoteFile:
    """Контрольные суммы одного удалённого файла"""

    sha256: str | None = None
    md5: str | None = None
