"""Capture the rendered HTML from a user-supplied public page URL."""

from __future__ import annotations

from urllib.parse import urlparse


async def capture_rendered_html(url: str, *, settle_seconds: int = 10, timeout_seconds: int = 60) -> str:
    """Return browser-rendered HTML after DOM content load and a settling delay."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a complete http:// or https:// URL")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("URL capture needs Playwright. Restart the app launcher to install the updated requirements.") from exc

    errors: list[str] = []
    async with async_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = await playwright.chromium.launch(headless=True, channel=channel)
                break
            except Exception as exc:
                errors.append(f"{channel or 'Playwright Chromium'}: {exc}")
        if browser is None:
            raise RuntimeError(
                "Could not start a browser for URL capture. Install Microsoft Edge or Google Chrome. "
                + " | ".join(errors[-2:])
            )

        try:
            page = await browser.new_page()
            await page.goto(url.strip(), wait_until="load", timeout=max(1, timeout_seconds) * 1000)
            await page.wait_for_timeout(max(0, settle_seconds) * 1000)
            return await page.content()
        finally:
            await browser.close()
