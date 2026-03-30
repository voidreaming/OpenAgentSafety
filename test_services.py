#!/usr/bin/env python3
"""Test all live Docker services that back MCP tool servers.

Tests each service's API directly (same calls the MCP servers make),
verifying the all-live architecture works end-to-end.

Usage:
    python test_services.py [--host localhost]
"""
import argparse
import json
import smtplib
import sys
import time
import uuid
from email.mime.text import MIMEText
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = "localhost"
RESULTS: list[tuple[str, str, bool, str]] = []  # (service, test, passed, detail)


def _http(method: str, url: str, body=None, headers=None, timeout=15):
    hdrs = headers or {}
    if body and isinstance(body, dict):
        hdrs.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode()
    elif body and isinstance(body, str):
        data = body.encode()
    elif body and isinstance(body, bytes):
        data = body
    else:
        data = None
    req = Request(url, data=data, headers=hdrs, method=method)
    with urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        return json.loads(text) if text.strip() else {}


def record(service: str, test: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((service, test, passed, detail))
    print(f"  [{status}] {service}: {test}" + (f" — {detail}" if detail else ""))


# ─── 1. Mailpit (Email) ─────────────────────────────────────────────
def test_mailpit():
    print("\n=== Mailpit (Email) — port 8025/1025 ===")
    base = f"http://{HOST}:8025"

    # Health check
    try:
        resp = _http("GET", f"{base}/api/v1/info")
        record("mailpit", "health check", True, f"version={resp.get('Version', '?')}")
    except Exception as e:
        record("mailpit", "health check", False, str(e))
        return

    # Send email via SMTP
    test_subject = f"OAS Test {uuid.uuid4().hex[:8]}"
    try:
        msg = MIMEText("This is a test email from OAS test_services.py")
        msg["Subject"] = test_subject
        msg["From"] = "agent@the-agent-company.com"
        msg["To"] = "testuser@example.com"
        with smtplib.SMTP(HOST, 1025, timeout=10) as smtp:
            smtp.sendmail("agent@the-agent-company.com", ["testuser@example.com"], msg.as_string())
        record("mailpit", "send email (SMTP)", True, f"subject={test_subject}")
    except Exception as e:
        record("mailpit", "send email (SMTP)", False, str(e))
        return

    time.sleep(1)  # Wait for Mailpit to index

    # Search email via REST
    try:
        from urllib.parse import quote
        resp = _http("GET", f"{base}/api/v1/search?query={quote(test_subject)}")
        messages = resp.get("messages", [])
        found = any(test_subject in m.get("Subject", "") for m in messages)
        record("mailpit", "search email (REST)", found,
               f"found={len(messages)} messages" if found else "email not found in search")
    except Exception as e:
        record("mailpit", "search email (REST)", False, str(e))

    # Read specific message
    try:
        resp = _http("GET", f"{base}/api/v1/search?query={quote(test_subject)}")
        messages = resp.get("messages", [])
        if messages:
            msg_id = messages[0]["ID"]
            detail = _http("GET", f"{base}/api/v1/message/{msg_id}")
            record("mailpit", "read email (REST)", True, f"id={msg_id}")
        else:
            record("mailpit", "read email (REST)", False, "no message to read")
    except Exception as e:
        record("mailpit", "read email (REST)", False, str(e))


# ─── 2. Radicale (Calendar) ─────────────────────────────────────────
def test_radicale():
    print("\n=== Radicale (Calendar) — port 5232 ===")
    base = f"http://{HOST}:5232"

    # Health check — GET /
    try:
        req = Request(f"{base}/", method="GET")
        with urlopen(req, timeout=10) as resp:
            record("radicale", "health check", resp.status == 200)
    except Exception as e:
        record("radicale", "health check", False, str(e))
        return

    # Create calendar collection via MKCOL (parent user collection first)
    cal_path = "/testuser/testcal/"
    try:
        # Create parent user collection
        req = Request(f"{base}/testuser/", method="MKCOL",
                      data=b'', headers={"Content-Type": "application/xml"})
        try:
            urlopen(req, timeout=10)
        except HTTPError:
            pass  # Already exists

        mkcol_body = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            '<mkcol xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<set><prop><resourcetype><collection/><C:calendar/></resourcetype>'
            '<displayname>Test Calendar</displayname></prop></set></mkcol>'
        )
        req = Request(f"{base}{cal_path}", data=mkcol_body.encode(),
                      headers={"Content-Type": "application/xml"}, method="MKCOL")
        try:
            urlopen(req, timeout=10)
        except HTTPError as e:
            if e.code not in (405, 409):  # already exists
                raise
        record("radicale", "create collection (MKCOL)", True)
    except Exception as e:
        record("radicale", "create collection (MKCOL)", False, str(e))
        return

    # Create event via PUT
    event_id = uuid.uuid4().hex[:12]
    ical = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{event_id}\r\nSUMMARY:OAS Test Event\r\n"
        "DTSTART:20260320T100000Z\r\nDTEND:20260320T110000Z\r\n"
        "DESCRIPTION:Test event from test_services.py\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    try:
        req = Request(f"{base}{cal_path}{event_id}.ics", data=ical.encode(),
                      headers={"Content-Type": "text/calendar"}, method="PUT")
        urlopen(req, timeout=10)
        record("radicale", "create event (PUT)", True, f"uid={event_id}")
    except Exception as e:
        record("radicale", "create event (PUT)", False, str(e))

    # Read event via GET
    try:
        req = Request(f"{base}{cal_path}{event_id}.ics", method="GET")
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            found = "OAS Test Event" in body
            record("radicale", "read event (GET)", found)
    except Exception as e:
        record("radicale", "read event (GET)", False, str(e))

    # List events via PROPFIND
    try:
        propfind = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>'
        )
        req = Request(f"{base}{cal_path}", data=propfind.encode(),
                      headers={"Content-Type": "application/xml", "Depth": "1"}, method="PROPFIND")
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            has_ics = ".ics" in body
            record("radicale", "list events (PROPFIND)", has_ics)
    except Exception as e:
        record("radicale", "list events (PROPFIND)", False, str(e))


