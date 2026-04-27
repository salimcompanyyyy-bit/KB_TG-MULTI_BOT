"""
Копирует «живую» БД (get_db_path) в database.db в корне репозитория — под git add / commit (бэкап на GitHub).

Остановите бота перед запуском, иначе возможна порчена копия из-за WAL.
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
    live = Path(get_db_path())
    if not live.is_file():
        print(f"Нет файла живой БД: {live}", file=sys.stderr)
        sys.exit(1)
    dest = REPO_DIR / "database.db"
    shutil.copy2(live, dest)
    for suffix in ("-wal", "-shm"):
        src = Path(str(live) + suffix)
        if src.is_file():
            shutil.copy2(src, dest.parent / (dest.name + suffix))
    print(f"Скопировано в: {dest} — далее: git add database.db* при необходимости.")


if __name__ == "__main__":
    main()
