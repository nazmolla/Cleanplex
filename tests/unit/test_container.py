"""Container bootstrap (#194): settings seeded from Plex's folder, nginx config for a container."""

from pathlib import Path

import pytest

from cleanplex import container, frontdoor
from cleanplex import database as db


def _plex_folder(tmp_path: Path, token: str = "tok123") -> Path:
    data = tmp_path / "Library/Application Support/Plex Media Server"
    data.mkdir(parents=True)
    (data / "Preferences.xml").write_text(
        f'<Preferences ProcessedMachineIdentifier="mid1" PlexOnlineToken="{token}" />')
    return tmp_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("CLEANPLEX_PLEX_URL", "CLEANPLEX_PLEX_TOKEN", "CLEANPLEX_PLEX_UPSTREAM", "CLEANPLEX_PLEX_MACHINE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(container, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("CLEANPLEX_PLEX_DATA", str(_plex_folder(tmp_path / "plex")))
    return monkeypatch


async def test_seed_reads_url_and_token_from_the_plex_folder(env):
    changed = await container.seed_settings()
    assert changed == {"plex_url": "http://127.0.0.1:32400", "plex_token": "set"}
    assert await db.get_setting("plex_token") == "tok123"


async def test_seed_keeps_a_token_the_user_already_saved(env):
    await container.seed_settings()
    await db.set_setting("plex_token", "typed-by-user")
    assert "plex_token" not in await container.seed_settings()
    assert await db.get_setting("plex_token") == "typed-by-user"


async def test_explicit_environment_overrides_saved_values(env):
    await container.seed_settings()
    env.setenv("CLEANPLEX_PLEX_URL", "http://plex:32400/")
    env.setenv("CLEANPLEX_PLEX_TOKEN", "from-env")
    changed = await container.seed_settings()
    assert changed == {"plex_url": "http://plex:32400", "plex_token": "set"}
    assert await db.get_setting("plex_token") == "from-env"


async def test_seed_is_a_no_op_when_nothing_changed(env):
    await container.seed_settings()
    assert await container.seed_settings() == {}


async def test_seed_without_a_plex_folder_sets_only_the_url(env, tmp_path):
    env.setenv("CLEANPLEX_PLEX_DATA", str(tmp_path / "missing"))
    assert await container.seed_settings() == {"plex_url": "http://127.0.0.1:32400"}


def test_upstream_environment_sets_both_nginx_upstream_and_plex_url(monkeypatch):
    monkeypatch.delenv("CLEANPLEX_PLEX_URL", raising=False)
    monkeypatch.setenv("CLEANPLEX_PLEX_UPSTREAM", "plex:32400")
    assert container.plex_url_from_env() == "http://plex:32400"
    conf = frontdoor.render_nginx_conf(Path("/c.pem"), Path("/k.pem"))
    assert "upstream cleanplex_plex { server plex:32400;" in conf


def test_nginx_config_loads_debian_modules_only_when_the_folder_exists(tmp_path):
    modules = tmp_path / "modules-enabled"
    modules.mkdir()
    with_modules = frontdoor.render_nginx_conf(Path("/c.pem"), Path("/k.pem"), modules_dir=modules)
    without = frontdoor.render_nginx_conf(Path("/c.pem"), Path("/k.pem"), modules_dir=tmp_path / "none")
    assert f"include {modules}/*.conf;" in with_modules
    assert "include" not in without.split("worker_processes")[0].replace("# Generated", "")


def test_nginx_temp_paths_stay_under_the_data_folder_for_unprivileged_users():
    conf = frontdoor.render_nginx_conf(Path("/c.pem"), Path("/k.pem"), log_dir="/data/frontdoor")
    assert "client_body_temp_path /data/frontdoor/tmp/body;" in conf


def test_write_frontdoor_raises_until_plex_has_a_certificate(env, tmp_path):
    with pytest.raises(FileNotFoundError):
        frontdoor.write_frontdoor(tmp_path / "data")


def test_preferences_token_is_read(env):
    assert frontdoor.find_plex_paths().token == "tok123"


def test_write_frontdoor_creates_the_nginx_temp_folders(env, tmp_path):
    from tests.unit.test_frontdoor import _make_p12
    p12 = tmp_path / "plex/Library/Application Support/Plex Media Server/Cache/cert-v2.p12"
    p12.parent.mkdir(parents=True)
    _make_p12(p12, "mid1")
    conf = frontdoor.write_frontdoor(tmp_path / "data")
    assert (conf.parent / "tmp" / "body").is_dir() and (conf.parent / "tmp" / "proxy").is_dir()