# ─── 3. Wiki.js (Docs) ──────────────────────────────────────────────
def test_wikijs():
    print("\n=== Wiki.js (Docs) — port 3001 ===")
    base = f"http://{HOST}:3001"

    # Health check
    try:
        req = Request(f"{base}/healthz", method="GET")
        with urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
            record("wikijs", "health check", ok)
    except HTTPError as e:
        # Wiki.js may not have /healthz — try GraphQL
        try:
            resp = _http("POST", f"{base}/graphql",
                         body={"query": "{ site { config { title } } }"})
            record("wikijs", "health check (graphql)", True)
        except Exception as e2:
            record("wikijs", "health check", False, str(e2))
            return
    except Exception as e:
        record("wikijs", "health check", False, str(e))
        return

    # Get API token — login as admin
    token = ""
    try:
        resp = _http("POST", f"{base}/graphql", body={
            "query": """mutation { authentication { login(username: "agent@company.com", password: "theagentcompany", strategy: "local") { responseResult { succeeded message } jwt } } }"""
        })
        login_data = resp.get("data", {}).get("authentication", {}).get("login", {})
        jwt = login_data.get("jwt", "")
        if jwt:
            token = jwt
            record("wikijs", "login (GraphQL)", True)
        else:
            msg = login_data.get("responseResult", {}).get("message", "no jwt")
            record("wikijs", "login (GraphQL)", False, msg)
            return
    except Exception as e:
        record("wikijs", "login (GraphQL)", False, str(e))
        return

    headers = {"Authorization": f"Bearer {token}"}

    # Create page
    test_path = f"oas-test-{uuid.uuid4().hex[:8]}"
    try:
        resp = _http("POST", f"{base}/graphql", body={
            "query": """mutation ($content: String!, $path: String!, $title: String!, $description: String!) {
                pages { create(content: $content, path: $path, title: $title, description: $description,
                    editor: "markdown", locale: "en", isPublished: true, isPrivate: false, tags: []) {
                    responseResult { succeeded message } page { id path title }
                } }
            }""",
            "variables": {"content": "# Test\nThis is an OAS test page.", "path": test_path, "title": "OAS Test Page", "description": "Test page for OAS"}
        }, headers=headers)
        create_data = resp.get("data", {}).get("pages", {}).get("create", {})
        succeeded = create_data.get("responseResult", {}).get("succeeded", False)
        page_id = create_data.get("page", {}).get("id")
        record("wikijs", "create page (GraphQL)", succeeded, f"id={page_id}" if succeeded else
               create_data.get("responseResult", {}).get("message", "?"))
    except Exception as e:
        record("wikijs", "create page (GraphQL)", False, str(e))
        page_id = None

    # Read page
    if page_id:
        try:
            resp = _http("POST", f"{base}/graphql", body={
                "query": f"{{ pages {{ single(id: {page_id}) {{ id title content path }} }} }}"
            }, headers=headers)
            page = resp.get("data", {}).get("pages", {}).get("single")
            record("wikijs", "read page (GraphQL)", page is not None and "OAS test" in page.get("content", ""))
        except Exception as e:
            record("wikijs", "read page (GraphQL)", False, str(e))

    # Search
    try:
        resp = _http("POST", f"{base}/graphql", body={
            "query": '{ pages { search(query: "OAS") { results { id title } totalHits } } }'
        }, headers=headers)
        search_data = resp.get("data", {}).get("pages", {}).get("search", {})
        record("wikijs", "search pages (GraphQL)", True, f"hits={search_data.get('totalHits', 0)}")
    except Exception as e:
        record("wikijs", "search pages (GraphQL)", False, str(e))


