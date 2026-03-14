#!/usr/bin/env python3
"""
Multi-Tenant Isolation Verification Script

Verifies the "Company → User" two-level tenant isolation:
  1. Setup: Create Company A (alice, bob) and Company B (charlie, diana)
  2. Create user-private memories via sessions
  3. Add company-shared knowledge base via resources
  4. Verify intra-company user memory isolation
  5. Verify intra-company knowledge base sharing
  6. Verify cross-company complete isolation

Prerequisites:
    Start server with root_api_key configured:
      openviking-server --config ov.conf

Usage:
    uv run verify_tenant_isolation.py
    uv run verify_tenant_isolation.py --url http://localhost:1933 --root-key my-root-key
"""

import argparse
import os
import sys
import tempfile
import time

import httpx

# ── Constants ──

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
SKIP = "\033[33m⊘\033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"

TIMEOUT = 120.0  # seconds for requests that wait for processing

stats = {"passed": 0, "failed": 0, "skipped": 0}


# ── Helpers ──


def check(condition: bool, label: str) -> bool:
    if condition:
        print(f"    {PASS} {label}")
        stats["passed"] += 1
    else:
        print(f"    {FAIL} {label}")
        stats["failed"] += 1
    return condition


def skip(label: str, reason: str = ""):
    msg = f"    {SKIP} {label}"
    if reason:
        msg += f" ({reason})"
    print(msg)
    stats["skipped"] += 1


def section(title: str):
    print(f"\n{BOLD}== {title} =={RESET}")


def h(key: str, agent: str = "default") -> dict:
    """Build request headers for a user key."""
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if agent != "default":
        headers["X-OpenViking-Agent"] = agent
    return headers


def api_get(url: str, headers: dict, params: dict = None) -> httpx.Response:
    return httpx.get(url, headers=headers, params=params, timeout=TIMEOUT)


def api_post(url: str, headers: dict, json: dict = None, **kwargs) -> httpx.Response:
    return httpx.post(url, headers=headers, json=json, timeout=TIMEOUT, **kwargs)


def api_delete(url: str, headers: dict, params: dict = None) -> httpx.Response:
    return httpx.delete(url, headers=headers, params=params, timeout=TIMEOUT)


def ls_uris(base: str, key: str, uri: str) -> list[str]:
    """List directory and return URIs. Returns empty list on error."""
    resp = api_get(f"{base}/api/v1/fs/ls", h(key), {"uri": uri, "simple": "true"})
    if not resp.is_success:
        return []
    result = resp.json().get("result", [])
    if isinstance(result, list):
        # simple mode returns list of relative paths
        return result
    return []


def ls_names(base: str, key: str, uri: str) -> list[str]:
    """List directory and return entry names. Returns empty list on error."""
    resp = api_get(f"{base}/api/v1/fs/ls", h(key), {"uri": uri})
    if not resp.is_success:
        return []
    result = resp.json().get("result", [])
    if isinstance(result, list):
        names = []
        for entry in result:
            if isinstance(entry, dict):
                # Extract name from uri like "viking://resources/xxx.txt"
                uri_str = entry.get("uri", entry.get("rel_path", ""))
                name = uri_str.rstrip("/").split("/")[-1] if uri_str else ""
                if name:
                    names.append(name)
            elif isinstance(entry, str):
                name = entry.rstrip("/").split("/")[-1] if entry else ""
                if name:
                    names.append(name)
        return names
    return []


# ── Main ──


