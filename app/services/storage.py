import uuid
from pathlib import Path


class StorageService:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def save(self, challenge_id: int, filename: str, data: bytes) -> Path:
        safe_name = Path(filename.replace("\\", "/")).name or "unnamed"
        target_dir = self.storage_dir / str(challenge_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        path.write_bytes(data)
        return path
