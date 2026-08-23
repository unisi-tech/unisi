"""
Tests for utils.py: path/url helpers (filename2url, url2filepath,
url2filename, upload_path, cache_url), the layout-tree walkers
(iter_layout_units, fill_parents), py_files, Screen.defaults, and the
module-import-time config bootstrap block at the top of the file.

Most of this is plain functions reading either their own arguments or the
ambient `config` module -- for the latter, tests monkeypatch the specific
config attribute they care about rather than relying on whatever fixtures_app/
config.py happened to load first in this pytest session (see
tests/core/conftest.py's module docstring for why that's not safe to rely
on: config is a process-wide singleton shared with every other test
directory that might run in the same session).

The config-bootstrap block itself (import config / synthesize one under
pytest / write a default config.py to disk) only ever runs once per
process -- by the time any test in this whole suite runs, some directory's
conftest.py has already triggered it. TestConfigBootstrap below tests that
block in fresh, throwaway *subprocesses* instead (the only way to actually
observe its various branches), matching the style used nowhere else in this
repo but justified by that same one-shot-per-process constraint -- see that
class's own docstring.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import unisi.utils as utils_mod
from unisi.units import ChangedProxy, Unit
from unisi.containers import Block, Screen

# The directory CONTAINING the unisi/ package -- what a fresh subprocess
# needs on its sys.path to be able to `import unisi` at all. Same
# computation as conftest.py's UNISI_ROOT, just derived from the already-
# imported module's own __file__ instead of this file's location.
_UNISI_ROOT = Path(utils_mod.__file__).resolve().parents[1]


class FakeUnit(Unit):
    def __init__(self, name):
        super().__init__(name)


class TestFilename2Url:
    def test_relative_path_is_unchanged(self):
        assert utils_mod.filename2url("style.css") == "style.css"

    def test_relative_path_with_subdir_is_unchanged(self):
        assert utils_mod.filename2url("icons/favicon.png") == "icons/favicon.png"

    def test_empty_string_does_not_crash(self):
        """Regression test: `if fn[0] == '/' or fn[1] == ':'` used to raise
        IndexError for any string shorter than 2 characters (including the
        empty string) before it ever got to decide whether the path looked
        absolute. An empty/short filename isn't a hostile input -- it's an
        easy accident (an empty upload field name, a path with a trailing
        separator stripped down to nothing upstream) -- so this must return
        a sensible value, not raise.
        """
        assert utils_mod.filename2url("") == ""

    def test_single_character_does_not_crash(self):
        # `fn[1]` alone used to raise IndexError for exactly this case --
        # 1-character strings pass the `fn[0] == '/'` check (False, so
        # short-circuit doesn't save it) and then fail on the length-2 read.
        assert utils_mod.filename2url("x") == "x"

    def test_full_posix_path_has_app_dir_prefix_stripped(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "app_dir", "/home/app")
        monkeypatch.setattr(utils_mod, "divpath", "/")
        assert utils_mod.filename2url("/home/app/screens/home.py") == "screens/home.py"

    def test_windows_drive_path_has_app_dir_prefix_stripped(self, monkeypatch):
        monkeypatch.setattr(utils_mod, "app_dir", "C:\\app")
        monkeypatch.setattr(utils_mod, "divpath", "\\")
        assert utils_mod.filename2url("C:\\app\\screens\\home.py") == "screens\\home.py"


class TestUrl2Filepath:
    def test_strips_up_to_first_slash(self):
        # No "//" after a scheme here -- a plain domain/path shape, which
        # is what "strip up to (and including) the FIRST slash" cleanly
        # supports: exactly one segment removed, unlike a full "http://"
        # URL (see test_full_url_keeps_the_double_slash_remainder below).
        assert utils_mod.url2filepath("host/some/path.png") == "some/path.png"

    def test_no_slash_returns_whole_string(self):
        assert utils_mod.url2filepath("noslash") == "noslash"

    def test_percent_20_becomes_space(self):
        assert utils_mod.url2filepath("host/a%20b.png") == "a b.png"

    def test_empty_string(self):
        assert utils_mod.url2filepath("") == ""

    def test_full_url_keeps_the_double_slash_remainder(self):
        # Documenting actual behavior for a "scheme://" URL: find('/')
        # locates the FIRST slash, which for "http://..." is the one
        # right after the colon -- only one of the two slashes in "//"
        # gets stripped, so the result still has a leading '/'. This
        # helper isn't scheme-aware; it isn't used anywhere internally
        # (dead-but-public code), so this pins down what it actually does
        # today rather than guessing at unwritten intent.
        assert utils_mod.url2filepath("http://host/path.png") == "/host/path.png"


class TestUrl2Filename:
    def test_strips_up_to_last_slash(self):
        assert utils_mod.url2filename("http://host/some/path.png") == "path.png"

    def test_no_slash_returns_whole_string(self):
        assert utils_mod.url2filename("justaname.png") == "justaname.png"

    def test_percent_20_becomes_space(self):
        assert utils_mod.url2filename("http://host/my%20file.png") == "my file.png"

    def test_traversal_prefix_is_discarded_since_it_comes_before_the_last_slash(self):
        assert utils_mod.url2filename("http://host/../../etc/evil.txt") == "evil.txt"


class TestUploadPath:
    def test_joins_upload_dir_and_filename(self, monkeypatch):
        monkeypatch.setattr(utils_mod.config, "upload_dir", "uploads")
        monkeypatch.setattr(utils_mod, "divpath", "/")
        assert utils_mod.upload_path("photo.png") == "uploads/photo.png"


class TestCacheUrl:
    class FakeResponse:
        def __init__(self, status_code=200, content=b"data"):
            self.status_code = status_code
            self.content = content

    class FakeRequests:
        def __init__(self, response):
            self._response = response
            self.calls = []

        def get(self, url):
            self.calls.append(url)
            return self._response

    def test_downloads_and_writes_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils_mod.config, "upload_dir", str(tmp_path))
        monkeypatch.setattr(utils_mod, "divpath", "/")
        fake = self.FakeRequests(self.FakeResponse(200, b"hello-bytes"))
        monkeypatch.setattr(utils_mod, "requests", fake)

        result = utils_mod.cache_url("http://example.com/dir/photo.png")

        expected_path = str(tmp_path / "photo.png")
        assert result == expected_path
        assert Path(expected_path).read_bytes() == b"hello-bytes"
        assert fake.calls == ["http://example.com/dir/photo.png"]

    def test_non_200_status_returns_none_and_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils_mod.config, "upload_dir", str(tmp_path))
        monkeypatch.setattr(utils_mod, "divpath", "/")
        fake = self.FakeRequests(self.FakeResponse(404, b""))
        monkeypatch.setattr(utils_mod, "requests", fake)

        result = utils_mod.cache_url("http://example.com/missing.png")

        assert result is None
        assert list(tmp_path.iterdir()) == []

    def test_local_filename_is_derived_from_the_url_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils_mod.config, "upload_dir", str(tmp_path))
        monkeypatch.setattr(utils_mod, "divpath", "/")
        fake = self.FakeRequests(self.FakeResponse(200, b"x"))
        monkeypatch.setattr(utils_mod, "requests", fake)

        result = utils_mod.cache_url("http://example.com/a/b/report%20final.pdf")

        assert result == str(tmp_path / "report final.pdf")


class TestIterLayoutUnits:
    def test_flat_list_of_units(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        assert list(utils_mod.iter_layout_units([a, b])) == [a, b]

    def test_nested_list_and_tuple(self):
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        assert list(utils_mod.iter_layout_units([a, (b, c)])) == [a, b, c]

    def test_non_unit_non_container_values_are_skipped(self):
        a = FakeUnit("a")
        assert list(utils_mod.iter_layout_units([a, None, "text", 5])) == [a]

    def test_single_unit_not_in_a_list(self):
        a = FakeUnit("a")
        assert list(utils_mod.iter_layout_units(a)) == [a]

    def test_block_recurses_into_its_own_value(self):
        inner = FakeUnit("inner")
        block = Block("B", inner)
        result = list(utils_mod.iter_layout_units([block]))
        assert result == [block, inner]

    def test_changed_proxy_wrapped_list_is_unwrapped(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        proxy = ChangedProxy([a, b], None)
        assert list(utils_mod.iter_layout_units(proxy)) == [a, b]


class TestFillParents:
    def test_flat_list_maps_each_unit_to_the_given_parent(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        parent = FakeUnit("parent")
        parents = {}
        utils_mod.fill_parents([a, b], parent, parents)
        assert parents == {a: parent, b: parent}

    def test_nested_group_still_maps_to_the_outer_parent(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        parent = FakeUnit("parent")
        parents = {}
        utils_mod.fill_parents([a, (b,)], parent, parents)
        assert parents == {a: parent, b: parent}

    def test_block_children_map_to_the_block_itself(self):
        inner = FakeUnit("inner")
        block = Block("B", inner)
        screen_stub = FakeUnit("screen")
        parents = {}
        utils_mod.fill_parents([block], screen_stub, parents)
        assert parents[block] is screen_stub
        assert parents[inner] is block


class TestPyFiles:
    def test_yields_py_files_only(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "notes.txt").write_text("")
        assert sorted(utils_mod.py_files(str(tmp_path))) == ["a.py", "b.py"]

    def test_excludes_init_py(self, tmp_path):
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "real.py").write_text("")
        assert list(utils_mod.py_files(str(tmp_path))) == ["real.py"]

    def test_nonexistent_directory_yields_nothing(self, tmp_path):
        assert list(utils_mod.py_files(str(tmp_path / "does_not_exist"))) == []


class TestScreenDefaults:
    def test_has_the_expected_keys(self):
        assert set(Screen.defaults.keys()) == {
            "icon", "prepare", "blocks", "header", "toolbar", "order",
            "persist", "reload", "lang", "voice", "image",
        }

    def test_blocks_and_toolbar_default_to_empty_lists(self):
        assert Screen.defaults["blocks"] == []
        assert Screen.defaults["toolbar"] == []

    def test_header_matches_the_configured_appname(self):
        assert Screen.defaults["header"] == utils_mod.config.appname

    def test_voice_is_the_opposite_of_mirror(self):
        assert Screen.defaults["voice"] == (not utils_mod.config.mirror)


class TestConfigBootstrap:
    """
    utils.py's `try: import config / except: ...` block at module scope
    only runs once per process (Python caches sys.modules['unisi.utils']),
    and every other test in this whole repo already needs it to have run
    before their own fixtures make sense -- so there's no way to observe
    its "no config.py anywhere" branches from within the main test process
    without tearing down half the test session's state. Each test here
    instead runs `python -c "import unisi"` in a fresh subprocess with a
    controlled, empty temp directory as both cwd and sys.path[0], which
    exercises the exact same first-import code path a real `python app.py`
    (or `pytest`, from an empty project) would.
    """

    def _run(self, tmp_path, script, extra_argv=()):
        return subprocess.run(
            [sys.executable, "-c", script, *extra_argv],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_under_pytest_with_no_config_py_synthesizes_in_memory_defaults(self, tmp_path):
        """The pytest-detection branch: 'pytest' faked into sys.modules
        (cheaper and just as valid a trigger as an actual pytest run,
        since the code only checks membership) with no config.py
        anywhere on sys.path. Must NOT write config.py/log to disk, and
        must produce the documented default values.
        """
        script = textwrap.dedent(f"""
            import sys, types
            sys.modules['pytest'] = types.ModuleType('pytest')
            sys.path.insert(0, {str(_UNISI_ROOT)!r})
            import unisi  # noqa: F401 -- triggers utils.py's bootstrap block
            import config
            print('hot_reload=', config.hot_reload)
            print('logfile=', config.logfile)
            print('autotest=', config.autotest)
            print('port=', config.port)
            print('upload_dir_exists=', __import__('os').path.isdir(config.upload_dir))
        """)
        result = self._run(tmp_path, script)

        assert result.returncode == 0, result.stderr
        assert "hot_reload= True" in result.stdout
        assert "logfile= None" in result.stdout
        assert "autotest= *" in result.stdout
        assert "port= 8000" in result.stdout
        assert "upload_dir_exists= True" in result.stdout  # a real tmp dir, just not in tmp_path
        assert not (tmp_path / "config.py").exists()
        assert not (tmp_path / "log").exists()

    def test_outside_pytest_with_no_config_py_writes_a_default_to_disk(self, tmp_path):
        """No 'pytest' in sys.modules and no config.py -- the real
        first-run-of-a-fresh-app path. Must write a usable config.py (and,
        because that default sets logfile='log', a log file) into cwd.
        """
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(_UNISI_ROOT)!r})
            import unisi  # noqa: F401 -- triggers utils.py's bootstrap block
            import config
            print('hot_reload=', config.hot_reload)
            print('logfile=', config.logfile)
            print('upload_dir=', config.upload_dir)
        """)
        result = self._run(tmp_path, script)

        assert result.returncode == 0, result.stderr
        assert "hot_reload= True" in result.stdout
        assert "logfile= log" in result.stdout
        assert "upload_dir= web" in result.stdout
        assert (tmp_path / "config.py").exists()
        assert (tmp_path / "log").exists()

    def test_generated_config_py_is_valid_for_a_second_run(self, tmp_path):
        """The config.py written by the first run must itself be valid,
        importable Python that a *second* process (pytest not in
        sys.modules either time) can load without regenerating it."""
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(_UNISI_ROOT)!r})
            import unisi  # noqa: F401 -- triggers utils.py's bootstrap block
            import config
        """)
        first = self._run(tmp_path, script)
        assert first.returncode == 0, first.stderr
        written = (tmp_path / "config.py").read_text()

        second = self._run(tmp_path, script)
        assert second.returncode == 0, second.stderr
        # second run must not have rewritten/duplicated the file
        assert (tmp_path / "config.py").read_text() == written
