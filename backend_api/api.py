from django.conf import settings
from googletrans import Translator
from ninja import Router, File, UploadedFile
from openai import AsyncOpenAI
from pydantic_ai import Agent, BinaryContent, ModelRetry
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData
from .serializer import OCRReceiptPostOut
from .services import catbox_upload_file, cloudinary_upload_file

router = Router()


@router.post("/receipt-items/", response={200: OCRReceiptPostOut})
async def post_ocr_receipt(request, file: File[UploadedFile]):
    client = AsyncOpenAI(
        # base_url="https://api.llm7.io/v1",
        api_key=settings.LLM_API_KEY,
    )
    # "gpt-5-nano-2025-08-07"
    model = LLM7ChatModel(
        "gpt-5-mini", provider=OpenAIProvider(openai_client=client)
    )
    agent = Agent(
        model=model,
        # output_type=PromptedOutput(ReceiptData),
        output_type=ReceiptData,
        system_prompt="""
            You are an expert system for reading receipts.
            You will be given receipts in a form of image. The text in the image receipt may be in Japanese and/or English.
            Your task it to extract the following text information from the provided image.
            Always translate the text to English before processing.
            if there is no cost on a particular receipt item, then don't include it.
            
            1. Receipt items which are listed in the receipt containing the english or japanese item name, item order, cost, quantity, and discount if any.
                a. If a discount applies, subtract it from the item directly above. Do not include discounts as receipt items.
            2. English and Japanese  name of the shop.
            3. Tax percentage if any.
            4. Total cost amount of all items in the receipt.
            5. Datetime of the receipt.
            
            The total amount in the receipt will always be correct. Make sure the receipt items you extracted add up to the total amount.
            It might be possible that the total cost already includes tax.
        """,
    )

    @agent.tool_plain
    async def translate_jp_to_en_text(text: str) -> str:
        """Translate Japanese text to English text."""

        translator = Translator()
        try:
            text_result = await translator.translate(text, dest='en')
        except Exception:
            raise ModelRetry("Translation failed, please try with a shorter chunk of text.")

        return text_result.text


    result = await agent.run(
        [
            """
            here are the image receipts:
            """,
            BinaryContent(data=file.read(), media_type="image/jpg"),
        ]
    )
    results = result.output

    try:
        url = catbox_upload_file(file)
    except Exception as e:
        print(f"exception: {str(e)}")
        url = cloudinary_upload_file(file)

    results.receipt_image_url = url

    return results
