"""Capture the rendered HTML from a user-supplied public page URL."""

from __future__ import annotations

from urllib.parse import urlparse


def capture_rendered_html(url: str, *, settle_seconds: int = 10) -> str:
    """Return browser-rendered HTML after DOM content load and a settling delay."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a complete http:// or https:// URL")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("URL capture needs Playwright. Restart the app launcher to install the updated requirements.") from exc

    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True)
                break
            except Exception as exc:
                errors.append(f"{channel or 'Playwright Chromium'}: {exc}")
        if browser is None:
            raise RuntimeError(
                "Could not start a browser for URL capture. Install Microsoft Edge or Google Chrome. "
                + " | ".join(errors[-2:])
            )

        try:
            page = browser.new_page()
            page.goto(url.strip(), wait_until="load", timeout=60_000)
            page.wait_for_timeout(max(0, settle_seconds) * 1000)
            return page.content()
        finally:
            browser.close()
