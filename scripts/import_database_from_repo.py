"""
Копирует data/database.db в путь «живой» БД (get_db_path в src/db_path.py).

Запуск из корня репо: python scripts/import_database_from_repo.py
Остановите бота заранее.
"""
from __future__ import annotations

import os
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

    source = REPO_DIR / "data" / "database.db"
    if not source.is_file():
        print("Нет data/database.db. Сначала git pull / checkout.", file=sys.stderr)
        sys.exit(1)
    target = Path(get_db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        src = Path(str(source) + suffix)
        if src.is_file():
            shutil.copy2(src, Path(str(target) + suffix))
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