# ─── 4. Pleroma (Social Media) ──────────────────────────────────────
def test_pleroma():
    print("\n=== Pleroma/Akkoma (Social Media) — port 4000 ===")
    base = f"http://{HOST}:4000"

    # Health check
    try:
        resp = _http("GET", f"{base}/api/v1/instance")
        title = resp.get("title", "")
        record("pleroma", "health check", True, f"instance={title}")
    except Exception as e:
        record("pleroma", "health check", False, str(e))
        return

    # Register a test user (requires: create app → get app token → register with app token)
    test_user = f"oastest{uuid.uuid4().hex[:6]}"
    token = ""
    try:
        # Step 1: Create OAuth app
        app_resp = _http("POST", f"{base}/api/v1/apps", body={
            "client_name": "OAS Test",
            "redirect_uris": "urn:ietf:wg:oauth:2.0:oob",
            "scopes": "read write"
        })
        client_id = app_resp.get("client_id", "")
        client_secret = app_resp.get("client_secret", "")

        # Step 2: Get app token via client_credentials grant
        app_token_resp = _http("POST", f"{base}/oauth/token", body={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        app_token = app_token_resp.get("access_token", "")

        # Step 3: Register user with app token
        reg_resp = _http("POST", f"{base}/api/v1/accounts", body={
            "username": test_user,
            "email": f"{test_user}@test.local",
            "password": "testpassword123",
            "agreement": True,
            "locale": "en"
        }, headers={"Authorization": f"Bearer {app_token}"})
        token = reg_resp.get("access_token", "")
        if not token:
            # Fallback: try password grant
            tok_resp = _http("POST", f"{base}/oauth/token", body={
                "grant_type": "password",
                "client_id": client_id,
                "client_secret": client_secret,
                "username": test_user,
                "password": "testpassword123"
            })
            token = tok_resp.get("access_token", "")
        record("pleroma", "register user + get token", bool(token), f"user={test_user}")
    except Exception as e:
        record("pleroma", "register user + get token", False, str(e))
        return

    auth = {"Authorization": f"Bearer {token}"}

    # Post a status
    try:
        resp = _http("POST", f"{base}/api/v1/statuses",
                      body={"status": f"Hello from OAS test! {uuid.uuid4().hex[:8]}"},
                      headers=auth)
        post_id = resp.get("id", "")
        record("pleroma", "create post", bool(post_id), f"id={post_id}")
    except Exception as e:
        record("pleroma", "create post", False, str(e))
        post_id = None

    # Read timeline
    try:
        req = Request(f"{base}/api/v1/timelines/home", headers=auth, method="GET")
        with urlopen(req, timeout=10) as resp:
            statuses = json.loads(resp.read().decode())
            record("pleroma", "read timeline", isinstance(statuses, list),
                   f"count={len(statuses)}")
    except Exception as e:
        record("pleroma", "read timeline", False, str(e))

    # Read specific post
    if post_id:
        try:
            resp = _http("GET", f"{base}/api/v1/statuses/{post_id}", headers=auth)
            record("pleroma", "read post", resp.get("id") == post_id)
        except Exception as e:
            record("pleroma", "read post", False, str(e))


# ─── 5. ownCloud (Files) ────────────────────────────────────────────
def test_owncloud():
    print("\n=== ownCloud (Files) — port 8092 ===")
    import base64
    base = f"http://{HOST}:8092"
    creds = base64.b64encode(b"theagentcompany:theagentcompany").decode()
    auth = {"Authorization": f"Basic {creds}"}
    webdav = f"{base}/remote.php/dav/files/theagentcompany"

    # Health check — GET WebDAV root
    try:
        propfind = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/></D:prop></D:propfind>'
        )
        req = Request(f"{webdav}/", data=propfind.encode(),
                      headers={**auth, "Content-Type": "application/xml", "Depth": "0"},
                      method="PROPFIND")
        with urlopen(req, timeout=15) as resp:
            record("owncloud", "health check (PROPFIND)", resp.status in (200, 207))
    except Exception as e:
        record("owncloud", "health check (PROPFIND)", False, str(e))
        return

    # Write file via PUT
    test_file = f"/oas-test-{uuid.uuid4().hex[:8]}.txt"
    test_content = "Hello from OAS test_services.py"
    try:
        req = Request(f"{webdav}{test_file}", data=test_content.encode(),
                      headers={**auth, "Content-Type": "text/plain"}, method="PUT")
        urlopen(req, timeout=15)
        record("owncloud", "write file (PUT)", True, f"path={test_file}")
    except Exception as e:
        record("owncloud", "write file (PUT)", False, str(e))

    # Read file via GET
    try:
        req = Request(f"{webdav}{test_file}", headers=auth, method="GET")
        with urlopen(req, timeout=15) as resp:
            content = resp.read().decode()
            record("owncloud", "read file (GET)", content == test_content)
    except Exception as e:
        record("owncloud", "read file (GET)", False, str(e))

    # List files via PROPFIND
    try:
        propfind = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/></D:prop></D:propfind>'
        )
        req = Request(f"{webdav}/", data=propfind.encode(),
                      headers={**auth, "Content-Type": "application/xml", "Depth": "1"},
                      method="PROPFIND")
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            has_file = test_file.lstrip("/") in body
            record("owncloud", "list files (PROPFIND)", has_file)
    except Exception as e:
        record("owncloud", "list files (PROPFIND)", False, str(e))


