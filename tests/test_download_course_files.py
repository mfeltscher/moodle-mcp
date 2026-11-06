import pytest

from moodle_mcp import api

HOST = "https://moodle.example.invalid"


def file_entry(name, path="/", size=None, url=None):
    data = f"content of {name}".encode()
    return {
        "type": "file",
        "filename": name,
        "filepath": path,
        "filesize": len(data) if size is None else size,
        "fileurl": url or f"{HOST}/webservice/pluginfile.php/1/mod_folder/content/1{path}{name}?forcedownload=1",
    }


SECTIONS = [
    {"name": "ANKÜNDIGUNGEN", "section": 1, "modules": [{"modname": "forum", "name": "News", "contents": None}]},
    {
        "name": "DATEIABLAGE",
        "section": 2,
        "modules": [
            {
                "modname": "folder",
                "name": "Vorlesungsunterlagen",
                "contents": [
                    file_entry("Skript.pdf", "/Unterlagen/"),
                    file_entry("Fall.pdf", "/Fall X/1) Anamnese/"),
                    {"type": "url", "filename": "Link", "fileurl": "https://elsewhere.invalid"},
                ],
            }
        ],
    },
    {
        "name": "Block: Theorie",
        "section": 3,
        "modules": [
            {"modname": "resource", "name": "Agenda", "contents": [file_entry("Agenda.pdf")]},
            {"modname": "folder", "name": "Skript", "contents": [file_entry("Teil 1.pdf")]},
            {"modname": "page", "name": "Info", "contents": [file_entry("index.html")]},
        ],
    },
]


class FakeResponse:
    def __init__(self, url):
        self.status_code = 200
        self.headers = {"content-type": "application/pdf"}
        name = url.split("?")[0].rsplit("/", 1)[-1].replace("%20", " ")
        self._data = f"content of {name}".encode()

    def iter_content(self, chunk_size):
        yield self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_moodle(monkeypatch):
    calls = []

    def fake_get(url, params=None, stream=False, timeout=None):
        calls.append((url, params))
        return FakeResponse(url)

    monkeypatch.setattr(api, "MOODLE_URL", f"{HOST}/webservice/rest/server.php")
    monkeypatch.setattr(api, "MOODLE_TOKEN", "test-token")
    monkeypatch.setattr(api, "get_moodle_api_data", lambda *a, **kw: SECTIONS)
    monkeypatch.setattr(api.requests, "get", fake_get)
    return calls


def test_mirrors_moodle_structure(fake_moodle, tmp_path):
    result = api.download_course_files(42, str(tmp_path), folder_name="DI4-2 Kasuistik 1")

    base = tmp_path / "DI4-2 Kasuistik 1"
    assert result["target_dir"] == str(base.resolve())
    assert (base / "DATEIABLAGE/Unterlagen/Skript.pdf").read_bytes() == b"content of Skript.pdf"
    assert (base / "DATEIABLAGE/Fall X/1) Anamnese/Fall.pdf").is_file()
    # Two file modules in one section -> module name becomes a subfolder; ":" is replaced.
    assert (base / "Block- Theorie/Agenda/Agenda.pdf").is_file()
    assert (base / "Block- Theorie/Skript/Teil 1.pdf").is_file()
    # Pages, forums and URL entries are not downloaded.
    assert not list(base.rglob("index.html"))
    assert (result["downloaded"], result["skipped"], result["failed"]) == (4, 0, 0)
    assert all(params == {"token": "test-token"} for _, params in fake_moodle)
    assert not list(base.rglob(".moodle-dl-*"))


def test_second_run_skips_existing_files(fake_moodle, tmp_path):
    api.download_course_files(42, str(tmp_path), folder_name="Kurs")
    fake_moodle.clear()

    result = api.download_course_files(42, str(tmp_path), folder_name="Kurs")

    assert (result["downloaded"], result["skipped"]) == (0, 4)
    assert fake_moodle == []


def test_foreign_host_and_traversal_are_rejected(fake_moodle, monkeypatch, tmp_path):
    evil = [
        {
            "name": "X",
            "section": 1,
            "modules": [
                {
                    "modname": "folder",
                    "name": "F",
                    "contents": [
                        file_entry("a.pdf", url="https://attacker.invalid/a.pdf"),
                        file_entry("..", "/../../"),
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(api, "get_moodle_api_data", lambda *a, **kw: evil)

    result = api.download_course_files(1, str(tmp_path), folder_name="Kurs")

    assert result["failed"] == 1  # foreign host
    assert fake_moodle == [] or all(HOST in url for url, _ in fake_moodle)
    # ".." components are neutralised, so nothing lands outside the course folder.
    assert {p.name for p in tmp_path.iterdir()} == {"Kurs"}


def test_relative_target_dir_is_refused(fake_moodle):
    with pytest.raises(ValueError):
        api.download_course_files(1, "relative/path", folder_name="Kurs")
