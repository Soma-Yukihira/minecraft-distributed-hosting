"""Changing a password in the application, resetting a forgotten one on the VPS."""

from mdh_harness import PASSWORD

import mdh_server.__main__ as cli

NEW = "a brand new password"


def test_change_password_keeps_this_session_and_ends_the_others(h) -> None:
    here = h.register("oscar")
    other_pc = h.login("oscar")
    r = h.post(here, "/me/password", {"current_password": "wrong one!!", "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"

    r = h.post(here, "/me/password", {"current_password": PASSWORD, "new_password": NEW})
    assert r.status_code == 204
    assert h.get(here, "/me").status_code == 200                      # this PC carries on
    assert h.get(other_pc, "/me").status_code == 401                  # the other one is logged out
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": other_pc.refresh_token})
    assert r.status_code == 401
    r = h.client.post("/api/v1/auth/refresh", json={"refresh_token": here.refresh_token})
    assert r.status_code == 200                                       # still renews its session

    # (Logging in with the old password is the same brute-force bucket: one try is enough here.)
    assert h.client.post("/api/v1/auth/login", json={"username": "oscar", "password": NEW}).status_code == 200


def test_new_password_must_be_long_enough(h) -> None:
    u = h.register("oscar")
    r = h.post(u, "/me/password", {"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422


def test_reset_password_on_the_vps(harness_factory, tmp_path, monkeypatch, capsys) -> None:
    h = harness_factory()
    session = h.register("Aaron")
    monkeypatch.setenv("MDH_DATABASE_PATH", str(tmp_path / "db" / "mdh.sqlite3"))
    answers = iter([NEW, NEW])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["reset-password", "aaron"]) == 0                 # any case
    assert "must log in again" in capsys.readouterr().out
    assert h.get(session, "/me").status_code == 401                   # forgotten PCs are logged out
    r = h.client.post("/api/v1/auth/login", json={"username": "Aaron", "password": PASSWORD})
    assert r.status_code == 401                                       # the old one no longer works
    assert h.client.post("/api/v1/auth/login", json={"username": "Aaron", "password": NEW}).status_code == 200

    assert cli.main(["reset-password", "nobody"]) == 1