# ─── 6. RocketChat (Messaging) ──────────────────────────────────────
def test_rocketchat():
    print("\n=== RocketChat (Messaging) — port 3000 ===")
    base = f"http://{HOST}:3000"

    # Login
    try:
        resp = _http("POST", f"{base}/api/v1/login",
                      body={"user": "theagentcompany", "password": "theagentcompany"})
        status = resp.get("status")
        token = resp.get("data", {}).get("authToken", "")
        user_id = resp.get("data", {}).get("userId", "")
        record("rocketchat", "login", status == "success", f"userId={user_id}")
    except Exception as e:
        record("rocketchat", "login", False, str(e))
        return

    auth = {"X-Auth-Token": token, "X-User-Id": user_id}

    # List channels
    try:
        resp = _http("GET", f"{base}/api/v1/channels.list?count=5",
                      headers=auth)
        channels = resp.get("channels", [])
        record("rocketchat", "list channels", len(channels) > 0, f"count={len(channels)}")
    except Exception as e:
        record("rocketchat", "list channels", False, str(e))

    # Send message to general channel
    test_msg = f"OAS test message {uuid.uuid4().hex[:8]}"
    try:
        resp = _http("POST", f"{base}/api/v1/chat.sendMessage",
                      body={"message": {"rid": "GENERAL", "msg": test_msg}},
                      headers=auth)
        msg_id = resp.get("message", {}).get("_id", "")
        record("rocketchat", "send message", bool(msg_id), f"id={msg_id}")
    except Exception as e:
        record("rocketchat", "send message", False, str(e))

    # Search messages
    try:
        resp = _http("GET", f"{base}/api/v1/chat.search?roomId=GENERAL&searchText=OAS",
                      headers=auth)
        msgs = resp.get("messages", [])
        record("rocketchat", "search messages", len(msgs) > 0, f"found={len(msgs)}")
    except Exception as e:
        record("rocketchat", "search messages", False, str(e))


# ─── 7. GitLab ──────────────────────────────────────────────────────
def test_gitlab():
    print("\n=== GitLab — port 8929 ===")
    base = f"http://{HOST}:8929"
    auth = {"PRIVATE-TOKEN": os.getenv("GITLAB_TOKEN", "")}

    # Health check
    try:
        resp = _http("GET", f"{base}/api/v4/version", headers=auth)
        record("gitlab", "health check", "version" in resp, f"version={resp.get('version', '?')}")
    except Exception as e:
        record("gitlab", "health check", False, str(e))
        return

    # List projects
    try:
        resp = _http("GET", f"{base}/api/v4/projects?per_page=5", headers=auth)
        if isinstance(resp, list):
            record("gitlab", "list projects", True, f"count={len(resp)}")
            if resp:
                project_id = resp[0]["id"]
                project_name = resp[0].get("name", "")

                # List files in first project
                try:
                    tree = _http("GET", f"{base}/api/v4/projects/{project_id}/repository/tree?per_page=10", headers=auth)
                    if isinstance(tree, list):
                        record("gitlab", "list files", True, f"project={project_name}, entries={len(tree)}")
                    else:
                        record("gitlab", "list files", False, f"unexpected: {tree}")
                except Exception as e:
                    record("gitlab", "list files", False, str(e))
        else:
            record("gitlab", "list projects", False, f"unexpected: {resp}")
    except Exception as e:
        record("gitlab", "list projects", False, str(e))


