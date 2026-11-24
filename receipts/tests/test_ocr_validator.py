"""The OCR output validator, driven through the real agent loop offline.

A scripted ``FunctionModel`` stands in for the provider and returns one
transcription per request, so these tests pin how many model requests a scan
costs and which transcription the run ends with. No network is touched.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from receipts.allocation import allocate_scan
from receipts.ocr import ScanAttempts, _build_agent
from receipts.tests import scans
from receipts.transcription import ReceiptScan

SUGI = scans.scan_payload(scans.vector("sugi"))
# Needs review: both 15% lines read under one item.
AMBIGUOUS = scans.scan_payload(
    scans.vector("sugi_both_item_discounts_read_under_one_item")
)
# Does not add up: the points line is missing.
POINTS_DROPPED = scans.scan_payload(scans.vector("sugi_points_line_dropped"))
# Cannot be read as money at all.
NEGATIVE_PRICE = {
    **SUGI,
    "items": [{**SUGI["items"][0], "printed_price": -987}, *SUGI["items"][1:]],
}


class Script:
    """Answers each model request with the next scripted transcription."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def respond(self, messages, info: AgentInfo) -> ModelResponse:
        self.requests.append(messages)
        payload = self.payloads[min(len(self.requests), len(self.payloads)) - 1]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=payload)]
        )

    def retry_prompts(self):
        return [
            part.content
            for messages in self.requests
            for message in messages
            for part in message.parts
            if isinstance(part, RetryPromptPart)
        ]


def _run(script):
    attempts = ScanAttempts()
    result = _build_agent(FunctionModel(script.respond)).run_sync(
        "here is the image receipt:", deps=attempts
    )
    return result.output, attempts


def _status(scan: ReceiptScan) -> str:
    return allocate_scan(scan)[1].status


class TestOneRequestWhenNothingCanImprove:
    def test_a_reconciled_scan_is_accepted_at_once(self):
        script = Script(SUGI)

        output, _ = _run(script)

        assert len(script.requests) == 1
        assert output == ReceiptScan.model_validate(SUGI)

    def test_a_scan_that_needs_a_person_is_not_read_again(self):
        script = Script(AMBIGUOUS, SUGI)

        output, _ = _run(script)

        assert len(script.requests) == 1
        assert _status(output) == "needs_review"


class TestTargetedReRead:
    def test_a_mismatch_is_read_once_more(self):
        script = Script(POINTS_DROPPED, SUGI)

        output, attempts = _run(script)

        assert len(script.requests) == 2
        assert _status(output) == "reconciled"
        assert [allocation.status for _, allocation in attempts.attempts] == [
            "mismatch",
            "reconciled",
        ]
        (message,) = script.retry_prompts()
        assert "difference of 494" in message
        assert "Do not add a line that is not printed" in message

    def test_only_one_re_read_is_spent_on_a_mismatch(self):
        script = Script(POINTS_DROPPED, POINTS_DROPPED, SUGI)

        output, _ = _run(script)

        assert len(script.requests) == 2
        assert _status(output) == "mismatch"

    def test_the_best_attempt_wins_over_the_last(self):
        script = Script(POINTS_DROPPED, AMBIGUOUS)

        output, _ = _run(script)

        assert len(script.requests) == 2
        assert _status(output) == "needs_review"

    def test_an_earlier_attempt_wins_a_tie(self):
        later = {**POINTS_DROPPED, "jp_shop_name": "二回目"}
        script = Script(POINTS_DROPPED, later)

        output, _ = _run(script)

        assert output.jp_shop_name == POINTS_DROPPED["jp_shop_name"]

    def test_no_re_read_is_left_after_two_unreadable_attempts(self):
        script = Script(NEGATIVE_PRICE, NEGATIVE_PRICE, POINTS_DROPPED, SUGI)

        output, _ = _run(script)

        assert len(script.requests) == 3
        assert _status(output) == "mismatch"


class TestUnreadableTranscriptions:
    def test_an_unreadable_scan_is_read_again(self):
        script = Script(NEGATIVE_PRICE, SUGI)

        output, attempts = _run(script)

        assert len(script.requests) == 2
        assert _status(output) == "reconciled"
        assert len(attempts.attempts) == 1
        (message,) = script.retry_prompts()
        assert "negative price" in message

    def test_three_unreadable_scans_exhaust_the_retries(self):
        script = Script(NEGATIVE_PRICE)
        attempts = ScanAttempts()

        with pytest.raises(UnexpectedModelBehavior):
            _build_agent(FunctionModel(script.respond)).run_sync(
                "receipt", deps=attempts
            )

        assert len(script.requests) == 3
        assert attempts.best() is None

    def test_a_usable_attempt_survives_a_run_that_fails_later(self):
        script = Script(POINTS_DROPPED, NEGATIVE_PRICE)
        attempts = ScanAttempts()

        with pytest.raises(UnexpectedModelBehavior):
            _build_agent(FunctionModel(script.respond)).run_sync(
                "receipt", deps=attempts
            )

        best = attempts.best()
        assert best is not None
        assert _status(best) == "mismatch"


class TranslatingScript(Script):
    """Calls the translate tool on each of the first ``calls`` requests."""

    def __init__(self, calls, *payloads):
        super().__init__(*payloads)
        self.calls = calls

    def respond(self, messages, info: AgentInfo) -> ModelResponse:
        if len(self.requests) < self.calls:
            self.requests.append(messages)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="translate_jp_to_en_text", args={"text": "おにぎり"}
                    )
                ]
            )
        return super().respond(messages, info)


class TestTranslationFailures:
    def test_a_scan_finishes_when_translation_keeps_failing(self):
        # googletrans scrapes an unofficial endpoint; a blocked connection is
        # one way it fails. The model reads Japanese itself, so the scan must
        # not depend on it.
        translator = MagicMock()
        translator.__aenter__.return_value = translator
        translator.translate = AsyncMock(side_effect=httpx.ConnectError("blocked"))
        script = TranslatingScript(3, SUGI)

        with patch("receipts.ocr.Translator", return_value=translator):
            output, _ = _run(script)

        assert output == ReceiptScan.model_validate(SUGI)
        assert len(script.requests) == 4
        assert translator.translate.await_count == 3
