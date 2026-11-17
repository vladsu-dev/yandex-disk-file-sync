"""Загрузка и проверка настроек запуска"""

import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

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
    if Path(log_file).is_relative_to(local_dir) or log_file == config_path:
        raise ConfigError("Укажите LOG_FILE вне LOCAL_DIR и отдельно от .env")
    if Path(config_path).is_relative_to(local_dir):
        raise ConfigError(
            "Выберите LOCAL_DIR вне папки, содержащей настройки .env"
        )

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
