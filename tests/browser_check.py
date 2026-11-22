"""Run with Playwright and a system Chrome/Chromium. Opens the actual offline file."""
import os
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_BIN"),
                                         args=["--disable-dev-shm-usage"])
    page = browser.new_page()
    page.context.set_offline(True)
    page.goto(Path(sys.argv[1]).resolve().as_uri())
    page.get_by_role("link",name="mixed.old-read · 42703").click()
    expect(page.locator("details:target pre")).to_contain_text('"sqlstate": "42703"')
    page.get_by_role("link",name="old.write-row-2 · request").click()
    page.locator("details:target").get_by_role("link",name="Observed wait for backend",exact=False).click()
    expect(page.locator("details:target pre")).to_contain_text('"blockers":')
    browser.close()
