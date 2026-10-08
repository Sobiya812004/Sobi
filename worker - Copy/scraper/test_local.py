"""
Local test — single browser scrape without SQS.
Usage: python -m worker.test_local [URL]

Runs the same browser scrape that the Selenium fallback uses (need_session=True) and prints
which fallback case the URL would fall into:
  Case A  sellers + oapv template  -> a session would be collected
  Case B  sellers, no template     -> sellers only, next input would be tried in the same Chrome
  Case C  no sellers               -> the fallback would retry in a new Chrome, then FINAL NO_SELLERS
"""
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from worker.scraper.scraper import Scraper

DEFAULT_URL = (
    "https://www.google.com/search?ibp=oshop&q=Xerox+113R00670+Drum+Cartridge"
    "&prds=gpcid:8211515603607648619,pvo:25,pvt:hg&udm=28&hl=en&gl=us"
    "&shem=pvflt,rimspwouoe"
)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    scraper = Scraper()
    scraper.start()

    try:
        result = scraper.scrape_browser(url, need_session=True)
        sellers = result.sellers

        if sellers and result.oapv_template:
            case = "A (sellers + oapv template -> session can be collected)"
        elif sellers:
            case = "B (sellers, no session -> try next input in the same Chrome)"
        else:
            case = f"C (no sellers, reason={result.no_sellers_reason or 'unknown'})"

        print(f"\n{'='*60}")
        print(f"Fallback case: {case}")
        print(f"More stores button: {result.has_more_stores}")
        print(f"oapv template captured: {bool(result.oapv_template)}")
        print(f"Found {len(sellers)} sellers:")
        print(f"{'='*60}")
        for s in sellers:
            print(f"  {s.name:30s} ${s.price:>8s}  ship: ${s.shipping}")
            print(f"    {s.prod_url}")
    finally:
        scraper.stop()


if __name__ == "__main__":
    main()
