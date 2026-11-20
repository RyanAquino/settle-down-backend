"""Run the local ASGI app with a dedicated test Redis and synthetic upstreams."""

import asyncio
import os


def main():
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    os.environ["REDIS_URL"] = os.environ["TEST_REDIS_URL"]
    os.environ["DAILY_SCAN_LIMIT"] = "2"
    os.environ["SCAN_LIMIT_EXEMPT_EMAILS"] = ""

    from settledown.asgi import application
    from backend_api.tests.test_personal_sessions import provider_patches

    config = Config()
    config.bind = ["127.0.0.1:18765"]
    config.accesslog = "-"
    auth, ocr, images = provider_patches()
    with auth, ocr, images:
        asyncio.run(serve(application, config))


if __name__ == "__main__":
    main()
