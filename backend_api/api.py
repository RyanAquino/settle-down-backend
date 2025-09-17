from django.conf import settings
from ninja import Router, File, UploadedFile
from openai import AsyncOpenAI
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData
from .serializer import OCRReceiptPostOut
import requests

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
            You are an expert system for reading receipts.
            You will be given an image of a receipt. The text in the image may be in Japanese and/or English.
            Your task it to extract the following text information from the provided image.
            
            1. Receipt items which are listed in the receipt containing the english and japanese item name, item order, cost, quantity, and discount if any.
                a. If a discount applies, subtract it from the item directly above.
            2. English and Japanese  name of the shop.
            3. Tax percentage if any.
            4. Total cost amount of all items in the receipt.
            5. Datetime of the receipt.
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

    def upload_file(file_obj: UploadedFile):
        file_obj.seek(0)
        response = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": file_obj},
        )
        if response.ok:
            return response.text.strip()

    url = upload_file(file)
    results.receipt_image_url = url

    return results
