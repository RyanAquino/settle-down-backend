import pyrebase
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from backend_api.dataclasses.settleup import SettleUpGroup
from backend_api.models import SettleUpGroup as SettleUpGroupDB


class SettleUpClient:

    def __init__(self):
        firebase = pyrebase.initialize_app(settings.SETTLE_UP_CONFIG)
        pb_auth = firebase.auth()
        creds = pb_auth.sign_in_with_email_and_password(
            settings.SETTLE_UP_USER,
            settings.SETTLE_UP_PASSWORD,
        )
        self.user_id = creds.get("localId")
        self.auth_params = {"auth": creds.get("idToken")}

    def get_groups(self) -> list[SettleUpGroup]:
        groups = requests.get(
            f"{settings.SETTLE_UP_BASE_URL}/userGroups/{self.user_id}.json",
            params=self.auth_params,
        )
        groups = groups.json()
        groups_map = []

        for group_id, metadata in groups.items():
            group = requests.get(f"{settings.SETTLE_UP_BASE_URL}/groups/{group_id}.json", params=self.auth_params)
            group = group.json()
            groups_map.append(
                SettleUpGroup(
                    name=group["name"],
                    id=group_id,
                )
            )

        return groups_map

    @transaction.atomic
    def get_or_create_group_members(self, groups):
        for group in groups:
            members = requests.get(f"{settings.SETTLE_UP_BASE_URL}/members/{group.id}.json", params=self.auth_params)
            members = members.json()

            for member_id, metadata in members.items():
                name = metadata.get("name")
                user, _ = User.objects.get_or_create(username=member_id, first_name=name)
                SettleUpGroupDB.objects.get_or_create(
                    group_id=group.id,
                    user=user,
                )