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
            first = page.locator("article").first
            first.get_by_role("button").first.wait_for()
            box = page.get_by_role("searchbox")
            box.fill("No such fixture session")
            page.get_by_text("No matching sessions.").wait_for()
            box.fill("")
            panels = page.locator("article")
            found = False
            for i in range(panels.count()):
                panel = panels.nth(i)
                panel.get_by_role("button", name="View handoff to claude").click()
                panel.locator("p").first.wait_for()
                if panel.locator("p").first.inner_text() == "diverged":
                    assert (
                        "--branch" in panel.inner_text()
                        and "new messages" in panel.inner_text()
                    )
                    found = True
                    break
            assert found and not errors, errors
            page.set_viewport_size({"width": 390, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            browser.close()
        print(
            "PASS: desktop/mobile project search, per-task branch details, read-only URL and Host isolation.",
            flush=True,
        )
    finally:
        p.terminate()
        p.wait(timeout=5)
