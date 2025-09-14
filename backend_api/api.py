from django.conf import settings
from ninja import Router, File, UploadedFile
from openai import AsyncOpenAI
from pydantic_ai import Agent, PromptedOutput, BinaryContent
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
        output_type=PromptedOutput(ReceiptData),
        system_prompt="""
            You are a helpful assistant. Your task is to extract a list of all purchased items from a given image receipt and the shop name if provided.
            The receipt might contain multiple items and might be in Japanese text. You must make sure the results are returned in the order they are listed in the receipt
            The receipt might contain discounts, if so subtract those to the item mentioned above the discount or on the same level whichever is applicable.  
            Return ONLY the structured list with translated english/japanese text. 
        """,
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
