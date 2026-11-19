"""Загрузка и проверка настроек запуска"""

import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from threading import TIMEOUT_MAX

from dotenv import dotenv_values


class ConfigError(Exception):
    """Ошибка настроек, которую можно исправить перед запуском"""


@dataclass(frozen=True)
class Settings:
    """Пять параметров из файла .env"""

    local_dir: str
    remote_dir: str
    token: str = field(repr=False)
    interval: float
    log_file: str


def required(values, name):
    """Возвращает непустой параметр или сообщает, что заполнить"""
    value = (values.get(name) or "").strip()
    if not value:
        raise ConfigError(f"Укажите {name} в файле .env")
    return value


def absolute_path(value, base):
    """Разрешает относительный путь от папки с .env"""
    return os.path.realpath(os.path.join(base, os.path.expanduser(value)))


def parse_interval(value):
    """Проверяет, что пауза задана положительным числом секунд"""
    try:
        interval = float(value)
    except ValueError:
        raise ConfigError(
            "SYNC_INTERVAL должен быть положительным числом секунд"
        ) from None
    if not isfinite(interval) or interval <= 0:
        raise ConfigError(
            "SYNC_INTERVAL должен быть конечным положительным числом секунд"
        )
    if interval >= TIMEOUT_MAX:
        raise ConfigError("Уменьшите значение SYNC_INTERVAL")
    return interval


def normalize_remote_dir(value):
    """Приводит путь к виду disk:/папка и исключает корень Диска"""
    path = value.removeprefix("disk:").strip("/")
    if (
        ":" in path
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ConfigError(
            "REMOTE_DIR должен указывать на папку, например disk:/Backup"
        )
    return f"disk:/{path}"


def file_status(path):
    """Возвращает сведения о файле, если путь существует"""
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def is_inside(path, directory, directory_status):
    """Проверяет принадлежность к папке по пути и по файловой системе"""
    ancestors = Path(path).parents
    if directory in ancestors:
        return True
    return any(
        status is not None and os.path.samestat(status, directory_status)
        for status in map(file_status, ancestors)
    )


def aliases_source_file(directory, status):
    """Ищет жёсткую ссылку на конфиг или журнал в локальной папке"""
    if status is None or status.st_nlink < 2:
        return False
    with os.scandir(directory) as entries:
        return any(
            os.path.samestat(entry.stat(follow_symlinks=False), status)
            for entry in entries
        )


def validate_paths(local_dir, log_file, config_path):
    """Не допускает попадания настроек и собственного журнала в зеркало"""
    try:
        directory = os.stat(local_dir)
        config = os.stat(config_path)
        log = file_status(log_file)
        if (
            is_inside(log_file, Path(local_dir), directory)
            or (log is not None and os.path.samestat(log, config))
            or aliases_source_file(local_dir, log)
        ):
            raise ConfigError(
                "Укажите LOG_FILE вне LOCAL_DIR и отдельно от .env"
            )
        if is_inside(
            config_path, Path(local_dir), directory
        ) or aliases_source_file(local_dir, config):
            raise ConfigError(
                "LOCAL_DIR должна находиться отдельно от файла .env"
            )
    except OSError:
        raise ConfigError(
            "Не удалось проверить LOCAL_DIR, LOG_FILE и .env"
        ) from None


def read_settings(env_file=".env"):
    """Читает настройки без изменения окружения процесса"""
    config_path = os.path.realpath(env_file)
    base = os.path.dirname(config_path)
    try:
        with open(config_path, encoding="utf-8-sig") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError(
            "Не удалось прочитать .env. Создайте его из .env.example"
        ) from None

    local_dir = absolute_path(required(values, "LOCAL_DIR"), base)
    if not os.path.isdir(local_dir):
        raise ConfigError(
            f"Папка LOCAL_DIR не существует или недоступна: {local_dir}"
        )

    log_file = absolute_path(required(values, "LOG_FILE"), base)
    validate_paths(local_dir, log_file, config_path)

    token = required(values, "YANDEX_DISK_TOKEN")
    if (
        not token.isascii()
        or not token.isprintable()
        or any(char.isspace() for char in token)
    ):
        raise ConfigError(
            "Укажите YANDEX_DISK_TOKEN без пробелов и префикса OAuth"
        )
    return Settings(
        local_dir=local_dir,
        remote_dir=normalize_remote_dir(required(values, "REMOTE_DIR")),
        token=token,
        interval=parse_interval(required(values, "SYNC_INTERVAL")),
        log_file=log_file,
    )
