"""Authenticate reconnect addresses before sending a viewer's credentials."""
import hashlib
import hmac
import json


def discovery_key(password, session):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), ('screen-discovery:' + session).encode(), 100_000)


def signature(payload, key):
    signed = {name: payload.get(name) for name in ('app', 'url', 'session', 'paired', 'created')}
    message = json.dumps(signed, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()
