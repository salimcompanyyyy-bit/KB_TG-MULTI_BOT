"""Единая логика пути к живой БД: вне папки репозитория, чтобы git checkout не затирал данные.

Перед вызовом get_db_path() вызывайте load_dotenv() (как в bot.py / скриптах).

Снимок для Git: data/database.db (корень репозитория = на уровень выше src/).
"""
from __future__ import annotations

import os
import shutil
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
