"""Convert poster HTML to PDF using Playwright (no server needed)."""
import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

html_path = Path(__file__).parent / "poster_summer_camp.html"
pdf_path  = Path(__file__).parent / "poster_summer_camp.pdf"

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = getattr(p, "chromium").launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1080, "height": 1080})
    page.goto(html_path.as_uri(), wait_until="networkidle")
    page.wait_for_timeout(1500)

    height = page.evaluate("document.querySelector('.poster').offsetHeight")
    page.set_viewport_size({"width": 1080, "height": height})
    page.wait_for_timeout(300)

    page.pdf(
        path=str(pdf_path),
        width="1080px",
        height=f"{height}px",
        print_background=True,
    )
    browser.close()

print(f"PDF saved: {pdf_path}")
print(f"Size: {pdf_path.stat().st_size / 1024:.0f} KB")
