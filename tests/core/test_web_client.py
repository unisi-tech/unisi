"""
Tests for the config.web_client feature: switching the web client served
at '/' to a separate, custom UNISI-protocol client, while the framework's
own bundled client stays reachable at /default no matter what.

Covers unisi/server.py's DEFAULT_CLIENT_ROUTE, active_webpath(),
resolve_in_root(), warn_if_web_client_misconfigured(), and the
config.web_client branches inside static_serve() itself.

Layered like the rest of tests/core (see test_server.py's own module
docstring for the general split):
  * active_webpath() and resolve_in_root() are small pure functions --
    tested directly, no aiohttp needed.
  * warn_if_web_client_misconfigured() only ever prints -- tested directly
    with capsys.
  * The actual routing/precedence behavior (custom root vs. the bundled
    fallback vs. /default vs. public_dirs) only means something wired into
    a real aiohttp app, same as the rest of static_serve -- tested through
    the `app_client` fixture from conftest.py, exactly like TestStaticServe
    in test_server.py.

None of this needs a real separate web-client project on disk: a plain
tmp_path with an index.html (and, where relevant, one more file) stands in
for "a separate web-client working over the UNISI protocol" -- static_serve
only ever cares about which *files* are where, never about what a client
actually does with the UNISI websocket protocol once loaded.
"""
import pytest

import unisi.server as server_mod
from unisi.server import DEFAULT_CLIENT_ROUTE


class TestActiveWebpath:
    def test_returns_bundled_webpath_when_unset(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        assert server_mod.active_webpath() == server_mod.webpath

    def test_returns_bundled_webpath_when_empty_string(self, monkeypatch):
        # '' is falsy, same as None -- an emptied-out setting should not
        # be treated as "serve from the current working directory".
        monkeypatch.setattr(server_mod.config, "web_client", "")
        assert server_mod.active_webpath() == server_mod.webpath

    def test_returns_the_configured_web_client_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))
        assert server_mod.active_webpath() == str(tmp_path)


class TestResolveInRoot:
    def test_finds_an_existing_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("hi")
        result = server_mod.resolve_in_root(str(tmp_path), "/a.txt")
        assert result == (tmp_path / "a.txt").resolve()

    def test_resolves_a_nested_path(self, tmp_path):
        (tmp_path / "js").mkdir()
        (tmp_path / "js" / "app.js").write_text("console.log(1)")
        result = server_mod.resolve_in_root(str(tmp_path), "/js/app.js")
        assert result == (tmp_path / "js" / "app.js").resolve()

    def test_missing_file_returns_none(self, tmp_path):
        assert server_mod.resolve_in_root(str(tmp_path), "/nope.txt") is None

    def test_nonexistent_root_returns_none_without_raising(self, tmp_path):
        missing_root = tmp_path / "does-not-exist"
        assert server_mod.resolve_in_root(str(missing_root), "/a.txt") is None

    def test_traversal_outside_root_is_blocked(self, tmp_path):
        (tmp_path / "secret.txt").write_text("secret")
        root = tmp_path / "root"
        root.mkdir()
        result = server_mod.resolve_in_root(str(root), "/../secret.txt")
        assert result is None

    def test_root_without_leading_slash_in_rpath_still_resolves(self, tmp_path):
        # lstrip('/') means a caller-supplied rpath doesn't strictly need
        # its own leading slash -- documenting that rather than requiring it.
        (tmp_path / "a.txt").write_text("hi")
        result = server_mod.resolve_in_root(str(tmp_path), "a.txt")
        assert result == (tmp_path / "a.txt").resolve()


class TestWarnIfWebClientMisconfigured:
    def test_no_warning_when_unset(self, monkeypatch, capsys):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        server_mod.warn_if_web_client_misconfigured()
        assert capsys.readouterr().out == ""

    def test_no_warning_when_index_html_is_present(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "index.html").write_text("<html></html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))
        server_mod.warn_if_web_client_misconfigured()
        assert capsys.readouterr().out == ""

    def test_warns_when_index_html_is_missing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))
        server_mod.warn_if_web_client_misconfigured()
        out = capsys.readouterr().out
        assert str(tmp_path) in out
        assert DEFAULT_CLIENT_ROUTE in out

    def test_warns_when_the_directory_does_not_exist_at_all(self, monkeypatch, tmp_path, capsys):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(server_mod.config, "web_client", str(missing))
        server_mod.warn_if_web_client_misconfigured()
        assert str(missing) in capsys.readouterr().out


