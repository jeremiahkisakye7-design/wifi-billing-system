import logging
import time

from app import SESSION_REAPER_INTERVAL_SECONDS, reap_expired_vouchers


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wifi-voucher-reaper")


def run_forever():
    while True:
        try:
            result = reap_expired_vouchers()
            if result["expiredVouchers"] or result["disconnectedDevices"]:
                logger.info("Expiry sweep: %s", result)
        except Exception:
            logger.exception("Voucher expiry sweep failed")
        time.sleep(SESSION_REAPER_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()