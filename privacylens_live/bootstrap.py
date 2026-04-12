"""Idempotent service bootstrap for PrivacyLens-Live.

After ``docker compose up -d`` brings the infrastructure containers online,
this module probes each service for the credentials defined in
:mod:`privacylens_live.config` and provisions whatever's missing:

- **BookStack** — probe-only. The API token is bound to the
  ``bookstack-db-data`` volume; if the volume is fresh, this raises with
  manual ``artisan tinker`` instructions because automating that flow
  reliably is more trouble than it's worth for a research stack.

- **Mattermost** — creates the admin user (open signup is enabled in
  docker-compose.yml so the first ``POST /api/v4/users`` succeeds without
  prior auth) and a default team. Both steps are idempotent: existing
  resources are detected via login / GET probes.

- **GoToSocial** — runs the gotosocial admin CLI inside the container to
  create + confirm the agent user, registers an OAuth2 application via
  the unauthenticated ``POST /api/v1/apps`` endpoint, and exchanges the
  user's credentials for a bearer token via the password grant. The
  resulting token is written back into the :class:`Config` so the caller
  can persist it to ``.env``.

The other three services need no bootstrap:

- **RocketChat** — admin is created on first boot from compose env vars.
- **Mailpit** — no auth.
- **Radicale** — HTTP Basic with hardcoded admin/admin.

Each function takes a :class:`Config` and returns the (possibly mutated)
config so the caller can decide whether to re-write ``.env``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import httpx

from privacylens_live.config import Config


logger = logging.getLogger("bootstrap")


# ── BookStack ──


def bootstrap_bookstack(config: Config) -> Config:
    """Verify the BookStack API token works. Raises with instructions if not.

    Does NOT attempt to create a token automatically — see module docstring.
    """
    headers = {
        "Authorization": (
            f"Token {config.bookstack_token_id}:{config.bookstack_token_secret}"
        )
    }
    try:
        resp = httpx.get(
            f"{config.bookstack_url}/api/books",
            headers=headers,
            params={"count": 1},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"BookStack probe failed (cannot reach {config.bookstack_url}): "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code == 200:
        logger.info("BookStack token OK.")
        return config

    raise RuntimeError(
        f"BookStack token rejected (HTTP {resp.status_code}). The "
        "bookstack-db-data volume appears fresh.\n\n"
        "To provision a token, run:\n"
        "  docker compose -f privacylens_live/docker-compose.yml \\\n"
        "    exec bookstack php artisan tinker\n\n"
        "Then in the tinker shell:\n"
        "  >>> $u = User::find(1);\n"
        "  >>> $t = new \\BookStack\\Api\\ApiToken();\n"
        "  >>> $t->name = 'privacylens';\n"
        f"  >>> $t->token_id = '{config.bookstack_token_id}';\n"
        f"  >>> $t->secret = Hash::make('{config.bookstack_token_secret}');\n"
        "  >>> $t->expires_at = '2099-12-31';\n"
        "  >>> $t->user_id = $u->id;\n"
        "  >>> $t->save();\n\n"
        "(Or pick fresh values and update bookstack_token_id / "
        "bookstack_token_secret in privacylens_live/config.py.)"
    )


# ── Mattermost ──


def bootstrap_mattermost(config: Config) -> Config:
    """Create the admin user (if missing) and the default team (if missing).

    Mattermost open signup is enabled in docker-compose.yml, so the first
    ``POST /api/v4/users`` works without auth. Subsequent runs find the
    admin via login and skip straight to the team check.
    """
    # Step 1: ensure admin exists by trying to log in. If 401, create.
    token = _mattermost_login(config)
    if token is None:
        logger.info("Mattermost admin missing — creating %s", config.mattermost_user)
        try:
            create_resp = httpx.post(
                f"{config.mattermost_url}/api/v4/users",
                json={
                    "username": config.mattermost_user,
                    "password": config.mattermost_password,
                    "email": config.mattermost_email,
                },
                timeout=15.0,
            )
            create_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to create mattermost admin: {exc}") from exc
        token = _mattermost_login(config)
        if token is None:
            raise RuntimeError(
                "Mattermost admin created but subsequent login still failed."
            )
    else:
        logger.info("Mattermost admin %s OK.", config.mattermost_user)

    # Step 2: ensure the default team exists.
    headers = {"Authorization": f"Bearer {token}"}
    team_resp = httpx.get(
        f"{config.mattermost_url}/api/v4/teams/name/{config.mattermost_team}",
        headers=headers,
        timeout=10.0,
    )
    if team_resp.status_code == 404:
        logger.info("Creating mattermost team '%s'", config.mattermost_team)
        try:
            create_team = httpx.post(
                f"{config.mattermost_url}/api/v4/teams",
                headers=headers,
                json={
                    "name": config.mattermost_team,
                    "display_name": config.mattermost_team.title(),
                    "type": "O",
                },
                timeout=15.0,
            )
            create_team.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to create mattermost team: {exc}") from exc
    elif team_resp.status_code == 200:
        logger.info("Mattermost team '%s' OK.", config.mattermost_team)
    else:
        raise RuntimeError(
            f"Unexpected response checking mattermost team: "
            f"HTTP {team_resp.status_code} {team_resp.text[:200]}"
        )

    return config


def _mattermost_login(config: Config) -> str | None:
    """Try to log in. Returns the bearer token, or None if creds rejected."""
    try:
        resp = httpx.post(
            f"{config.mattermost_url}/api/v4/users/login",
            json={
                "login_id": config.mattermost_user,
                "password": config.mattermost_password,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Mattermost unreachable at {config.mattermost_url}: {exc}"
        ) from exc
    if resp.status_code == 200:
        return resp.headers.get("Token") or resp.headers.get("token")
    if resp.status_code in (401, 404):
        return None
    raise RuntimeError(
        f"Unexpected mattermost login response: "
        f"HTTP {resp.status_code} {resp.text[:200]}"
    )


# ── GoToSocial ──


def bootstrap_gotosocial(config: Config, compose_file: Path) -> Config:
    """Provision GoToSocial user + OAuth token. Updates config.gotosocial_token.

    Idempotent: if the existing token already verifies via
    ``/api/v1/accounts/verify_credentials``, returns config unchanged.
    Otherwise runs the gotosocial admin CLI inside the container to create
    + confirm the user, registers an OAuth2 app, then walks GoToSocial's
    scripted browser-style ``authorization_code`` flow to mint a real
    user-bound bearer token.

    Why the scripted browser flow: GoToSocial's ``/oauth/token`` endpoint
    only accepts ``authorization_code`` and ``client_credentials`` grants
    (not ``password``), and ``client_credentials`` produces an app token
    that can't be used for user-bound endpoints like
    ``POST /api/v1/statuses`` (returns 401). The only automated way to
    mint a user token is to script the OAuth dance: GET /oauth/authorize
    (populates session) → POST /auth/sign_in → GET /oauth/authorize again
    (consent page) → POST /oauth/authorize (auto-consent) → exchange the
    redirect code via /oauth/token.
    """
    # Step 1: probe existing token if any.
    if config.gotosocial_token:
        if _gotosocial_token_valid(config):
            logger.info("GoToSocial token OK.")
            return config
        logger.warning("GoToSocial token in config rejected — reprovisioning.")

    # Step 2: create + confirm the user via the admin CLI.
    logger.info("Provisioning GoToSocial user %s...", config.gotosocial_user)
    _gts_admin(
        compose_file,
        "account",
        "create",
        "--username",
        config.gotosocial_user,
        "--email",
        config.gotosocial_email,
        "--password",
        config.gotosocial_password,
    )
    _gts_admin(
        compose_file,
        "account",
        "confirm",
        "--username",
        config.gotosocial_user,
    )

    # Step 3: register an OAuth2 app (unauthenticated endpoint).
    try:
        app_resp = httpx.post(
            f"{config.gotosocial_url}/api/v1/apps",
            data={
                "client_name": "privacylens-live",
                "redirect_uris": "urn:ietf:wg:oauth:2.0:oob",
                "scopes": "read write",
            },
            timeout=15.0,
        )
        app_resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GoToSocial app registration failed: {exc}") from exc
    app = app_resp.json()
    client_id = app["client_id"]
    client_secret = app["client_secret"]

    # Step 4: walk the scripted OAuth code flow to get a user-bound token.
    token = _gotosocial_oauth_token(config, client_id, client_secret)

    config.gotosocial_token = token
    logger.info("GoToSocial token provisioned (%d chars).", len(token))
    return config


def _gotosocial_oauth_token(config: Config, client_id: str, client_secret: str) -> str:
    """Run GoToSocial's scripted ``authorization_code`` flow.

    GoToSocial issues session cookies bound to the configured ``GTS_HOST``
    (``gotosocial`` in our docker-compose), but we connect via
    ``localhost:4000``. httpx correctly refuses to store cookies whose
    ``Domain`` doesn't match the request host, so we parse ``Set-Cookie``
    by hand and re-inject it on each step. This is the documented
    workaround for headless test setups; see GoToSocial issue tracker.
    """
    import re
    from urllib.parse import parse_qs, urlparse

    redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read write",
    }

    def _inject_cookie(client: httpx.Client, response: httpx.Response) -> None:
        set_cookie = response.headers.get("set-cookie", "")
        m = re.match(r"([^=]+)=([^;]+)", set_cookie)
        if m:
            client.cookies.set(m.group(1), m.group(2))

    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            # 4a: GET /oauth/authorize without auth — populates session
            # with the OAuth params and 303-redirects to /auth/sign_in.
            r = client.get(
                f"{config.gotosocial_url}/oauth/authorize", params=auth_params
            )
            _inject_cookie(client, r)

            # 4b: POST /auth/sign_in with the cookie. GoToSocial expects
            # the EMAIL (not the username) here.
            r = client.post(
                f"{config.gotosocial_url}/auth/sign_in",
                data={
                    "username": config.gotosocial_email,
                    "password": config.gotosocial_password,
                },
            )
            _inject_cookie(client, r)

            # 4c: GET /oauth/authorize again — now authenticated. Returns
            # the consent page (HTML) with status 200.
            r = client.get(
                f"{config.gotosocial_url}/oauth/authorize", params=auth_params
            )
            _inject_cookie(client, r)

            # 4d: POST consent — empty body is enough; GoToSocial reads
            # the OAuth params from the session.
            consent = client.post(f"{config.gotosocial_url}/oauth/authorize", data={})
            location = consent.headers.get("location", "")
            code = parse_qs(urlparse(location).query).get("code", [None])[0]
            if not code:
                raise RuntimeError(
                    f"GoToSocial OAuth consent did not return a code: "
                    f"status={consent.status_code} location={location!r}"
                )

        # 4e: Exchange the code for an access token (no cookies needed).
        token_resp = httpx.post(
            f"{config.gotosocial_url}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=15.0,
        )
        token_resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GoToSocial token exchange failed: {exc}") from exc

    token = token_resp.json().get("access_token", "")
    if not token:
        raise RuntimeError(
            f"GoToSocial token exchange returned no access_token: "
            f"{token_resp.text[:200]}"
        )
    return token


def _gotosocial_token_valid(config: Config) -> bool:
    try:
        resp = httpx.get(
            f"{config.gotosocial_url}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {config.gotosocial_token}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _gts_admin(compose_file: Path, *args: str) -> None:
    """Run a gotosocial admin command. 'Already exists' counts as success.

    The gotosocial container ships the binary at ``/gotosocial/gotosocial``
    and does NOT add it to ``$PATH``, so the bare name ``gotosocial``
    fails with ``executable file not found``. Use the absolute path.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "gotosocial",
        "/gotosocial/gotosocial",
        "admin",
        *args,
    ]
    result = subprocess.run(  # noqa: S603 — args are not user input
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr).lower()
    if "already" in output or "exists" in output:
        logger.info("gotosocial admin %s: already provisioned", args[0])
        return
    raise RuntimeError(
        f"gotosocial admin {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


# ── Public entrypoint ──


def bootstrap_all(config: Config, compose_file: Path) -> Config:
    """Run every bootstrap probe in order. Returns the (possibly updated) config.

    The order matters only insofar as bookstack is checked first so the user
    sees the most likely manual-intervention prompt before the harder-to-debug
    gotosocial flow runs.
    """
    config = bootstrap_bookstack(config)
    config = bootstrap_mattermost(config)
    config = bootstrap_gotosocial(config, compose_file)
    return config
