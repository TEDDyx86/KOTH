import shutil

from .config import ToolSettings


def inventory(settings: ToolSettings) -> list[dict]:
    rows = []
    for name in sorted(settings.tools):
        path = shutil.which(name)
        rows.append({"name": name, "mode": settings.tools[name].mode,
                     "path": path, "present": path is not None})
    return rows
