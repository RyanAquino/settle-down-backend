"""The receipts routes over HTTP: URL routing, middleware, auth and each view.

Requests go through Django's test ``Client``, so they traverse the real URL
conf, middleware stack and ninja's auth exactly as a deployed request does.
Only the outbound boundaries are mocked: the OCR agent and the image hosts,
patched per test.
"""

import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from pydantic_ai import BinaryContent, UnexpectedModelBehavior

from receipts.allocation import allocate_scan
from receipts.tests import client_gate, scans
from receipts.transcription import ReceiptScan

TOKEN = "test-app-token"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

RECEIPT_URL = "/api/v1/receipts/receipt-items/"


@pytest.fixture(autouse=True)
def _app_token():
    with override_settings(APP_AUTH=TOKEN):
        yield


# A plain receipt as the OCR model transcribes it: one line, 10% tax added on
# top. Its response is the same as before discounts were allocated.
RECEIPT = ReceiptScan.model_validate(
    {
        "jp_shop_name": "サンプルマート",
        "en_shop_name": "Sample Mart",
        # The model reports the receipt's JST wall time, without an offset.
        "receipt_date": "2026-05-30T12:34:56",
        "decimal_places": 0,
        "items": [
            {
                "item_order": 1,
                "japanese_name": "おにぎり",
                "english_name": "Onigiri",
                "quantity": 2,
                "printed_price": 300,
            }
        ],
        "adjustments": [],
        "tax_lines": [{"rate": 10, "taxable_amount": None, "tax_amount": 30}],
        "amount_due": 330,
    }
)


LEGACY_RECEIPT_FIELDS = [
    "en_shop_name",
    "jp_shop_name",
    "tax_percentage",
    "total_amount",
    "receipt_date",
    "receipt_image_url",
]


@pytest.fixture
def ocr_agent():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output=RECEIPT))
    with patch("receipts.ocr.get_openrouter_receipt_agent", return_value=agent):
        yield agent


def _upload():
    return SimpleUploadedFile("receipt.jpg", b"jpeg-bytes", content_type="image/jpeg")


