"""The server entrypoints import cleanly and expose a callable application.

Docker runs ``hypercorn settledown.asgi:application``; a broken import there
would only surface at container start without this check.
"""

import importlib

import pytest


@pytest.mark.parametrize("module", ["settledown.asgi", "settledown.wsgi"])
def test_entrypoint_exposes_application(module):
    application = importlib.import_module(module).application

    assert callable(application)
