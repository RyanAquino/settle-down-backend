"""The committed OpenAPI document must match what the code serves.

``openapi.json`` is the API contract the client builds against. Committing it
makes every contract change visible in review, and CI runs ``oasdiff`` against
the base branch's copy to flag breaking changes.

After an intentional API change, regenerate it with:

    UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest backend_api/tests/test_openapi_schema.py
"""

import json
import os
from pathlib import Path

from django.conf import settings

from settledown.api import api

SNAPSHOT = Path(settings.BASE_DIR) / "openapi.json"


def _render() -> str:
    return json.dumps(api.get_openapi_schema(), indent=2, ensure_ascii=False) + "\n"


def test_openapi_snapshot_is_up_to_date():
    rendered = _render()

    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT"):
        SNAPSHOT.write_text(rendered, encoding="utf-8")

    assert SNAPSHOT.read_text(encoding="utf-8") == rendered, (
        "openapi.json is stale. Regenerate it with "
        "`UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest backend_api/tests/test_openapi_schema.py`"
        " and review the contract change."
    )
