"""Serialize scans per account and commit the receipt and its charge together."""

import json
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from zoneinfo import ZoneInfo

from django.conf import settings
from django_redis import get_redis_connection
from ninja.errors import HttpError
from redis.exceptions import LockNotOwnedError, WatchError

JAPAN = ZoneInfo("Asia/Tokyo")


def window(account):
    today = datetime.now(JAPAN).date()
    reset = datetime.combine(today + timedelta(days=1), time.min, JAPAN)
    return f"scan:used:{account['uid']}:{today}", reset


def exempt(account):
    return (
        account["verified"]
        and account["email"].casefold() in settings.SCAN_LIMIT_EXEMPT_EMAILS
    )


def allowance(account):
    key, reset = window(account)
    used = int(get_redis_connection().get(key) or 0)
    return {
        "email": account["email"],
        "remaining": (
            None if exempt(account) else max(0, settings.DAILY_SCAN_LIMIT - used)
        ),
        "resets_at": reset.isoformat(),
    }


def reconciles(receipt):
    """Use the mobile review screen's printed-total and rounding rules."""
    costs = [Decimal(str(item.cost)).normalize() for item in receipt.receipt_items]
    total = Decimal(str(receipt.total_amount)).normalize()
    subtotal = sum(costs)
    if (
        subtotal <= 0
        or total <= 0
        or receipt.tax_percentage < 0
        or any(cost < 0 for cost in costs)
    ):
        return False
    if len({item.item_order for item in receipt.receipt_items}) != len(costs):
        return False
    if subtotal == total:
        return True
    digits = max(0, *(-value.as_tuple().exponent for value in [total, *costs]))
    unit = Decimal(1).scaleb(-max(2, digits)) if digits else Decimal(1)
    tax = subtotal * Decimal(str(receipt.tax_percentage)) / 100
    return total - subtotal in {
        tax.quantize(unit, rounding=mode) for mode in (ROUND_CEILING, ROUND_FLOOR)
    }


class Scan:
    def __init__(self, account, scan_id, digest):
        self.account = account
        self.digest = digest
        self.redis = get_redis_connection()
        self.key = f"scan:result:{account['uid']}:{scan_id}"
        self.lock = self.redis.lock(
            f"scan:lock:{account['uid']}", timeout=300, thread_local=False
        )

    def begin(self):
        if not self.lock.acquire(blocking=False):
            raise HttpError(409, "Another scan is processing. Try again shortly.")
        try:
            saved = self.redis.get(self.key)
            if saved:
                data = json.loads(saved)
                if data["digest"] != self.digest:
                    raise HttpError(400, "This scan ID belongs to another image.")
                return data["receipt"]
            if allowance(self.account)["remaining"] == 0:
                raise HttpError(429, "Next scan at midnight JST.")
        except BaseException:
            self.close()
            raise

    def finish(self, receipt):
        key, reset = window(self.account)
        with self.redis.pipeline() as pipe:
            try:
                pipe.watch(self.lock.name)
                if pipe.get(self.lock.name) != self.lock.local.token:
                    raise HttpError(409, "Scan timed out. Please retry.")
                pipe.multi()
                pipe.set(
                    self.key,
                    json.dumps({"digest": self.digest, "receipt": receipt}),
                    ex=2 * 86400,
                )
                if not exempt(self.account):
                    pipe.incr(key)
                    pipe.expireat(key, int(reset.timestamp()))
                pipe.execute()
            except WatchError:
                raise HttpError(409, "Scan timed out. Please retry.") from None

    def close(self):
        try:
            self.lock.release()
        except LockNotOwnedError:
            pass
