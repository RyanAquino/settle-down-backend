from django.conf import settings
from ninja import Router, File, UploadedFile
from openai import AsyncOpenAI
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData
from .serializer import OCRReceiptPostOut

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(request, file: File[UploadedFile]):
    client = AsyncOpenAI(
        # base_url="https://api.llm7.io/v1",
        api_key=settings.LLM_API_KEY,
    )
    model = LLM7ChatModel(
        "gpt-5-nano-2025-08-07", provider=OpenAIProvider(openai_client=client)
    )
    agent = Agent(
        model=model,
        # output_type=PromptedOutput(ReceiptData),
        output_type=ReceiptData,
        system_prompt="""
            You are a helpful assistant. Analyze the provided receipt image and return a structured output with:
            
            1. Shop name (if present).
            2. List of purchased items in the order they appear on the receipt.
               a. Each item should include both the Japanese text and its English translation.
               b. If a discount applies, subtract it from the item directly above.
        """,
        # 3. Return only the structured list (no explanations, extra text, or commentary).
    )

    result = await agent.run(
        [
            """
            here is the receipt image below.
            """,
            BinaryContent(data=file.read(), media_type="image/jpg"),
        ]
    )
    results = result.output

    return results
