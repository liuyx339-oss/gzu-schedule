"""Take full-page screenshot of the summer camp poster HTML at 1080px width."""
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

html_path = Path(__file__).parent / "poster_summer_camp.html"
output_path = Path(__file__).parent / "poster_summer_camp.png"

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = getattr(p, "chromium").launch(channel="msedge")
    page = browser.new_page(viewport={"width": 1080, "height": 1080})
    page.goto(html_path.as_uri(), wait_until="networkidle")
    page.wait_for_timeout(2000)

    # Get full page height, then set viewport correctly + screenshot
    height = page.evaluate("document.querySelector('.poster').offsetHeight")
    page.set_viewport_size({"width": 1080, "height": height})
    page.wait_for_timeout(500)
    page.screenshot(path=str(output_path), full_page=True)
    browser.close()

print(f"Poster saved to: {output_path}")
print(f"Size: {output_path.stat().st_size / 1024:.0f} KB")
print(f"Dimensions: 1080 x {height} px")
