"""
Копирует «живую» БД в data/database.db — под git add / commit.

Запуск из корня репо: python scripts/export_database_to_repo.py
Остановите бота заранее.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from db_path import REPO_DIR, get_db_path  # noqa: E402


def main() -> None:
    import shutil
    import os

    live = Path(get_db_path())
    if not live.is_file():
        print(f"Нет файла живой БД: {live}", file=sys.stderr)
        sys.exit(1)
    dest = REPO_DIR / "data" / "database.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live, dest)
    for suffix in ("-wal", "-shm"):
        src = Path(str(live) + suffix)
        if src.is_file():
            shutil.copy2(src, dest.parent / (dest.name + suffix))
    print(f"Скопировано в: {dest} — далее: git add data/ при необходимости.")


if __name__ == "__main__":
    main()
