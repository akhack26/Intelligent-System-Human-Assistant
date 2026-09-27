import os
import time
from datetime import datetime, timedelta

import pytest

from isha_core import file_index as FI
from isha_core import nl_search as NL


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "root"
    files = {
        "Downloads/report_2025.pdf": 10, "Downloads/invoice.pdf": 10, "Downloads/setup.exe": 10,
        "Downloads/big_movie.mp4": 12 * 1024 * 1024, "Desktop/calculator.py": 50, "Desktop/notes.txt": 5,
        "Pictures/holiday.jpg": 2000, "Pictures/huge.png": 11 * 1024 * 1024, "Music/song.mp3": 100,
        "node_modules/junk.pdf": 1, ".hidden/secret.pdf": 1, "Documents/Projects/readme.md": 3,
    }
    for rel, size in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.seek(max(0, size - 1)); f.write(b"\0")
    old = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(root / "Downloads/invoice.pdf", (old, old))
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=12).timestamp()
    os.utime(root / "Desktop/notes.txt", (yesterday, yesterday))
    return root


@pytest.fixture()
def index(tree, tmp_path):
    idx = FI.FileIndex(tmp_path / "idx.db", [tree])
    res = idx.update()
    assert res["success"] and res["completed"]
    yield idx
    idx.close()


def names(rows):
    return sorted(r["name"] for r in rows)


def test_index_by_extension_and_exclusions(index):
    rows = index.search(FI.SearchQuery(extensions=[".pdf"]))
    assert names(rows) == ["invoice.pdf", "report_2025.pdf"]      # node_modules + hidden excluded


def test_index_media_size_location(index, tree):
    assert names(index.search(FI.SearchQuery(media_type="video"))) == ["big_movie.mp4"]
    big = index.search(FI.SearchQuery(media_type="image", min_size=10 * 1024 * 1024))
    assert names(big) == ["huge.png"]
    desk = index.search(FI.SearchQuery(locations=[str(tree / "Desktop")]))
    assert names(desk) == ["calculator.py", "notes.txt"]
    assert names(index.search(FI.SearchQuery(kind="dir", name_terms=["projects"]))) == ["Projects"]


def test_index_date_filter(index):
    recent = index.search(FI.SearchQuery(extensions=[".pdf"], modified_after=time.time() - 7 * 86400))
    assert names(recent) == ["report_2025.pdf"]


def test_incremental_update(index, tree):
    (tree / "Downloads/new.pdf").write_bytes(b"x")
    (tree / "Downloads/report_2025.pdf").unlink()
    res = index.update()
    assert res["completed"] and res["removed"] >= 1
    assert names(index.search(FI.SearchQuery(extensions=[".pdf"]))) == ["invoice.pdf", "new.pdf"]
    st = index.status()
    assert st["ready"] and st["entries"] > 0


def test_fallback_scan_matches_index(tree):
    res = FI.scan_search(FI.SearchQuery(extensions=[".pdf"]), [tree])
    assert names(res["results"]) == ["invoice.pdf", "report_2025.pdf"]


def test_nl_parse_examples():
    p = NL.parse_search_request("10 MB se badi videos dhundo")
    q = p.candidates[0]
    assert q.media_type == "video" and q.min_size == 10 * 1024 * 1024
    q = NL.parse_search_request("kal modify hui file dhundo").candidates[0]
    assert q.modified_after and q.modified_before and q.modified_before - q.modified_after == 86400
    q = NL.parse_search_request("Downloads mein python files dhundo").candidates[0]
    assert q.extensions == [".py"] and q.locations and q.locations[0].endswith("Downloads")
    p = NL.parse_search_request("meri last wali photo")
    assert p.top_only and p.candidates[0].media_type == "image"
    p = NL.parse_search_request("Meri 2025 wali PDF dhundo")
    assert len(p.candidates) == 2 and "2025" in p.candidates[0].name_terms
    q = NL.parse_search_request("Desktop par calculator.py dhundo").candidates[0]
    assert q.name_terms == ["calculator.py"]


def test_engine_uses_index_then_candidates(index, tree):
    eng = NL.FileSearchEngine(index, [tree])
    res = eng.search_text("2025 wali pdf")
    assert res["method"] == "index" and names(res["results"]) == ["report_2025.pdf"]
    res = eng.search_text("saari mp4 files")
    assert names(res["results"]) == ["big_movie.mp4"]


def test_engine_without_index_scans(tree):
    res = NL.FileSearchEngine(None, [tree]).search_text("mp3 songs")
    assert res["method"] == "scan" and names(res["results"]) == ["song.mp3"]
