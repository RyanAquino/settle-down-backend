from django.conf import settings
from django.contrib.auth.models import User
from ninja import Router, File, UploadedFile, Path, PatchDict, Query
from ninja.errors import HttpError
from ninja.pagination import paginate
from openai import AsyncOpenAI
from pydantic_ai import Agent, PromptedOutput, BinaryContent
from pydantic_ai.providers.openai import OpenAIProvider

from .dataclasses.llm7_override import LLM7ChatModel
from .dataclasses.receipt_item import ReceiptData
from .models import ReceiptItem, Receipt
from .serializer import ReceiptItemGetOut, UserGetOutSchema, UserPostIn, UserPath, ReceiptPath, ReceiptGetOut, \
    ReceiptItemPostIn, ReceiptItemPath, UserFilterQuery, ReceiptPatchIn, OCRReceiptPostOut

router = Router()


@router.get("/users/", response={200: list[UserGetOutSchema]})
@paginate
def get_users(request, query_params: Query[UserFilterQuery]):
    users = User.objects.all()

    if query_params.group_id:
        users = users.filter(settleupgroup__group_id=query_params.group_id)

    return users

@router.post("/users/", response={201: UserGetOutSchema})
def post_users(request, payload: UserPostIn):
    if User.objects.filter(username=payload.name).exists():
        raise HttpError(400, "User with this username already exists")
    return User.objects.create_user(username=payload.name)


@router.delete("/users/{user_id}/", response={204: None})
def delete_users(request, path_params: Path[UserPath]):
    User.objects.filter(id=path_params.user_id).delete()
    return 204, None


@router.get("/", response={200: list[ReceiptGetOut]})
@paginate
def get_receipt_items(request):
    return Receipt.objects.all()


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
    file.seek(0)

    results = result.output

    return results

@router.get("/receipt-items/{receipt_item_id}/", response={200: ReceiptItemGetOut})
def get_receipt_item(request, path_params: Path[ReceiptItemPath]):
    receipt_item = ReceiptItem.objects.filter(id=path_params.receipt_item_id).first()
    return 200, receipt_item

@router.patch("/receipt-items/{receipt_item_id}/", response={204: None})
def patch_receipt_item(request, payload: PatchDict[ReceiptItemPostIn], path_params: Path[ReceiptItemPath]):
    receipt_item = ReceiptItem.objects.filter(id=path_params.receipt_item_id).first()

    for k, v in payload.items():
        setattr(receipt_item, k, v)

    receipt_item.save()
    return 204, None


@router.patch("/{receipt_id}/", response={204: None})
def patch_receipt(request, path_params: Path[ReceiptPath], payload: ReceiptPatchIn):
    receipt = Receipt.objects.filter(id=path_params.receipt_id).first()
    user = User.objects.get(username=payload.paid_by)
    receipt.paid_by = user
    receipt.save()
    return 204, None

@router.delete("/{receipt_id}/", response={204: None})
def delete_receipts(request, path_params: Path[ReceiptPath]):
    Receipt.objects.filter(id=path_params.receipt_id).delete()
    return 204, None