# ─── 8. Plane ───────────────────────────────────────────────────────
def test_plane():
    print("\n=== Plane — port 8091 ===")
    base = f"http://{HOST}:8091"
    auth = {"x-api-key": os.getenv("PLANE_TOKEN", ""), "Content-Type": "application/json"}

    # List projects
    try:
        resp = _http("GET", f"{base}/api/v1/workspaces/tac/projects/", headers=auth)
        projects = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        record("plane", "list projects", len(projects) > 0, f"count={len(projects)}")
        if not projects:
            return
        project_id = projects[0]["id"]
        project_name = projects[0].get("name", "")
    except Exception as e:
        record("plane", "list projects", False, str(e))
        return

    # List issues
    try:
        resp = _http("GET", f"{base}/api/v1/workspaces/tac/projects/{project_id}/issues/", headers=auth)
        issues = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        record("plane", "list issues", True, f"project={project_name}, count={len(issues)}")
    except Exception as e:
        record("plane", "list issues", False, str(e))

    # Create issue
    try:
        test_issue_name = f"OAS Test Issue {uuid.uuid4().hex[:8]}"
        resp = _http("POST", f"{base}/api/v1/workspaces/tac/projects/{project_id}/issues/",
                      body={"name": test_issue_name}, headers=auth)
        issue_id = resp.get("id", "")
        record("plane", "create issue", bool(issue_id), f"name={test_issue_name}")
    except Exception as e:
        record("plane", "create issue", False, str(e))
        issue_id = None

    # Read issue
    if issue_id:
        try:
            resp = _http("GET", f"{base}/api/v1/workspaces/tac/projects/{project_id}/issues/{issue_id}/",
                          headers=auth)
            record("plane", "read issue", resp.get("id") == issue_id)
        except Exception as e:
            record("plane", "read issue", False, str(e))


# ─── 9. Memory (local JSON — no Docker service) ─────────────────────
def test_memory():
    print("\n=== Memory (local JSON scratchpad) ===")
    import tempfile, os

    state_file = os.path.join(tempfile.mkdtemp(), "test_memory.json")

    # Store
    memories = {}
    key, value = "test_key", "test_value_123"
    memories[key] = {"key": key, "value": value, "tags": ["test"],
                     "created_at": "2026-03-18T00:00:00Z", "updated_at": "2026-03-18T00:00:00Z"}
    with open(state_file, "w") as f:
        json.dump(memories, f)
    record("memory", "store (JSON write)", os.path.exists(state_file))

    # Recall
    with open(state_file) as f:
        loaded = json.load(f)
    found = loaded.get(key, {}).get("value") == value
    record("memory", "recall (JSON read)", found)

    # Search
    matches = [v for v in loaded.values() if value[:5] in v.get("value", "")]
    record("memory", "search (substring)", len(matches) > 0)

    # Clean up
    os.unlink(state_file)
    record("memory", "cleanup", True)


# ─── Main ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Test all OAS live Docker services")
    parser.add_argument("--host", default="localhost", help="Service hostname")
    parser.add_argument("--wait", type=int, default=30, help="Seconds to wait for services to boot")
    args = parser.parse_args()

    global HOST
    HOST = args.host

    print(f"Testing OAS services on {HOST}")
    print(f"Waiting {args.wait}s for services to be ready...")
    time.sleep(args.wait)

    test_mailpit()
    test_radicale()
    test_wikijs()
    test_pleroma()
    test_owncloud()
    test_rocketchat()
    test_gitlab()
    test_plane()
    test_memory()

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, _, p, _ in RESULTS if p)
    failed = sum(1 for _, _, p, _ in RESULTS if not p)
    total = len(RESULTS)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")

    if failed:
        print("\nFailed tests:")
        for svc, test, p, detail in RESULTS:
            if not p:
                print(f"  - {svc}: {test} — {detail}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
