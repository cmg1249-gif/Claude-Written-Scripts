"""HTTPS trust for both installed Windows roots and fresh/offline Windows images."""
import ssl
import certifi


def make_context():
    context = ssl.create_default_context()
    # Preserve administrator-installed roots and supplement the Windows store.
    context.load_verify_locations(cafile=certifi.where())
    return context


HTTPS_CONTEXT = make_context()
