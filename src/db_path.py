"""Единая логика пути к живой БД: вне папки репозитория, чтобы git checkout не затирал данные.

Перед вызовом get_db_path() вызывайте load_dotenv() (как в bot.py / скриптах).

Снимок для Git: data/database.db (корень репозитория = на уровень выше src/).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Корень репозитория (родитель каталога src/)
REPO_DIR = Path(__file__).resolve().parent.parent
# Снимок в репо для git push/pull
SNAPSHOT_DB = REPO_DIR / "data" / "database.db"
# Старая раскладка до вынесения в data/
LEGACY_ROOT_DB = REPO_DIR / "database.db"


def get_db_path() -> str:
    custom = (os.environ.get("DB_PATH") or "").strip()
    if custom:
        return str(Path(os.path.expanduser(custom)).resolve())

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            local = str(Path(os.path.expanduser("~")) / "AppData" / "Local")
        base = Path(local) / "KB_TG-MULTI_BOT"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", str(Path(os.path.expanduser("~")) / ".local" / "share"))
        base = Path(xdg) / "KB_TG-MULTI_BOT"

    ext = base / "database.db"
    if ext.is_file():
        return str(ext)

    source: Path | None = None
    if SNAPSHOT_DB.is_file():
        source = SNAPSHOT_DB
    elif LEGACY_ROOT_DB.is_file():
        source = LEGACY_ROOT_DB

    if source is not None:
        ext.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, ext)
        for suffix in ("-wal", "-shm"):
            src = Path(str(source) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(str(ext) + suffix))
        return str(ext)

    ext.parent.mkdir(parents=True, exist_ok=True)
    return str(ext)


def export_live_to_repo_snapshot() -> None:
    """
    Снимок текущей рабочей БД в data/database.db (для дальнейшего git add).
    Онлайн-копия через API SQLite copy (лучше, чем копировать файл при WAL).
    """
    live_path = get_db_path()
    SNAPSHOT_DB.parent.mkdir(parents=True, exist_ok=True)
    live = sqlite3.connect(live_path, timeout=30.0)
    try:
        live.execute("PRAGMA busy_timeout=30000")
        dest = sqlite3.connect(str(SNAPSHOT_DB), timeout=30.0)
        try:
            dest.execute("PRAGMA busy_timeout=30000")
            with dest:
                live.backup(dest, pages=10)
        finally:
            dest.close()
    finally:
        live.close()
    for suffix in ("-wal", "-shm"):
        w = Path(str(SNAPSHOT_DB) + suffix)
        if w.is_file():
            try:
                w.unlink()
            except OSError:
                pass


def import_repo_snapshot_to_live() -> None:
    """
    Заменить рабочую БД содержимым data/database.db (тот же механизм backup).
    """
    if not SNAPSHOT_DB.is_file():
        raise FileNotFoundError("Нет data/database.db в репозитории.")
    live_path = get_db_path()
    src = sqlite3.connect(str(SNAPSHOT_DB), timeout=30.0)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dest = sqlite3.connect(live_path, timeout=30.0)
        try:
            dest.execute("PRAGMA busy_timeout=30000")
            with dest:
                src.backup(dest, pages=10)
        finally:
            dest.close()
    finally:
        src.close()
