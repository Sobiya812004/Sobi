"""
Local test — single scrape without SQS.
Usage: python -m worker.test_local [URL]
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
        sellers = scraper.scrape(url)
        print(f"\n{'='*60}")
        print(f"Found {len(sellers)} sellers:")
        print(f"{'='*60}")
        for s in sellers:
            print(f"  {s.name:30s} ${s.price:>8s}  ship: ${s.shipping}")
            print(f"    {s.prod_url}")
    finally:
        scraper.stop()


if __name__ == "__main__":
    main()
