"""Чтение обычных файлов первого уровня локальной папки"""

import hashlib
import os
import stat
from contextlib import contextmanager


def list_names(directory):
    """Собирает имена целиком перед выполнением операций в облаке"""
    with os.scandir(directory) as entries:
        return {entry.name for entry in entries}


def open_file_descriptor(path, flags):
    """Запрещает ссылки и блокировку на каналах, если ОС поддерживает флаги"""
    return os.open(
        path,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )


@contextmanager
def open_regular_file(path):
    """Открывает только тот обычный файл, который был обнаружен по пути"""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("Путь больше не указывает на обычный файл")
    with open(path, "rb", opener=open_file_descriptor) as file:
        opened = os.fstat(file.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
            before, opened
        ):
            raise OSError("Файл был заменён при открытии")
        yield file


def file_digest(path, algorithm):
    """Вычисляет хеш файла без загрузки всего содержимого в память"""
    with open_regular_file(path) as file:
        before = os.fstat(file.fileno())
        digest = hashlib.file_digest(
            file, lambda: hashlib.new(algorithm, usedforsecurity=False)
        ).hexdigest()
        verify_unchanged(file, before)
        return digest


def verify_unchanged(file, before):
    """Обнаруживает изменение открытого файла во время чтения"""
    after = os.fstat(file.fileno())
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OSError("Файл изменился во время чтения")
