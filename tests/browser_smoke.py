"""Optional Playwright check inside the disposable native acceptance environment."""

import os
import subprocess
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright


def exercise(repo, cwd, env):
    p = subprocess.Popen(
        [str(repo / "threadway-cli"), "ui", "--no-browser"],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        url = p.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:")
        req = urllib.request.Request(
            url + "tasks", headers={"Host": "untrusted.example"}
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("Foreign Host accepted")
        except urllib.error.HTTPError as err:
            assert err.code == 403
        for suffix in ("not-an-endpoint", "detail/other/claude"):
            try:
                urllib.request.urlopen(url + suffix)
                raise AssertionError("Unknown path accepted")
            except urllib.error.HTTPError as err:
                assert err.code == 404
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=os.environ.get("THREADWAY_TEST_CHROMIUM") or None,
            )
            page = browser.new_page(viewport={"width": 1120, "height": 780})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(url)
            page.get_by_role("heading", name="Threadway", exact=True).wait_for()
            items = page.locator("#tasks .task")
            items.first.wait_for()
            box = page.get_by_role("searchbox")
            box.fill("no-such-fixture-xyz")
            page.get_by_text("No matching sessions.").wait_for()
            box.fill("")
            items.first.wait_for()
            # Use the deliberately diverged original (last in creation order).
            items.last.click()
            page.locator("#target").select_option("claude")
            page.get_by_text("Independent changes on both sides", exact=True).wait_for()
            page.locator("#source").select_option("codex")
            page.get_by_role("button", name="Prepare separate branch").wait_for()
            page.get_by_role("button", name="Prepare separate branch").click()
            page.get_by_text("Native session ready.").wait_for()
            assert page.get_by_role("button", name="Copy command").count() == 1
            page.locator("#mode").select_option("checkpoint")
            page.get_by_text(
                "Continue from recent context and keep older records available locally"
            ).wait_for()
            page.get_by_role("button", name="Prepare handoff").click()
            page.get_by_text("Native session ready.").wait_for()
            # A forged cross-origin POST never writes any session.
            response = page.request.post(
                url + "handoff", headers={"Origin": "http://elsewhere.invalid"}, data={}
            )
            assert response.status == 403
            page.set_viewport_size({"width": 390, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            browser.close()
        print(
            "PASS: desktop/mobile GUI, search, preview, explicit branch preparation and cross-origin/Host refusal.",
            flush=True,
        )
    finally:
        p.terminate()
        p.wait(timeout=5)
