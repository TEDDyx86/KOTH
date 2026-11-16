import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    def __init__(self) -> None:
        self.db_path = Path(os.environ.get("CTF_DB_PATH", str(BASE_DIR / "ctf.db")))
        self.storage_dir = Path(os.environ.get("CTF_STORAGE_DIR", str(BASE_DIR / "storage")))
        self.flag_format = os.environ.get("CTF_FLAG_FORMAT", "flag{...}")


settings = Settings()
