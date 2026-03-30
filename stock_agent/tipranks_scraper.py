"""TipRanks automated scraper — logs in, runs stock screener, exports CSV."""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


async def scrape_tipranks(config=None):
    """Scrape TipRanks stock screener and download CSV.

    Args:
        config: Optional Config object. If None, uses defaults from env vars.
    """
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
        try:
            logger.info("Launching browser")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            context.set_default_timeout(60_000)
            page = await context.new_page()

            # ── Step 1: Handle cookie consent if it appears ──
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

            # ── Step 2: Log in ──
            logger.info("Navigating to TipRanks sign-in page")
            await page.goto("https://www.tipranks.com/sign-in", wait_until="domcontentloaded")
            await _dismiss_cookies()

            # Wait for login form
            logger.info("Filling login form")
            email_input = page.locator('input[name="email"], input[type="email"]').first
            await email_input.wait_for(state="visible", timeout=30_000)
            await email_input.fill(email)

            password_input = page.locator('input[name="password"], input[type="password"]').first
            await password_input.wait_for(state="visible", timeout=10_000)
            await password_input.fill(password)

            # Click sign-in button
            submit_btn = page.locator(
                'button[type="submit"], button:has-text("Sign In"), '
                'button:has-text("Log In"), button:has-text("LOGIN")'
            ).first
            await submit_btn.click()

            # Wait for navigation away from sign-in page
            logger.info("Waiting for login to complete")
            try:
                await page.wait_for_url(
                    lambda url: "sign-in" not in url,
                    timeout=30_000,
                )
                logger.info("Login successful")
            except Exception:
                # Check if we're still on sign-in — could be an error
                if "sign-in" in page.url:
                    await page.screenshot(path=str(data_dir / "tipranks_debug.png"))
                    logger.error("Login failed — screenshot saved to tipranks_debug.png")
                    return

            await _dismiss_cookies()

            # ── Step 3: Navigate to screener with filters ──
            screener_url = (
                "https://www.tipranks.com/screener/stocks"
                "?smartScore=8,9,10"
                "&analystConsensus=strongBuy,moderateBuy"
                "&marketCap=mega,large"
            )
            logger.info("Navigating to stock screener with filters")
            await page.goto(screener_url, wait_until="domcontentloaded")
            await _dismiss_cookies()

            # Wait for results table to appear
            logger.info("Waiting for screener results to load")
            try:
                await page.wait_for_selector(
                    "table, [class*='screener'], [data-testid*='screener']",
                    timeout=30_000,
                )
            except Exception:
                # Results may load differently — wait a bit and check
                await page.wait_for_timeout(5000)

            # Wait extra for data to fully populate
            await page.wait_for_timeout(3000)

            # Try to apply Hedge Fund Signal filter via UI if not available via URL
            try:
                hf_filter = page.locator(
                    "text=Hedge Fund, text=HF Signal, [data-testid*='hedge']"
                ).first
                if await hf_filter.is_visible(timeout=3000):
                    await hf_filter.click()
                    positive_opt = page.locator("text=Positive").first
                    if await positive_opt.is_visible(timeout=3000):
                        await positive_opt.click()
                        logger.info("Applied Hedge Fund Signal: Positive filter")
                        await page.wait_for_timeout(2000)
            except Exception:
                logger.info("Hedge Fund Signal filter not available via UI — skipping")

            # ── Step 4: Download CSV ──
            logger.info("Looking for CSV export button")
            export_btn = page.locator(
                'button:has-text("Export"), button:has-text("Download"), '
                'button:has-text("CSV"), a:has-text("Export"), '
                '[data-testid*="export"], [aria-label*="export"], '
                '[aria-label*="download"]'
            ).first

            try:
                await export_btn.wait_for(state="visible", timeout=15_000)
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
