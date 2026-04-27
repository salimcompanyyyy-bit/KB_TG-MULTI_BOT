"""
Копирует database.db из корня репозитория в путь «живой» БД (get_db_path из db_path).

Запускайте, остановив бота, чтобы подтянуть в рабочую копию снимок, пришедший с git pull.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from db_path import REPO_DIR, get_db_path


def main() -> None:
    source = REPO_DIR / "database.db"
    if not source.is_file():
        print("В репозитории нет database.db. Сначала сделайте git pull / checkout.", file=sys.stderr)
        sys.exit(1)
    target = Path(get_db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        src = source.parent / (source.name + suffix)
        if src.is_file():
            shutil.copy2(src, target.parent / (target.name + suffix))
        else:
            extra = target.parent / (target.name + suffix)
            if extra.is_file():
                try:
                    os.remove(extra)
                except OSError:
                    pass
    print(f"Скопировано в: {target}")


if __name__ == "__main__":
    main()
