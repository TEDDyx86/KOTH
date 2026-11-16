from app.services.storage import StorageService


def test_save_writes_file_and_sanitizes_name(tmp_path):
    storage = StorageService(tmp_path / "storage")
    path = storage.save(7, "../../evil/../report.pdf", b"%PDF-1.4 fake")
    assert path.exists()
    assert path.parent == (tmp_path / "storage" / "7")
    assert path.name.endswith("report.pdf")
    assert ".." not in path.name
    assert path.read_bytes() == b"%PDF-1.4 fake"


def test_save_avoids_collisions(tmp_path):
    storage = StorageService(tmp_path / "storage")
    a = storage.save(1, "same.txt", b"a")
    b = storage.save(1, "same.txt", b"b")
    assert a != b
    assert b.read_bytes() == b"b"
