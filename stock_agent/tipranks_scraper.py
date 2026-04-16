"""TipRanks automated scraper — logs in, runs stock screener, exports CSV.

Uses stealth techniques to avoid bot detection:
- Realistic viewport, locale, and timezone
- Human-like typing delays and mouse movements
- Submits form via Enter key (not button click)
- Random wait times between actions
"""

import asyncio
import logging
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


async def _human_type(page, selector: str, text: str):
    """Type text with human-like delays between keystrokes."""
    el = page.locator(selector).first
    await el.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    # Clear existing text
    await el.fill("")
    await asyncio.sleep(random.uniform(0.1, 0.3))
    # Type character by character
    for char in text:
        await el.type(char, delay=random.randint(30, 120))
    await asyncio.sleep(random.uniform(0.3, 0.7))


async def scrape_tipranks(config=None):
    """Scrape TipRanks stock screener and download CSV."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        )
        return

    # Resolve config values
    if config:
        data_dir = Path(config.DATA_DIR)
        email = config.TIPRANKS_EMAIL
        password = config.TIPRANKS_PASSWORD
    else:
        data_dir = Path(os.environ.get("STOCK_DATA_DIR", "data/stock_agent"))
        email = os.environ.get("TIPRANKS_EMAIL", "")
        password = os.environ.get("TIPRANKS_PASSWORD", "")

    if not email or not password:
        logger.error("TIPRANKS_EMAIL and TIPRANKS_PASSWORD must be set")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    download_dir = data_dir / "_tipranks_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_latest = data_dir / "tipranks_latest.csv"
    dest_dated = data_dir / f"tipranks_{today}.csv"

    async with async_playwright() as p:
        browser = None
        page = None
        try:
            logger.info("Launching browser")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )

            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1366, "height": 768},
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            context.set_default_timeout(60_000)

            # Stealth: remove webdriver flag
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
            """)

            page = await context.new_page()

            # ── Step 1: Handle cookie consent ──
            async def _dismiss_cookies():
                try:
                    btn = page.locator(
                        "button:has-text('Accept'), button:has-text('Got it'), "
                        "button:has-text('I agree'), button:has-text('OK')"
                    ).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        logger.info("Dismissed cookie consent popup")
                except Exception:
                    pass

            # ── Step 2: Log in with human-like behavior ──
            logger.info("Navigating to TipRanks sign-in page")
            await page.goto(
                "https://www.tipranks.com/sign-in",
                wait_until="networkidle",
                timeout=45_000,
            )
            await asyncio.sleep(random.uniform(1.5, 3.0))
            await _dismiss_cookies()
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Wait for login form
            logger.info("Filling login form with human-like typing")
            email_input = 'input[name="email"], input[type="email"]'
            password_input = 'input[name="password"], input[type="password"]'

            await page.wait_for_selector(email_input, state="visible", timeout=30_000)
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Type email with human-like delays
            await _human_type(page, email_input, email)
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Type password
            await page.wait_for_selector(password_input, state="visible", timeout=10_000)
            await _human_type(page, password_input, password)
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Check terms checkbox if visible and not already checked
            try:
                terms_checkbox = page.locator(
                    'input[type="checkbox"]'
                ).first
                if await terms_checkbox.is_visible(timeout=3000):
                    is_checked = await terms_checkbox.is_checked()
                    if not is_checked:
                        await terms_checkbox.check()
                        logger.info("Checked terms checkbox")
                        await asyncio.sleep(random.uniform(0.3, 0.7))
            except Exception:
                pass

            # Submit via Enter key (more human-like than button click)
            logger.info("Submitting login form")
            await page.keyboard.press("Enter")

            # Wait for navigation away from sign-in page
            logger.info("Waiting for login to complete")
            try:
                await page.wait_for_url(
                    lambda url: "sign-in" not in url,
                    timeout=45_000,
                )
                logger.info("Login successful — redirected to %s", page.url)
            except Exception:
                # Try clicking the Sign In button as fallback
                logger.info("Enter key did not submit — trying button click")
                try:
                    submit_btn = page.locator(
                        'button:has-text("Sign In")'
                    ).first
                    await submit_btn.click()
                    await page.wait_for_url(
                        lambda url: "sign-in" not in url,
                        timeout=30_000,
                    )
                    logger.info("Login successful via button click")
                except Exception:
                    # Check for error messages
                    error_msg = ""
                    try:
                        err = page.locator(
                            '[class*="error"], [class*="alert"], '
                            'text=incorrect, text=invalid, text=wrong'
                        ).first
                        if await err.is_visible(timeout=3000):
                            error_msg = await err.text_content()
                    except Exception:
                        pass

                    await page.screenshot(path=str(data_dir / "tipranks_debug.png"))
                    logger.error(
                        "Login failed — error: %s — screenshot saved to tipranks_debug.png",
                        error_msg or "unknown (still on sign-in page)",
                    )
                    return

            await asyncio.sleep(random.uniform(2.0, 3.0))
            await _dismiss_cookies()

            # ── Step 3: Navigate to screener with filters ──
            screener_url = (
                "https://www.tipranks.com/screener/stocks"
                "?smartScore=8,9,10"
                "&analystConsensus=strongBuy,moderateBuy"
                "&marketCap=mega,large"
            )
            logger.info("Navigating to stock screener with filters")
            await page.goto(screener_url, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(random.uniform(2.0, 4.0))
            await _dismiss_cookies()

            # Wait for results table to appear
            logger.info("Waiting for screener results to load")
            try:
                await page.wait_for_selector(
                    "table, [class*='screener'], [data-testid*='screener'], "
                    "[class*='TableRow'], [class*='tableRow']",
                    timeout=30_000,
                )
            except Exception:
                await page.wait_for_timeout(5000)

            # Wait extra for data to fully populate
            await page.wait_for_timeout(4000)

            # ── Step 4: Download CSV ──
            logger.info("Looking for CSV export button")
            export_btn = page.locator(
                'button:has-text("Export"), button:has-text("Download"), '
                'button:has-text("CSV"), a:has-text("Export"), '
                '[data-testid*="export"], [aria-label*="export"], '
                '[aria-label*="download"], button:has-text("export")'
            ).first

            try:
                await export_btn.wait_for(state="visible", timeout=15_000)
            except Exception:
                # Try scrolling down to find it
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5)
                try:
                    await export_btn.wait_for(state="visible", timeout=10_000)
                except Exception:
                    await page.screenshot(path=str(data_dir / "tipranks_debug.png"))
                    logger.error("Export button not found — screenshot saved")
                    return

            # Start waiting for download before clicking
            async with page.expect_download(timeout=30_000) as download_info:
                await export_btn.click()
                logger.info("Clicked export button, waiting for download")

            download = await download_info.value
            download_path = download_dir / download.suggested_filename
            await download.save_as(str(download_path))
            logger.info("Downloaded CSV: %s", download_path.name)

            # ── Step 5: Move CSV to final locations ──
            shutil.copy2(str(download_path), str(dest_latest))
            shutil.copy2(str(download_path), str(dest_dated))
            logger.info("Saved CSV to %s and %s", dest_latest.name, dest_dated.name)

            # Clean up download dir
            try:
                download_path.unlink()
            except Exception:
                pass

        except Exception as e:
            logger.exception("TipRanks scraper failed: %s", e)
            try:
                if page:
                    await page.screenshot(path=str(data_dir / "tipranks_debug.png"))
                    logger.info("Debug screenshot saved to tipranks_debug.png")
            except Exception:
                pass
            raise

        finally:
            if browser:
                await browser.close()
                logger.info("Browser closed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(scrape_tipranks())