class TestReceiptEndpoint:
    def test_response_body_is_the_scan_contract(self, client, ocr_agent):
        """Every field the installed app parses is unchanged; the rest is additive."""
        with patch(
            "receipts.image_hosting.cloudinary_upload_file",
            return_value="https://res.cloudinary.com/r.jpg",
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        body = response.json()
        # The body as it was before discounts were allocated, field for field.
        legacy_item_fields = [
            "english_name",
            "japanese_name",
            "item_order",
            "cost",
            "quantity",
            "discount",
        ]
        assert {
            **{key: body[key] for key in LEGACY_RECEIPT_FIELDS},
            "receipt_items": [
                {key: item[key] for key in legacy_item_fields}
                for item in body["receipt_items"]
            ],
        } == {
            "receipt_items": [
                {
                    "english_name": "Onigiri",
                    "japanese_name": "おにぎり",
                    "item_order": 1,
                    "cost": 300.0,
                    "quantity": 2,
                    "discount": 0.0,
                }
            ],
            "en_shop_name": "Sample Mart",
            "jp_shop_name": "サンプルマート",
            "tax_percentage": 10.0,
            "total_amount": 330.0,
            "receipt_date": "2026-05-30T12:34:56",
            "receipt_image_url": "https://res.cloudinary.com/r.jpg",
        }
        assert body["receipt_items"][0]["list_price"] == 300.0
        assert {
            key: body[key]
            for key in body
            if key not in LEGACY_RECEIPT_FIELDS and key != "receipt_items"
        } == {
            "decimal_places": 0,
            "adjustments": [],
            "tax_lines": [{"rate": 10.0, "taxable_amount": None, "tax_amount": 30.0}],
            "reconciliation": {"status": "reconciled", "difference": 0.0},
        }

    def test_extracts_receipt_and_hosts_image_on_cloudinary(self, client, ocr_agent):
        with (
            patch(
                "receipts.image_hosting.cloudinary_upload_file",
                return_value="https://res.cloudinary.com/r.jpg",
            ),
            patch("receipts.image_hosting.catbox_upload_file") as catbox,
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        data = response.json()
        assert data["receipt_image_url"] == "https://res.cloudinary.com/r.jpg"
        assert data["en_shop_name"] == "Sample Mart"
        assert data["total_amount"] == 330
        assert [item["english_name"] for item in data["receipt_items"]] == ["Onigiri"]
        catbox.assert_not_called()

        prompt = ocr_agent.run.await_args.args[0]
        images = [part for part in prompt if isinstance(part, BinaryContent)]
        assert [image.data for image in images] == [b"jpeg-bytes"]

    def test_tells_the_model_the_real_image_type(self, client, ocr_agent):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        upload = SimpleUploadedFile(
            "receipt.png", png, content_type="application/octet-stream"
        )
        with patch(
            "receipts.image_hosting.cloudinary_upload_file",
            return_value="https://res.cloudinary.com/r.png",
        ):
            response = client.post(RECEIPT_URL, {"file": upload}, **AUTH)

        assert response.status_code == 200
        prompt = ocr_agent.run.await_args.args[0]
        (image,) = [part for part in prompt if isinstance(part, BinaryContent)]
        assert image.media_type == "image/png"
        assert image.data == png

    def test_falls_back_to_catbox_when_cloudinary_fails(
        self, client, ocr_agent, caplog
    ):
        with (
            patch(
                "receipts.image_hosting.cloudinary_upload_file",
                side_effect=RuntimeError("cloudinary down"),
            ),
            patch(
                "receipts.image_hosting.catbox_upload_file",
                return_value="https://files.catbox.moe/r.jpg",
            ),
            caplog.at_level(logging.WARNING, logger="receipts.image_hosting"),
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        assert response.json()["receipt_image_url"] == "https://files.catbox.moe/r.jpg"
        # The Cloudinary failure is logged with its traceback, not swallowed.
        (record,) = caplog.records
        assert "falling back to catbox" in record.getMessage()
        assert record.exc_info[1].args == ("cloudinary down",)

    def test_the_upload_runs_off_the_event_loop(self, client, ocr_agent):
        threads = {}

        async def read_on_the_loop(prompt, deps):
            threads["read"] = threading.get_ident()
            return SimpleNamespace(output=RECEIPT)

        def upload_in_a_worker(file_obj):
            threads["upload"] = threading.get_ident()
            return "https://res.cloudinary.com/r.jpg"

        ocr_agent.run.side_effect = read_on_the_loop
        with (
            patch(
                "receipts.image_hosting.cloudinary_upload_file",
                side_effect=upload_in_a_worker,
            ),
            patch("receipts.image_hosting.catbox_upload_file") as catbox,
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        assert (
            response.json()["receipt_image_url"] == "https://res.cloudinary.com/r.jpg"
        )
        catbox.assert_not_called()
        assert threads["upload"] != threads["read"]

    def test_requires_a_file(self, client, ocr_agent):
        response = client.post(RECEIPT_URL, {}, **AUTH)

        assert response.status_code == 422
        ocr_agent.run.assert_not_awaited()


def _scan_response(client, ocr_agent, scan):
    ocr_agent.run.return_value = SimpleNamespace(output=scan)
    with patch(
        "receipts.image_hosting.cloudinary_upload_file",
        return_value="https://res.cloudinary.com/r.jpg",
    ):
        response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)
    assert response.status_code == 200
    return response.json()


def _accepted_by_installed_app(body):
    items = body["receipt_items"]
    return client_gate.accepts(
        costs=[item["cost"] for item in items],
        tax_percentage=body["tax_percentage"],
        total=body["total_amount"],
        item_orders=[item["item_order"] for item in items],
    )


class TestDiscountedReceipts:
    """Item costs carry every discount, so installed apps need no release."""

    def test_each_line_costs_its_share_after_every_discount(self, client, ocr_agent):
        body = _scan_response(client, ocr_agent, scans.scan(scans.vector("sugi")))

        assert [
            (item["item_order"], item["cost"], item["discount"])
            for item in body["receipt_items"]
        ] == [
            (1, 837.0, 150.0),
            (2, 744.0, 133.0),
            (3, 1809.0, 501.0),
            (4, 1533.0, 425.0),
            (5, 650.0, 117.0),
            (6, 236.0, 42.0),
        ]
        assert body["total_amount"] == 5809.0
        assert body["tax_percentage"] == 10.0
        assert body["jp_shop_name"] == "テスト薬局"
        assert body["receipt_date"] == "2026-11-23T15:16:00"
        assert _accepted_by_installed_app(body)

    def test_a_receipt_a_person_must_review_allocates_nothing(self, client, ocr_agent):
        ambiguous = scans.vector("sugi_both_item_discounts_read_under_one_item")

        body = _scan_response(client, ocr_agent, scans.scan(ambiguous))

        assert {item["cost"] for item in body["receipt_items"]} == {0.0}
        assert {item["discount"] for item in body["receipt_items"]} == {0.0}
        assert [item["list_price"] for item in body["receipt_items"]] == [
            987.0,
            877.0,
            2310.0,
            1958.0,
            767.0,
            278.0,
        ]
        assert body["total_amount"] == 5809.0
        # Fail closed: the installed app rejects it rather than file a guess.
        assert not _accepted_by_installed_app(body)

    def test_cents_are_returned_as_printed(self, client, ocr_agent):
        cents = scans.vector("coupon_before_tax_in_cents")

        body = _scan_response(client, ocr_agent, scans.scan(cents))

        assert [item["cost"] for item in body["receipt_items"]] == [11.72, 6.75, 4.51]
        assert body["tax_percentage"] == 8.25
        assert body["total_amount"] == 24.88
        assert _accepted_by_installed_app(body)

    def test_a_receipt_without_a_date_is_dated_now(self, client, ocr_agent):
        now = datetime(2026, 11, 23, 6, 16, tzinfo=UTC)
        undated = scans.scan(scans.vector("sugi"), receipt_date=None)

        with patch("receipts.api.timezone.now", return_value=now):
            body = _scan_response(client, ocr_agent, undated)

        assert body["receipt_date"] == "2026-11-23T06:16:00Z"


class TestSurcharges:
    def test_a_surcharge_share_is_not_counted_as_a_discount(self, client, ocr_agent):
        service = scans.vector("service_charge_with_tax_added_on_top")

        body = _scan_response(client, ocr_agent, scans.scan(service))

        assert [item["cost"] for item in body["receipt_items"]] == [
            13200.0,
            5280.0,
            605.0,
        ]
        assert {item["discount"] for item in body["receipt_items"]} == {0.0}
        assert _accepted_by_installed_app(body)

    def test_discounts_stay_exact_when_tax_is_spread_into_costs(
        self, client, ocr_agent
    ):
        spread = scans.vector("coupon_the_store_split_per_tax_group")

        body = _scan_response(client, ocr_agent, scans.scan(spread))

        assert [item["cost"] for item in body["receipt_items"]] == [1234.0, 942.0]
        assert [item["discount"] for item in body["receipt_items"]] == [58.0, 43.0]


class TestFailedReads:
    def test_a_read_that_fails_after_a_usable_attempt_answers_with_it(
        self, client, ocr_agent, caplog
    ):
        usable = scans.scan(scans.vector("sugi_points_line_dropped"))

        async def fail_after_one_attempt(prompt, deps):
            deps.record(usable, allocate_scan(usable)[1])
            raise UnexpectedModelBehavior("Exceeded maximum output retries (2)")

        ocr_agent.run.side_effect = fail_after_one_attempt
        with (
            patch(
                "receipts.image_hosting.cloudinary_upload_file",
                return_value="https://res.cloudinary.com/r.jpg",
            ),
            caplog.at_level(logging.WARNING, logger="receipts.ocr"),
        ):
            response = client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["total_amount"] == 5809.0
        assert not _accepted_by_installed_app(body)
        (record,) = caplog.records
        assert "using the best attempt" in record.getMessage()
        assert isinstance(record.exc_info[1], UnexpectedModelBehavior)

    def test_a_read_with_no_usable_attempt_is_an_error(self, client, ocr_agent):
        ocr_agent.run.side_effect = UnexpectedModelBehavior("Exceeded retries")

        with (
            patch("receipts.image_hosting.cloudinary_upload_file") as cloudinary,
            patch("receipts.image_hosting.catbox_upload_file") as catbox,
            pytest.raises(UnexpectedModelBehavior),
        ):
            client.post(RECEIPT_URL, {"file": _upload()}, **AUTH)

        # The photo is hosted only after a read, so a failed scan uploads nothing.
        cloudinary.assert_not_called()
        catbox.assert_not_called()


ALLOCATIONS_URL = "/api/v1/receipts/allocations/"


def _correction(body, **changes):
    """Post a scan response back as a correction, as the app's review step does."""
    return {
        "decimal_places": body["decimal_places"],
        "receipt_items": body["receipt_items"],
        "adjustments": body["adjustments"],
        "tax_lines": body["tax_lines"],
        "total_amount": body["total_amount"],
        **changes,
    }


class TestReviewFields:
    def test_a_reconciled_scan_lists_each_adjustment_and_its_shares(
        self, client, ocr_agent
    ):
        body = _scan_response(client, ocr_agent, scans.scan(scans.vector("sugi")))

        assert [item["list_price"] for item in body["receipt_items"]] == [
            987.0,
            877.0,
            2310.0,
            1958.0,
            767.0,
            278.0,
        ]
        assert [
            (
                entry["printed_label"],
                entry["kind"],
                entry["status"],
                entry["item_orders"],
                {
                    share["item_order"]: share["amount"]
                    for share in entry["allocations"]
                },
            )
            for entry in body["adjustments"]
        ] == [
            ("1品割引", "discount", "verified", [3], {3: 347.0}),
            ("1品割引", "discount", "verified", [4], {4: 294.0}),
            (
                "全品割引",
                "discount",
                "verified",
                [1, 2, 5, 6],
                {1: 79.0, 2: 70.0, 5: 62.0, 6: 22.0},
            ),
            (
                "スギポイント値引金額",
                "points",
                "assumed",
                [1, 2, 3, 4, 5, 6],
                {1: 71.0, 2: 63.0, 3: 154.0, 4: 131.0, 5: 55.0, 6: 20.0},
            ),
        ]
        assert body["adjustments"][2]["percentage"] == 8.0
        assert body["adjustments"][2]["eligible_subtotal"] == 2909.0
        assert body["reconciliation"] == {"status": "reconciled", "difference": 0.0}
        assert body["decimal_places"] == 0

    def test_a_scan_needing_review_shows_scopes_but_no_shares(self, client, ocr_agent):
        ambiguous = scans.vector("sugi_both_item_discounts_read_under_one_item")

        body = _scan_response(client, ocr_agent, scans.scan(ambiguous))

        assert body["reconciliation"] == {"status": "needs_review", "difference": 0.0}
        assert [
            (entry["status"], entry["item_orders"]) for entry in body["adjustments"]
        ] == [
            ("verified", [3]),
            ("unresolved", [3]),
            ("verified", [1, 2, 5, 6]),
            ("assumed", [1, 2, 3, 4, 5, 6]),
        ]
        assert {len(entry["allocations"]) for entry in body["adjustments"]} == {0}

    def test_a_mismatch_reports_the_unexplained_difference(self, client, ocr_agent):
        dropped = scans.vector("sugi_points_line_dropped")

        body = _scan_response(client, ocr_agent, scans.scan(dropped))

        assert body["reconciliation"] == {"status": "mismatch", "difference": -494.0}


class TestCorrections:
    """A person picks the items for each adjustment and the server recalculates."""

    def _review(self, client, ocr_agent, name):
        return _scan_response(client, ocr_agent, scans.scan(scans.vector(name)))

    def _post(self, client, payload):
        return client.post(
            ALLOCATIONS_URL, payload, content_type="application/json", **AUTH
        )

    def test_choosing_the_items_reconciles_the_receipt(self, client, ocr_agent):
        body = self._review(
            client, ocr_agent, "sugi_both_item_discounts_read_under_one_item"
        )
        adjustments = body["adjustments"]
        adjustments[1]["item_orders"] = [4]

        response = self._post(client, _correction(body, adjustments=adjustments))

        assert response.status_code == 200
        result = response.json()
        assert [item["cost"] for item in result["receipt_items"]] == [
            837.0,
            744.0,
            1809.0,
            1533.0,
            650.0,
            236.0,
        ]
        assert [item["discount"] for item in result["receipt_items"]] == [
            150.0,
            133.0,
            501.0,
            425.0,
            117.0,
            42.0,
        ]
        assert result["reconciliation"] == {"status": "reconciled", "difference": 0.0}
        assert result["total_amount"] == 5809.0
        assert result["tax_percentage"] == 10.0
        assert {entry["status"] for entry in result["adjustments"]} == {"verified"}
        assert result["adjustments"][2]["percentage"] == 8.0
        assert result["adjustments"][2]["eligible_subtotal"] == 2909.0
        assert _accepted_by_installed_app(
            {**body, **result, "receipt_items": result["receipt_items"]}
        )

    def test_a_scan_posted_back_unchanged_gives_the_same_money(self, client, ocr_agent):
        body = self._review(client, ocr_agent, "sugi")

        result = self._post(client, _correction(body)).json()

        assert [item["cost"] for item in result["receipt_items"]] == [
            item["cost"] for item in body["receipt_items"]
        ]
        assert result["adjustments"] == [
            {**entry, "status": "verified"} for entry in body["adjustments"]
        ]

    def test_tax_printed_without_a_rate_is_spread_again(self, client, ocr_agent):
        body = self._review(client, ocr_agent, "tax_amount_printed_without_a_rate")

        result = self._post(client, _correction(body)).json()

        assert [item["cost"] for item in result["receipt_items"]] == [
            22.77,
            11.33,
            45.3,
        ]
        assert result["reconciliation"]["status"] == "reconciled"

    def test_removing_a_line_that_was_paid_leaves_a_mismatch(self, client, ocr_agent):
        body = self._review(client, ocr_agent, "sugi")

        result = self._post(
            client, _correction(body, adjustments=body["adjustments"][:3])
        ).json()

        assert result["reconciliation"] == {"status": "mismatch", "difference": -494.0}
        assert {item["cost"] for item in result["receipt_items"]} == {0.0}
        assert {item["discount"] for item in result["receipt_items"]} == {0.0}

    def test_a_coupon_larger_than_its_item_can_be_spread_wider(self, client, ocr_agent):
        body = self._review(client, ocr_agent, "discount_larger_than_its_item")
        adjustments = body["adjustments"]
        adjustments[0]["item_orders"] = [1, 2]

        result = self._post(client, _correction(body, adjustments=adjustments)).json()

        assert [item["cost"] for item in result["receipt_items"]] == [83.0, 415.0]

    @pytest.mark.parametrize(
        ("name", "line", "chosen"),
        [
            # 10% of 300 is not the printed 50.
            ("percent_discount_on_named_items", 0, [1]),
            # 750 + 1,100 is not the printed base 1,250.
            ("base_recovered_by_subset_search", 0, [3, 4]),
        ],
    )
    def test_a_choice_the_printed_numbers_contradict_is_marked_for_checking(
        self, client, ocr_agent, name, line, chosen
    ):
        body = self._review(client, ocr_agent, name)
        adjustments = body["adjustments"]
        adjustments[line]["item_orders"] = chosen

        result = self._post(client, _correction(body, adjustments=adjustments)).json()

        assert result["reconciliation"]["status"] == "reconciled"
        assert result["adjustments"][line]["status"] == "assumed"

    def test_a_choice_the_per_rate_bases_contradict_goes_back_to_review(
        self, client, ocr_agent
    ):
        body = self._review(client, ocr_agent, "sugi")
        adjustments = body["adjustments"]
        # The 8% line on items whose costs do not make the printed 2,909, and the
        # 15% line on an item 15% of which is not 294: the snack's share then no
        # longer matches its printed 8% base.
        adjustments[2]["item_orders"] = [1, 2, 5]
        adjustments[1]["item_orders"] = [5]

        result = self._post(client, _correction(body, adjustments=adjustments)).json()

        assert result["reconciliation"]["status"] == "needs_review"
        assert [entry["status"] for entry in result["adjustments"]] == [
            "verified",
            "unresolved",
            "unresolved",
            "verified",
        ]
        assert {item["cost"] for item in result["receipt_items"]} == {0.0}

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"adjustments": [{"item_orders": [9]}]}, "must apply to items"),
            ({"adjustments": [{"item_orders": []}]}, "must apply to items"),
            ({"adjustments": [{"item_orders": [1]}]}, "larger than what its items"),
            ({"adjustments": [{"amount": 0}]}, "must be positive"),
            ({"receipt_items": [{"list_price": -1}]}, "cannot be negative"),
            ({"receipt_items": "duplicate"}, "its own item_order"),
            ({"receipt_items": []}, "at least one item"),
            ({"total_amount": 0}, "The total must be positive"),
            ({"decimal_places": 7}, "decimal_places must be between"),
        ],
    )
    def test_a_choice_that_cannot_be_applied_is_rejected(
        self, client, ocr_agent, change, message
    ):
        body = self._review(client, ocr_agent, "discount_larger_than_its_item")
        payload = _correction(body)
        if "adjustments" in change:
            payload["adjustments"] = [
                {**payload["adjustments"][0], **change["adjustments"][0]}
            ]
        elif change.get("receipt_items") == "duplicate":
            payload["receipt_items"] = [
                {**item, "item_order": 1} for item in payload["receipt_items"]
            ]
        elif change.get("receipt_items") == []:
            payload["receipt_items"] = []
        elif "receipt_items" in change:
            payload["receipt_items"] = [
                {**payload["receipt_items"][0], **change["receipt_items"][0]},
                *payload["receipt_items"][1:],
            ]
        else:
            payload.update(change)

        response = self._post(client, payload)

        assert response.status_code == 422
        assert message in response.json()["detail"]

    def test_corrections_need_the_app_token(self, client):
        response = client.post(ALLOCATIONS_URL, {}, content_type="application/json")

        assert response.status_code == 401