class TestDefaultRoute:
    """/default always serves the bundled client, regardless of config.web_client."""

    @pytest.mark.asyncio
    async def test_serves_the_bundled_index_with_no_custom_client_configured(
        self, app_client, monkeypatch
    ):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        resp = await app_client.get(DEFAULT_CLIENT_ROUTE)
        assert resp.status == 200
        assert "<html" in (await resp.text()).lower()

    @pytest.mark.asyncio
    async def test_trailing_slash_also_serves_the_bundled_index(self, app_client):
        resp = await app_client.get(f"{DEFAULT_CLIENT_ROUTE}/")
        assert resp.status == 200
        assert "<html" in (await resp.text()).lower()

    @pytest.mark.asyncio
    async def test_matches_root_exactly_when_no_custom_client_is_configured(
        self, app_client, monkeypatch
    ):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        root_body = await (await app_client.get("/")).text()
        default_body = await (await app_client.get(DEFAULT_CLIENT_ROUTE)).text()
        assert root_body == default_body

    @pytest.mark.asyncio
    async def test_serves_a_bundled_asset(self, app_client):
        resp = await app_client.get(f"{DEFAULT_CLIENT_ROUTE}/favicon.ico")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_file_under_default_is_404(self, app_client):
        resp = await app_client.get(f"{DEFAULT_CLIENT_ROUTE}/no/such/file.xyz")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_traversal_outside_the_bundled_client_is_blocked(self, app_client):
        resp = await app_client.get(f"{DEFAULT_CLIENT_ROUTE}/../../../../../../etc/passwd")
        assert resp.status in (404, 400)
        if resp.status == 200:
            pytest.fail("path traversal must never return 200")

    @pytest.mark.asyncio
    async def test_a_path_merely_starting_with_default_is_not_treated_as_the_route(
        self, app_client, tmp_path, monkeypatch
    ):
        """/defaultXYZ is not '/default' plus a '/'-separated tail, so it
        must fall through to normal (here: custom-client) handling instead
        of being swallowed by the /default special case.
        """
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "defaultXYZ.js").write_text("console.log('not the default route')")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))

        resp = await app_client.get("/defaultXYZ.js")
        assert resp.status == 200
        assert await resp.text() == "console.log('not the default route')"


class TestCustomWebClient:
    """config.web_client switches '/' (and other non-/default paths) over
    to a separate client's own files, per the docstring of this module.
    """

    @pytest.mark.asyncio
    async def test_root_and_default_diverge_once_a_custom_client_is_active(
        self, app_client, tmp_path, monkeypatch
    ):
        """The end-to-end proof that switching actually works: '/' shows
        the custom client, /default keeps showing the bundled one, and the
        two are no longer the same response.
        """
        (tmp_path / "index.html").write_text("<html>custom client</html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))

        root_body = await (await app_client.get("/")).text()
        default_body = await (await app_client.get(DEFAULT_CLIENT_ROUTE)).text()

        assert "custom client" in root_body
        assert "custom client" not in default_body
        assert root_body != default_body

    @pytest.mark.asyncio
    async def test_serves_the_custom_clients_own_asset(self, app_client, tmp_path, monkeypatch):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "app.js").write_text("console.log('custom')")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))

        resp = await app_client.get("/app.js")
        assert resp.status == 200
        assert await resp.text() == "console.log('custom')"

    @pytest.mark.asyncio
    async def test_custom_asset_takes_precedence_over_the_bundled_one_of_the_same_name(
        self, app_client, tmp_path, monkeypatch
    ):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "favicon.ico").write_text("custom favicon")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))

        resp = await app_client.get("/favicon.ico")
        assert resp.status == 200
        assert await resp.text() == "custom favicon"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_bundled_client_for_a_file_it_does_not_provide(
        self, app_client, tmp_path, monkeypatch
    ):
        """The key resilience case this fallback exists for: the bundled
        client's own build hardcodes absolute, root-relative asset paths
        (see server.py's static_serve() comment on step 1b), so a custom
        client that -- like most real ones -- never defines that exact
        path must still resolve it from the bundled client instead of
        404ing. favicon.ico stands in for that here as a real bundled file
        a minimal custom client plausibly won't ship its own copy of.
        """
        (tmp_path / "index.html").write_text("<html>custom client</html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))

        resp = await app_client.get("/favicon.ico")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_misconfigured_web_client_gracefully_falls_back_to_the_bundled_root(
        self, app_client, tmp_path, monkeypatch
    ):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(server_mod.config, "web_client", str(missing))

        resp = await app_client.get("/")
        assert resp.status == 200
        assert "<html" in (await resp.text()).lower()

    @pytest.mark.asyncio
    async def test_public_dirs_still_work_once_a_custom_client_is_active(
        self, app_client, tmp_path, monkeypatch
    ):
        """Regression guard: the new bundled-fallback step (1b) sits before
        the public_dirs step in static_serve() and must fall through to it
        for paths neither the custom client nor the bundled one has,
        exactly as it already did for the plain (no web_client) case in
        test_server.py::TestStaticServe.
        """
        client_dir = tmp_path / "client"
        client_dir.mkdir()
        (client_dir / "index.html").write_text("<html></html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(client_dir))

        public_dir = tmp_path / "public"
        public_dir.mkdir()
        (public_dir / "shared.txt").write_text("public content")
        monkeypatch.setattr(server_mod.config, "public_dirs", [str(public_dir)])

        resp = await app_client.get(f"{public_dir}/shared.txt")
        assert resp.status == 200
        assert await resp.text() == "public content"

    @pytest.mark.asyncio
    async def test_traversal_outside_the_custom_client_root_is_blocked(
        self, app_client, tmp_path, monkeypatch
    ):
        (tmp_path / "secret.txt").write_text("do not serve me")
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (allowed / "index.html").write_text("<html></html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(allowed))

        resp = await app_client.get("/../secret.txt")
        assert resp.status in (404, 400)
        if resp.status == 200:
            pytest.fail("path traversal must never return 200")

    @pytest.mark.asyncio
    async def test_unset_web_client_keeps_serving_the_bundled_client_at_root(
        self, app_client, monkeypatch
    ):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        resp = await app_client.get("/")
        assert resp.status == 200
        assert "<html" in (await resp.text()).lower()
