"""Точка запуска python -m file_sync"""

import argparse
import os
import sys
import time
from contextlib import contextmanager

from .config import ConfigError, read_settings
from .storage import CloudConfigError
from .sync import synchronize
from .yandex_disk import YandexDisk


@contextmanager
def file_logger(path):
    """Настраивает лог для CLI и закрывает его при выходе"""
    from loguru import logger

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sink = logger.add(
            path,
            level="INFO",
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss ZZ} | {level:<5} | {message}",
        )
    except OSError:
        raise ConfigError(f"Не удалось открыть LOG_FILE: {path}") from None
    try:
        yield logger
    finally:
        logger.remove(sink)


def run(settings, storage, log, once):
    """Запускает первый цикл сразу, затем выдерживает паузу между циклами"""
    while True:
        success = synchronize(settings.local_dir, storage, log)
        if once:
            return 0 if success else 1
        try:
            time.sleep(settings.interval)
        except OverflowError:
            raise ConfigError("Уменьшите значение SYNC_INTERVAL") from None


def start(settings, once):
    """Открывает ресурсы и передаёт управление циклу синхронизации"""
    with file_logger(settings.log_file) as log:
        log.info("Сервис запущен. Локальная папка: {}", settings.local_dir)
        storage = YandexDisk(settings.token, settings.remote_dir)
        try:
            return run(settings, storage, log, once)
        except CloudConfigError as error:
            log.error("{}", error)
            return 2
        except KeyboardInterrupt:
            log.info("Сервис остановлен пользователем")
            return 0
        finally:
            storage.close()


def main(argv=None):
    """Разбирает параметры запуска и возвращает код выхода"""
    parser = argparse.ArgumentParser(
        description="Синхронизация локальной папки с Яндекс Диском"
    )
    parser.add_argument(
        "--once", action="store_true", help="Выполнить один цикл и выйти"
    )
    args = parser.parse_args(argv)
    try:
        return start(read_settings(), args.once)
    except ConfigError as error:
        print(f"Ошибка настроек: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
