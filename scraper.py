import json
import re
import time
import logging
import os
import requests

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from worker.scraper.config import PAGE_LOAD_WAIT, MAX_MORE_STORES_CLICKS, MAX_SELLERS
from worker.scraper.models import Seller

logger = logging.getLogger(__name__)

CHROME_BIN        = "/opt/google/chrome/google-chrome"
CHROMEDRIVER_BIN  = "/usr/local/bin/chromedriver"

MAX_GRID_RETRIES     = 3
GRID_RETRY_DELAY_SEC = 2

MAX_OAPV_403_RETRIES     = 3
OAPV_403_RETRY_DELAY_SEC = 2


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}


class CaptchaError(Exception):
    pass

class SessionExpiredError(CaptchaError):
    pass


class Scraper:

    def __init__(self):
        self.driver = None

    def start(self):
        self.driver = self._init_driver()

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def restart(self):
        self.stop()
        self.start()

    # ------------------public scrape methods------------------

    def wait_for_page_ready(self, timeout: int = PAGE_LOAD_WAIT):
        """Wait until captcha text, the sellers grid, or the 'More stores' button appears —
        whichever comes first. Falls back to the full timeout if none appear (e.g. genuinely
        no Shopping panel for this product), matching the old fixed-sleep behavior as a ceiling."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: (
                    "This page checks to see if it's really you" in d.page_source
                    or d.find_elements(By.CSS_SELECTOR, "[data-attrid='organic_offers_grid']")
                    or d.find_elements(By.XPATH, "//div[@role='button'][contains(., 'More stores')]")
                )
            )
        except TimeoutException:
            pass

    def scrape(self, url: str, seller_limit: int = MAX_SELLERS) -> list[Seller]:
        """Browser scrape — returns sellers only."""
        sellers, _ = self.scrape_and_capture_template(url, seller_limit)
        return sellers

    def scrape_and_capture_template(self, url: str, seller_limit: int = MAX_SELLERS) -> tuple:
        """
        Browser scrape. Returns (sellers, oapv_template).
        - If More stores button found: clicks, captures oapv_template URL, fetches sellers via CDP.
        - If no More stores: returns DOM sellers, oapv_template=None.
        - If organic_offers_grid comes back with 0 items and no More stores button, the page
          load is retried up to MAX_GRID_RETRIES times before accepting it as a genuine
          no-sellers result.
        """
        all_sellers = []
        has_more_stores = False

        for attempt in range(1, MAX_GRID_RETRIES + 1):
            self.driver.get(url)
            self.wait_for_page_ready()

            if "This page checks to see if it's really you" in self.driver.page_source:
                raise CaptchaError("CAPTCHA detected")

            if "Details aren't available for this product" in self.driver.page_source:
                logger.info("'Details aren't available for this product' in page source — genuine NO_SELLERS, not retrying")
                all_sellers, grid_count = [], 0
                has_more_stores = False
                break

            if "Buying options" not in self.driver.page_source:
                logger.info("'Buying options' not in page source — genuine NO_SELLERS, not retrying")
                all_sellers, grid_count = [], 0
                has_more_stores = False
                break

            all_sellers, grid_count = self._get_sellers_from_page()
            logger.info("Sellers from DOM: %d", len(all_sellers))

            has_more_stores = bool(self.driver.find_elements(
                By.XPATH, "//div[@role='button'][contains(., 'More stores')]"
            ))

            if grid_count > 0 or has_more_stores:
                break

            if attempt < MAX_GRID_RETRIES:
                logger.info(
                    "0 items in organic_offers_grid and no More stores button "
                    "(attempt %d/%d) — retrying page load: %s",
                    attempt, MAX_GRID_RETRIES, url,
                )
                time.sleep(GRID_RETRY_DELAY_SEC)
            else:
                logger.info(
                    "0 items in organic_offers_grid after %d attempts — accepting as genuine NO_SELLERS",
                    MAX_GRID_RETRIES,
                )

        oapv_template = None

        if has_more_stores and len(all_sellers) < seller_limit:
            logger.info("More stores button found — clicking and capturing oapv")
            seen_urls = set()
            for data, template_url in self._click_more_stores_with_url(seen_urls):
                all_sellers.extend(self._extract_sellers(data))
                if oapv_template is None:
                    oapv_template = template_url
                    logger.info("oapv_template captured")
                if len(all_sellers) >= seller_limit:
                    break
        else:
            logger.info("No More stores button — DOM sellers only")

        all_sellers = self._deduplicate(all_sellers)
        all_sellers.sort(
            key=lambda s: float(s.price) if s.price.replace(".", "", 1).isdigit() else 999999
        )
        return all_sellers[:seller_limit], oapv_template

    def scrape_via_requests(self, url: str, session, oapv_template: str,
                        seller_limit: int = MAX_SELLERS) -> list[Seller]:
        """
        Pure requests scrape — no browser needed.
        1. requests.get HTML → extract oapvfc
        - oapvfc NOT found → genuine NO_SELLERS (no Google Shopping panel)
        - oapvfc found     → call oapv API for all sellers with rating/review
        2. Empty first oapv batch → session/xsrf expired → CaptchaError
        Raises CaptchaError on 429 or expired session.
        """
        gpcid_match = re.search(r'gpcid:(\d+)', url)
        catalogid_match = re.search(r'catalogid:(\d+)', url)

        if gpcid_match:
            id_param = f"gpcid:{gpcid_match.group(1)}"
            gpcid    = gpcid_match.group(1)
        elif catalogid_match:
            id_param = f"catalogid:{catalogid_match.group(1)}"
            gpcid    = catalogid_match.group(1)
        else:
            logger.error("No gpcid or catalogid in URL: %s", url)
            return []

        # Fetch HTML to get fresh oapvfc
        page_url = f"https://www.google.com/search?ibp=oshop&q=Pivot&prds={id_param}"
        resp = session.get(page_url, headers={**HEADERS, "Accept": "text/html"}, timeout=15)
        logger.info("oapv HTML fetch status_code=%d", resp.status_code)
        if resp.status_code == 429:
            raise CaptchaError("Rate-limited on HTML fetch")
        if "This page checks to see if it's really you" in resp.text:
            raise CaptchaError("CAPTCHA detected in HTML response")
        if len(resp.text.strip()) < 100:
            raise Exception(f"Incomplete HTML response received (length={len(resp.text.strip())})")

        logger.debug(f"Fetched HTML for fetching oapvfc: {resp.text[:1000]}...")
        gpcid, oapvfc = self._extract_product_tokens(resp.text, gpcid)
        if not oapvfc:
            if "Details aren't available for this product" in resp.text:
                logger.info("Details aren't available for this product in HTML response")
                return []     # explicit no-product message
            if "Buying options" not in resp.text:
                logger.info("Buying options panel not present in HTML response")   # panel header itself never rendered — no Shopping data at all       
                return []           
            raise Exception("Buying options panel present but oapvfc extraction failed")            # panel exists, sellers likely exist, but token extraction failed — real problem

        # Always call oapv — returns all sellers with rating/review
        # regardless of whether More stores button is present
        # empty first batch = session expired (oapvfc valid means product exists)
        all_sellers = []
        sori = 0
        first_batch = True

        while len(all_sellers) < seller_limit:
            oapv_url = self._build_oapv_url(oapv_template, gpcid, oapvfc, sori)

            resp = None
            for oapv_attempt in range(1, MAX_OAPV_403_RETRIES + 1):
                resp = session.get(oapv_url, headers=HEADERS, timeout=15)
                logger.info("oapv sori=%d status_code=%d", sori, resp.status_code)
                if resp.status_code != 403:
                    break
                if oapv_attempt < MAX_OAPV_403_RETRIES:
                    logger.warning(
                        "oapv 403 at sori=%d (attempt %d/%d) — retrying",
                        sori, oapv_attempt, MAX_OAPV_403_RETRIES,
                    )
                    time.sleep(OAPV_403_RETRY_DELAY_SEC)
                else:
                    logger.warning(
                        "oapv still 403 at sori=%d after %d attempts",
                        sori, MAX_OAPV_403_RETRIES,
                    )

            if resp.status_code == 429:
                raise CaptchaError("Rate-limited on oapv fetch")
            if resp.status_code == 403:
                raise SessionExpiredError(
                    f"oapv 403 persisted after {MAX_OAPV_403_RETRIES} attempts at sori={sori}"
                )
            lines = resp.text.splitlines()
            if len(lines) <= 1:
                raise SessionExpiredError("Session expired — empty oapv response")
            data = json.loads("".join(lines[1:]))
            result = data.get("ProductDetailsResult")
            if not isinstance(result, list) or len(result) <= 81:
                raise SessionExpiredError(f"Session expired — incomplete ProductDetailsResult at sori={sori}")
            sellers = self._extract_sellers(data)
            logger.info("oapv sori=%d | %d sellers", sori, len(sellers))
            if not sellers:
                if first_batch:
                    raise SessionExpiredError("Session expired — empty sellers at sori=0")
                break  # result[81] is null — genuine end of seller list
            all_sellers.extend(sellers)
            if first_batch:
                # sori=0 always returns 3-5 sellers regardless of total count
                # next page starts at sori=1
                sori = 1
                first_batch = False
            else:
                if len(sellers) < 10:
                    break  # partial batch = last page
                sori += 10

        all_sellers = self._deduplicate(all_sellers)
        all_sellers.sort(
            key=lambda s: float(s.price) if s.price.replace(".", "", 1).isdigit() else 999999
        )
        return all_sellers[:seller_limit]

    # ------------------internal helpers------------------

    def _init_driver(self, retries=3):
        for attempt in range(1, retries + 1):
            try:
                options = uc.ChromeOptions()
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")
                options.add_argument("--window-size=1920,1080")
                options.add_argument("--disable-background-timer-throttling")
                options.add_argument("--disable-backgrounding-occluded-windows")
                options.add_argument("--disable-renderer-backgrounding")
                options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

                if os.path.exists(CHROME_BIN):
                    # EC2 — use installed Chrome + undetected_chromedriver
                    options.binary_location = CHROME_BIN
                    chrome_version = int(os.popen(f"{CHROME_BIN} --version").read().split()[2].split(".")[0])
                    driver = uc.Chrome(
                        options=options,
                        driver_executable_path=CHROMEDRIVER_BIN,
                        version_main=chrome_version,
                    )
                else:
                    # Local dev
                    driver = uc.Chrome(options=options)

                driver.execute_cdp_cmd("Network.enable", {})
                logger.info("Chrome started (attempt %d)", attempt)
                return driver
            except Exception as e:
                logger.error("Chrome init failed (attempt %d): %s", attempt, e)
                time.sleep(5)
        raise RuntimeError("Failed to start Chrome")

    def redirect_goto(self, url):
        if not url:
            return ""
        if url.startswith("https://www.google.com/goto"):
            try:
                response = requests.get(url, headers=HEADERS, allow_redirects=False)
 
                if response.status_code in [301, 302]:
                    target_url = response.headers.get("Location")
                    logger.info(f"Target URL: {target_url}")
                    url = target_url
                else:
                    response = requests.get(url, headers = HEADERS, allow_redirects=True, timeout=10)
                    logger.info("Google redirect status_code=%d url=%s", response.status_code, response.url)
                    url = response.url
            except requests.RequestException as e:
                logger.error("Failed to redirect to %s : %s", url, e)
                return url
 
        url = url.split("?",1)[0]
        logger.info("seller_url=%s", url)
        return url

    def clean_seller_name(self, seller) -> str:
        if seller is None:
            return ""
        seller = str(seller).strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÿ0-9&?._@\- ,(){}+[':;]+", seller):
            return ""
        return seller

    def _get_sellers_from_page(self) -> tuple:
        """Returns (sellers, grid_item_count)."""
        sellers = []
        grid_count = 0
        try:
            grid = self.driver.find_elements(
                By.CSS_SELECTOR, "[data-attrid='organic_offers_grid'] [role='listitem']"
            )
            grid_count = len(grid)
            logger.info("Found %d items in organic_offers_grid", grid_count)
            for item in grid:
                try:
                    name_el  = item.find_elements(By.CSS_SELECTOR, ".gUf0b")
                    seller_name     = name_el[0].text.strip().replace("\t", " ").replace("|", " ") if name_el else ""
                    name = self.clean_seller_name(seller_name)
                    link_el  = item.find_elements(By.CSS_SELECTOR, "a.P9159d")
                    prod_url = link_el[0].get_attribute("href") if link_el else ""
                    url = self.redirect_goto(prod_url)    # redirect to seller page

                    price_el = item.find_elements(By.CSS_SELECTOR, ".JIep9e span, .Pgbknd span")
                    price    = self._clean_price(
                        price_el[0].get_attribute("aria-label") or price_el[0].text if price_el else ""
                    )
                    ship_el  = item.find_elements(By.CSS_SELECTOR,
                                                  "[aria-label*='delivery'], [aria-label*='Delivery']")
                    shipping = self._clean_shipping(
                        ship_el[0].get_attribute("aria-label") or "" if ship_el else ""
                    )
                    stock_el = item.find_elements(By.CSS_SELECTOR, ".jvP2Jb span")
                    stock    = stock_el[0].text.strip() if stock_el else ""
                    rating_el = item.find_elements(By.CSS_SELECTOR, ".NFq8Ad")
                    rating   = rating_el[0].text.strip().replace("/5", "") if rating_el else ""
                    cond_el  = item.find_elements(By.CSS_SELECTOR, ".b7GjXe span, .yd3Rwd")
                    condition = cond_el[0].text.strip() if cond_el else "New"

                    if not name or price in ("na", "", "0.00"):
                        continue
                    sellers.append(Seller(name=name, price=price, shipping=shipping,
                                          prod_url=url, stock=stock, rating=rating, condition=condition))
                except Exception:
                    pass
        except Exception as e:
            logger.debug("DOM extraction error: %s", e)
        return sellers, grid_count

    def _click_more_stores_with_url(self, seen_urls: set):
        """Click More stores repeatedly, yield (data, oapv_url) for each new oapv response."""
        more_stores_locator = (By.XPATH, "//div[@role='button'][contains(., 'More stores')]")
        for i in range(MAX_MORE_STORES_CLICKS):
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable(more_stores_locator)
                )
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(1)
                self.driver.execute_script("arguments[0].click();", btn)
                logger.info("Clicked 'More stores' #%d", i + 1)
                button_still_present = True
                try:
                    WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable(more_stores_locator)
                    )
                except TimeoutException:
                    button_still_present = False  # no more stores — avoid re-waiting next loop
                yield from self._get_oapv_data_with_url(seen_urls)
                if not button_still_present:
                    break
            except TimeoutException:
                break
            except Exception as e:
                logger.error("Error clicking More stores: %s", e)
                break

    def _get_oapv_data_with_url(self, seen_urls: set):
        """Yield (data, url) for each new oapv response in network logs."""
        for log in self.driver.get_log("performance"):
            try:
                msg = json.loads(log["message"])["message"]
                if msg["method"] != "Network.responseReceived":
                    continue
                url = msg["params"]["response"]["url"]
                if "async/oapv" not in url or url in seen_urls:
                    continue
                seen_urls.add(url)
                body = self.driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": msg["params"]["requestId"]}
                )
                lines = body["body"].splitlines()
                if len(lines) <= 1:
                    continue
                content = "".join(lines[1:]).strip()
                if not content:
                    continue
                yield json.loads(content), url
            except Exception as e:
                if "No resource with given identifier" not in str(e):
                    logger.error("Failed to get oapv body: %s", e)

    @staticmethod
    def _extract_product_tokens(html: str, gpcid: str) -> tuple:
        """Extract (gpcid, oapvfc) from HTML. Falls back to first panel if gpcid is stale."""
        m = re.search(rf'data-gpcid="({gpcid})"[^>]*data-oapvfc="([^"]+)"', html)
        if m:
            return m.group(1), m.group(2)
        m = re.search(rf'data-oapvfc="([^"]+)"[^>]*data-gpcid="({gpcid})"', html)
        if m:
            return m.group(2), m.group(1)
        m = re.search(r'data-gpcid="(\d+)"[^>]*data-oapvfc="([^"]+)"', html)
        if m:
            logger.info("gpcid fallback: %s → %s", gpcid, m.group(1))
            return m.group(1), m.group(2)
        m = re.search(r'data-oapvfc="([^"]+)"[^>]*data-gpcid="(\d+)"', html)
        if m:
            logger.info("gpcid fallback: %s → %s", gpcid, m.group(2))
            return m.group(2), m.group(1)
        return gpcid, None

    @staticmethod
    def _build_oapv_url(template_url: str, gpcid: str, oapvfc: str, sori: int) -> str:
        url = template_url
        url = re.sub(r'(gpcid:)\d+',      f'\\g<1>{gpcid}',  url)
        url = re.sub(r'(oapvfc:)[^,]+',   f'\\g<1>{oapvfc}', url)
        if re.search(r'sori:\d+', url):
            url = re.sub(r'(sori:)\d+', f'\\g<1>{sori}', url)
        else:
            url = url.replace(',mno:', f',sori:{sori},mno:')
        return url

    def _extract_sellers(self, data: dict) -> list[Seller]:
        sellers = []
        try:
            result = data.get("ProductDetailsResult", [])
            if not isinstance(result, list) or len(result) <= 81:
                return sellers
            for seller in result[81][0][0]:
                try:
                    seller_name     = seller[1][0].strip().replace("\t", " ").replace("|", " ")
                    name = self.clean_seller_name(seller_name)
                    url = seller[2][0]
                    prod_url = self.redirect_goto(url)  # redirect to seller page
                    price    = self._parse_price(seller[48])
                    shipping = self._parse_shipping(seller[48])
                    if not name or price in ("na", "", "0.00"):
                        continue
                    rating = ""
                    reviews = ""
                    try:
                        rating  = seller[24][0][0][0].replace("/5", "")
                        reviews = str(seller[24][0][0][1])
                    except Exception:
                        pass
                    stock = ""
                    try:
                        raw_stock = seller[51][4][0][0][0]   
                        if raw_stock == "In stock":
                            stock = "In stock online"
                        else:
                            stock = raw_stock 
                    except Exception:
                        pass
                    condition = "New"
                    try:
                        condition = seller[51][1][0][0][0]
                    except Exception:
                        pass
                    sellers.append(Seller(name=name, price=price, shipping=shipping,
                                          prod_url=prod_url, stock=stock,
                                          rating=rating, reviews=reviews, condition=condition))
                except Exception:
                    pass
        except Exception:
            pass
        return sellers

    @staticmethod
    def _parse_price(block) -> str:
        try:
            return "".join(c for c in block[0] if c not in "$,!").strip()
        except Exception:
            return "na"

    @staticmethod
    def _parse_shipping(block) -> str:
        try:
            raw = block[6].replace("\u00a0", " ").replace("\u00c2", "")
            val = "".join(c for c in raw if c not in "$,!+ ").strip()
            return "0.00" if val.upper() == "FREE" else val
        except Exception:
            return "0.00"

    @staticmethod
    def _clean_price(text: str) -> str:
        try:
            match = re.search(r'[\d,]+\.\d{2}', text.replace(",", ""))
            return match.group(0) if match else "na"
        except Exception:
            return "na"

    @staticmethod
    def _clean_shipping(text: str) -> str:
        try:
            if "free" in text.lower():
                return "0.00"
            match = re.search(r'\$([\d,]+\.\d{2})', text)
            return match.group(1).replace(",", "") if match else "0.00"
        except Exception:
            return "0.00"

    @staticmethod
    def _deduplicate(sellers: list[Seller]) -> list[Seller]:
        seen = {}
        for s in sellers:
            key = (s.name.lower().strip(), s.prod_url.strip())
            if key not in seen:
                seen[key] = s
        return list(seen.values())