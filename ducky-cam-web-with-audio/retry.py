"""Respect service backoff without disabling cancellation or TLS validation."""
import math
import time
from email.utils import parsedate_to_datetime


def retry_delay(error, default=5):
    if getattr(error, 'code', None) not in (429, 503):
        return default
    value = (getattr(error, 'headers', None) or {}).get('Retry-After')
    try:
        delay = int(value)
    except (TypeError, ValueError):
        try:
            delay = math.ceil(parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, AttributeError, OverflowError):
            delay = 60
    return max(60, delay)