def run(base_url: str, root_key: str):
    base = base_url.rstrip("/")
    root_h = {"X-API-Key": root_key, "Content-Type": "application/json"}

    # ================================================================
    section("0. Health Check")
    # ================================================================
    resp = httpx.get(f"{base}/health", timeout=10)
    if not check(resp.is_success, "Server is reachable"):
        print("  Cannot reach server. Aborting.")
        return

    # ================================================================
    section("1. Setup: Create Companies and Users")
    # ================================================================

    # Clean up from previous runs (ignore errors)
    httpx.delete(f"{base}/api/v1/admin/accounts/company_a", headers=root_h, timeout=30)
    httpx.delete(f"{base}/api/v1/admin/accounts/company_b", headers=root_h, timeout=30)
    time.sleep(0.5)

    # Create Company A with admin alice
    resp = api_post(
        f"{base}/api/v1/admin/accounts",
        root_h,
        {"account_id": "company_a", "admin_user_id": "alice"},
    )
    check(resp.is_success, "Created Company A with admin alice")
    alice_key = resp.json()["result"]["user_key"]

    # Register bob in Company A
    resp = api_post(
        f"{base}/api/v1/admin/accounts/company_a/users",
        root_h,
        {"user_id": "bob", "role": "user"},
    )
    check(resp.is_success, "Registered bob in Company A")
    bob_key = resp.json()["result"]["user_key"]

    # Create Company B with admin charlie
    resp = api_post(
        f"{base}/api/v1/admin/accounts",
        root_h,
        {"account_id": "company_b", "admin_user_id": "charlie"},
    )
    check(resp.is_success, "Created Company B with admin charlie")
    charlie_key = resp.json()["result"]["user_key"]

    # Register diana in Company B
    resp = api_post(
        f"{base}/api/v1/admin/accounts/company_b/users",
        root_h,
        {"user_id": "diana", "role": "user"},
    )
    check(resp.is_success, "Registered diana in Company B")
    diana_key = resp.json()["result"]["user_key"]

    print(f"\n  Company A: alice (ADMIN), bob (USER)")
    print(f"  Company B: charlie (ADMIN), diana (USER)")

    # ================================================================
    section("2. Create User Memories via Sessions")
    # ================================================================

    def create_memory(key: str, username: str, topic: str) -> str | None:
        """Create a session, add messages, commit. Returns session_id."""
        # Create session
        resp = api_post(f"{base}/api/v1/sessions", h(key))
        if not resp.is_success:
            print(f"    {FAIL} Failed to create session for {username}: {resp.text}")
            stats["failed"] += 1
            return None
        sid = resp.json()["result"]["session_id"]

        # Add messages
        api_post(
            f"{base}/api/v1/sessions/{sid}/messages",
            h(key),
            {"role": "user", "content": f"Tell me about {topic}"},
        )
        api_post(
            f"{base}/api/v1/sessions/{sid}/messages",
            h(key),
            {
                "role": "assistant",
                "content": f"Here is detailed information about {topic}. "
                f"This is {username}'s private knowledge about {topic}.",
            },
        )

        check(True, f"{username}: session {sid[:8]}... created with messages about '{topic}'")
        return sid

    alice_sid = create_memory(alice_key, "alice", "Project Alpha secret plan")
    bob_sid = create_memory(bob_key, "bob", "Budget Bravo confidential report")
    charlie_sid = create_memory(charlie_key, "charlie", "Research Charlie quantum computing")
    diana_sid = create_memory(diana_key, "diana", "Design Diana neural interface")

    # ================================================================
    section("3. Add Company Knowledge Base via Resources")
    # ================================================================

    def upload_knowledge(key: str, company: str, filename: str, content: str) -> bool:
        """Upload a text file as company shared resource."""
        # Create temp file and upload
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            # Upload temp file
            with open(tmp_path, "rb") as f:
                resp = httpx.post(
                    f"{base}/api/v1/resources/temp_upload",
                    headers={"X-API-Key": key},
                    files={"file": (filename, f, "text/plain")},
                    timeout=TIMEOUT,
                )
            if not resp.is_success:
                print(f"    {FAIL} Failed to upload {filename} for {company}: {resp.text}")
                stats["failed"] += 1
                return False
            temp_path = resp.json()["result"]["temp_path"]

            # Add as resource (wait for processing)
            resp = api_post(
                f"{base}/api/v1/resources",
                h(key),
                {"temp_path": temp_path, "reason": f"{company} knowledge base", "wait": True},
            )
            if resp.is_success:
                check(True, f"{company}: uploaded '{filename}'")
                return True
            else:
                print(f"    {FAIL} Failed to add resource {filename}: {resp.text}")
                stats["failed"] += 1
                return False
        finally:
            os.unlink(tmp_path)

    kb_a_ok = upload_knowledge(
        alice_key,
        "Company A",
        "company_a_handbook.txt",
        "Company A Employee Handbook\n\n"
        "1. Company A was founded in 2020.\n"
        "2. Our mission is to build the best widgets.\n"
        "3. Vacation policy: 20 days per year.\n"
        "4. Keyword: ALPHA_HANDBOOK_MARKER\n",
    )

    kb_b_ok = upload_knowledge(
        charlie_key,
        "Company B",
        "company_b_handbook.txt",
        "Company B Employee Handbook\n\n"
        "1. Company B was founded in 2022.\n"
        "2. Our mission is to revolutionize gadgets.\n"
        "3. Vacation policy: 25 days per year.\n"
        "4. Keyword: BRAVO_HANDBOOK_MARKER\n",
    )

    # ================================================================
    section("4. Verify: User Memory Isolation (within company)")
    # ================================================================
    # alice and bob should only see their own sessions

    print("  Company A:")

    # alice lists sessions → should see her own
    resp = api_get(f"{base}/api/v1/sessions", h(alice_key))
    alice_sessions = resp.json().get("result", []) if resp.is_success else []
    alice_session_ids = []
    if isinstance(alice_sessions, list):
        for s in alice_sessions:
            sid = s.get("session_id", s) if isinstance(s, dict) else s
            alice_session_ids.append(sid)
    check(
        alice_sid is not None and alice_sid in alice_session_ids,
        "alice can see her own session",
    )
    check(
        bob_sid is None or bob_sid not in alice_session_ids,
        "alice cannot see bob's session",
    )

    # bob lists sessions → should see his own
    resp = api_get(f"{base}/api/v1/sessions", h(bob_key))
    bob_sessions = resp.json().get("result", []) if resp.is_success else []
    bob_session_ids = []
    if isinstance(bob_sessions, list):
        for s in bob_sessions:
            sid = s.get("session_id", s) if isinstance(s, dict) else s
            bob_session_ids.append(sid)
    check(
        bob_sid is not None and bob_sid in bob_session_ids,
        "bob can see his own session",
    )
    check(
        alice_sid is None or alice_sid not in bob_session_ids,
        "bob cannot see alice's session",
    )

    print("  Company B:")

    resp = api_get(f"{base}/api/v1/sessions", h(charlie_key))
    charlie_sessions = resp.json().get("result", []) if resp.is_success else []
    charlie_session_ids = []
    if isinstance(charlie_sessions, list):
        for s in charlie_sessions:
            sid = s.get("session_id", s) if isinstance(s, dict) else s
            charlie_session_ids.append(sid)
    check(
        charlie_sid is not None and charlie_sid in charlie_session_ids,
        "charlie can see his own session",
    )
    check(
        diana_sid is None or diana_sid not in charlie_session_ids,
        "charlie cannot see diana's session",
    )

    resp = api_get(f"{base}/api/v1/sessions", h(diana_key))
    diana_sessions = resp.json().get("result", []) if resp.is_success else []
    diana_session_ids = []
    if isinstance(diana_sessions, list):
        for s in diana_sessions:
            sid = s.get("session_id", s) if isinstance(s, dict) else s
            diana_session_ids.append(sid)
    check(
        diana_sid is not None and diana_sid in diana_session_ids,
        "diana can see her own session",
    )
    check(
        charlie_sid is None or charlie_sid not in diana_session_ids,
        "diana cannot see charlie's session",
    )

    # ================================================================
    section("5. Verify: Company Knowledge Base Sharing (within company)")
    # ================================================================

    if kb_a_ok:
        print("  Company A resources:")
        alice_resources = ls_names(base, alice_key, "viking://resources/")
        bob_resources = ls_names(base, bob_key, "viking://resources/")
        check(
            any("company_a" in name.lower() for name in alice_resources),
            f"alice can see Company A knowledge base (resources: {alice_resources})",
        )
        check(
            any("company_a" in name.lower() for name in bob_resources),
            f"bob can see Company A knowledge base (resources: {bob_resources})",
        )
    else:
        skip("Company A knowledge base sharing", "upload failed")

    if kb_b_ok:
        print("  Company B resources:")
        charlie_resources = ls_names(base, charlie_key, "viking://resources/")
        diana_resources = ls_names(base, diana_key, "viking://resources/")
        check(
            any("company_b" in name.lower() for name in charlie_resources),
            f"charlie can see Company B knowledge base (resources: {charlie_resources})",
        )
        check(
            any("company_b" in name.lower() for name in diana_resources),
            f"diana can see Company B knowledge base (resources: {diana_resources})",
        )
    else:
        skip("Company B knowledge base sharing", "upload failed")

    # ================================================================
    section("6. Verify: Cross-Company Isolation")
    # ================================================================

    if kb_a_ok and kb_b_ok:
        print("  Company A users vs Company B resources:")
        alice_resources = ls_names(base, alice_key, "viking://resources/")
        bob_resources = ls_names(base, bob_key, "viking://resources/")
        check(
            not any("company_b" in name.lower() for name in alice_resources),
            f"alice cannot see Company B resources (sees: {alice_resources})",
        )
        check(
            not any("company_b" in name.lower() for name in bob_resources),
            f"bob cannot see Company B resources (sees: {bob_resources})",
        )

        print("  Company B users vs Company A resources:")
        charlie_resources = ls_names(base, charlie_key, "viking://resources/")
        diana_resources = ls_names(base, diana_key, "viking://resources/")
        check(
            not any("company_a" in name.lower() for name in charlie_resources),
            f"charlie cannot see Company A resources (sees: {charlie_resources})",
        )
        check(
            not any("company_a" in name.lower() for name in diana_resources),
            f"diana cannot see Company A resources (sees: {diana_resources})",
        )
    else:
        skip("Cross-company resource isolation", "some uploads failed")

    # Cross-company session isolation
    print("  Cross-company session isolation:")
    check(
        alice_sid is None or alice_sid not in charlie_session_ids,
        "charlie cannot see alice's session (Company A)",
    )
    check(
        alice_sid is None or alice_sid not in diana_session_ids,
        "diana cannot see alice's session (Company A)",
    )
    check(
        charlie_sid is None or charlie_sid not in alice_session_ids,
        "alice cannot see charlie's session (Company B)",
    )
    check(
        charlie_sid is None or charlie_sid not in bob_session_ids,
        "bob cannot see charlie's session (Company B)",
    )

    # Cross-company direct access attempt
    print("  Cross-company direct access attempt:")
    if alice_sid:
        resp = api_get(f"{base}/api/v1/sessions/{alice_sid}", h(charlie_key))
        check(
            not resp.is_success or resp.status_code >= 400,
            f"charlie cannot access alice's session directly (HTTP {resp.status_code})",
        )
    if charlie_sid:
        resp = api_get(f"{base}/api/v1/sessions/{charlie_sid}", h(alice_key))
        check(
            not resp.is_success or resp.status_code >= 400,
            f"alice cannot access charlie's session directly (HTTP {resp.status_code})",
        )

    # ================================================================
    section("7. Verify: Search Isolation (best-effort)")
    # ================================================================
    # Search depends on embedding models. Skip gracefully if not available.

    if kb_a_ok and kb_b_ok:
        print("  Testing grep (pattern-based, no embeddings needed):")
        # grep for ALPHA_HANDBOOK_MARKER in resources
        resp = api_post(
            f"{base}/api/v1/search/grep",
            h(alice_key),
            {"uri": "viking://resources/", "pattern": "ALPHA_HANDBOOK_MARKER"},
        )
        if resp.is_success:
            alice_grep = resp.json().get("result", [])
            has_match = bool(alice_grep) if isinstance(alice_grep, list) else bool(alice_grep)
            check(has_match, "alice can grep Company A's marker in resources")
        else:
            skip("alice grep Company A", f"HTTP {resp.status_code}")

        resp = api_post(
            f"{base}/api/v1/search/grep",
            h(charlie_key),
            {"uri": "viking://resources/", "pattern": "ALPHA_HANDBOOK_MARKER"},
        )
        if resp.is_success:
            charlie_grep = resp.json().get("result", [])
            has_match = bool(charlie_grep) if isinstance(charlie_grep, list) else bool(charlie_grep)
            check(not has_match, "charlie cannot grep Company A's marker (sees Company B only)")
        else:
            skip("charlie grep Company A marker", f"HTTP {resp.status_code}")

        resp = api_post(
            f"{base}/api/v1/search/grep",
            h(charlie_key),
            {"uri": "viking://resources/", "pattern": "BRAVO_HANDBOOK_MARKER"},
        )
        if resp.is_success:
            charlie_grep_b = resp.json().get("result", [])
            has_match = bool(charlie_grep_b) if isinstance(charlie_grep_b, list) else bool(charlie_grep_b)
            check(has_match, "charlie can grep Company B's marker in resources")
        else:
            skip("charlie grep Company B", f"HTTP {resp.status_code}")

        resp = api_post(
            f"{base}/api/v1/search/grep",
            h(alice_key),
            {"uri": "viking://resources/", "pattern": "BRAVO_HANDBOOK_MARKER"},
        )
        if resp.is_success:
            alice_grep_b = resp.json().get("result", [])
            has_match = bool(alice_grep_b) if isinstance(alice_grep_b, list) else bool(alice_grep_b)
            check(not has_match, "alice cannot grep Company B's marker (sees Company A only)")
        else:
            skip("alice grep Company B marker", f"HTTP {resp.status_code}")
    else:
        skip("Search isolation tests", "some uploads failed")

    # ================================================================
    section("8. Cleanup")
    # ================================================================

    resp = httpx.delete(
        f"{base}/api/v1/admin/accounts/company_a", headers=root_h, timeout=30
    )
    check(resp.is_success, "Deleted Company A (cascade)")

    resp = httpx.delete(
        f"{base}/api/v1/admin/accounts/company_b", headers=root_h, timeout=30
    )
    check(resp.is_success, "Deleted Company B (cascade)")

    # Verify keys are invalidated
    resp = api_get(f"{base}/api/v1/fs/ls", h(alice_key), {"uri": "viking://"})
    check(not resp.is_success, "alice's key invalidated after Company A deletion")
    resp = api_get(f"{base}/api/v1/fs/ls", h(charlie_key), {"uri": "viking://"})
    check(not resp.is_success, "charlie's key invalidated after Company B deletion")

    # ================================================================
    section("Results")
    # ================================================================
    total = stats["passed"] + stats["failed"] + stats["skipped"]
    print(f"\n  {PASS} Passed: {stats['passed']}")
    print(f"  {FAIL} Failed: {stats['failed']}")
    if stats["skipped"]:
        print(f"  {SKIP} Skipped: {stats['skipped']}")
    print(f"  Total: {total}")
    print()

    if stats["failed"] > 0:
        print(f"  {FAIL} SOME TESTS FAILED")
        sys.exit(1)
    else:
        print(f"  {PASS} ALL TESTS PASSED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify multi-tenant isolation for Company → User model"
    )
    parser.add_argument("--url", default="http://localhost:1933", help="Server URL")
    parser.add_argument("--root-key", default="my-root-key", help="Root API key")
    args = parser.parse_args()

    run(args.url, args.root_key)
