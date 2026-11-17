"""Сравнение локальной папки и облака за один проход"""

import os
import stat

from .local_files import file_digest, list_names
from .storage import CloudError


def sync_file(path, remote_file, storage, logger):
    """Загружает обычный файл только при отсутствии или изменении копии"""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        return
    if remote_file is None:
        storage.load(path)
        logger.info("Загружен файл {!r}", os.path.basename(path))
        return

    algorithm = "sha256" if remote_file.sha256 else "md5"
    expected_digest = remote_file.sha256 or remote_file.md5
    if file_digest(path, algorithm) == expected_digest:
        return
    storage.reload(path)
    logger.info("Обновлён файл {!r}", os.path.basename(path))


def is_missing(directory, filename):
    """Проверяет точное написание имени с учётом регистра"""
    try:
        os.lstat(os.path.join(directory, filename))
    except FileNotFoundError:
        return True
    return filename not in list_names(directory)


def delete_missing(directory, original, filename, storage, logger):
    """Удаляет облачную копию после повторной локальной проверки"""
    if not is_missing(directory, filename):
        return
    # Исчезновение всей папки нельзя считать удалением всех файлов
    if not os.path.samestat(original, os.stat(directory)):
        raise OSError("Локальная папка была заменена во время сверки")
    storage.delete(filename)
    logger.info("Удалён из облачной папки файл {!r}", filename)


def sync_local_files(directory, names, remote, storage, logger):
    """Обрабатывает локальные файлы независимо друг от друга"""
    success = True
    for name in sorted(names):
        try:
            sync_file(
                os.path.join(directory, name),
                remote.get(name),
                storage,
                logger,
            )
        except (OSError, CloudError) as error:
            logger.error("Файл {!r}: {}", name, error)
            success = False
    return success


def delete_remote_files(directory, original, names, storage, logger):
    """Удаляет облачные файлы без локального аналога"""
    success = True
    for name in sorted(names):
        try:
            delete_missing(directory, original, name, storage, logger)
        except (OSError, CloudError) as error:
            logger.error("Удаление файла {!r}: {}", name, error)
            success = False
    return success


def synchronize(directory, storage, logger):
    """Сверяет два полных списка перед записью изменений в облако"""
    try:
        original = os.stat(directory)
        local_names = list_names(directory)
        remote = storage.get_info()
    except (OSError, CloudError) as error:
        logger.error("Цикл пропущен: {}", error)
        return False

    written = sync_local_files(directory, local_names, remote, storage, logger)
    if not written:
        # Сохраняем старую копию при сбое загрузки нового имени
        logger.error("Удаления отложены до успешной сверки локальных файлов")
        return False

    removed = delete_remote_files(
        directory, original, remote.keys() - local_names, storage, logger
    )
    if removed:
        logger.info("Цикл синхронизации завершён")
        return True
    logger.error("Цикл завершён с ошибками")
    return False
