from typing import DefaultDict, Dict, Iterable, List

import graphene
from django.core.exceptions import ValidationError
from django.db import transaction

from ...channel import models
from ...checkout.models import Checkout
from ...core.permissions import ChannelPermissions
from ...order.models import Order
from ..core.mutations import BaseMutation, ModelDeleteMutation, ModelMutation
from ..core.types.common import ChannelError, ChannelErrorCode
from ..core.utils import get_duplicated_values, get_duplicates_ids
from ..utils import resolve_global_ids_to_primary_keys
from .types import Channel


class ChannelCreateInput(graphene.InputObjectType):
    name = graphene.String(description="Name of the channel.", required=True)
    slug = graphene.String(description="Slug of the channel.", required=True)
    currency_code = graphene.String(
        description="Currency of the channel.", required=True
    )


class ChannelCreate(ModelMutation):
    class Arguments:
        input = ChannelCreateInput(
            required=True, description="Fields required to create channel."
        )

    class Meta:
        description = "Creates new channel."
        model = models.Channel
        permissions = (ChannelPermissions.MANAGE_CHANNELS,)
        error_type_class = ChannelError
        error_type_field = "channel_errors"

    @classmethod
    def get_type_for_model(cls):
        return Channel


class ChannelUpdateInput(graphene.InputObjectType):
    name = graphene.String(description="Name of the channel.")
    slug = graphene.String(description="Slug of the channel.")


class ChannelUpdate(ModelMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of a channel to update.")
        input = ChannelUpdateInput(
            description="Fields required to update a channel.", required=True
        )

    class Meta:
        description = "Update a channel."
        model = models.Channel
        permissions = (ChannelPermissions.MANAGE_CHANNELS,)
        error_type_class = ChannelError
        error_type_field = "channel_errors"


class ChannelDeleteInput(graphene.InputObjectType):
    target_channel = graphene.ID(
        required=True,
        description="ID of channel to migrate orders from origin channel.",
    )


class ChannelDelete(ModelDeleteMutation):
    class Arguments:
        id = graphene.ID(required=True, description="ID of a channel to delete.")
        input = ChannelDeleteInput(
            required=True, description="Fields required to delete a channel."
        )

    class Meta:
        description = (
            "Delete a channel. Orders associated with the deleted "
            "channel will be moved to the target channel. "
            "Checkouts, product availability, and pricing will be removed."
        )
        model = models.Channel
        permissions = (ChannelPermissions.MANAGE_CHANNELS,)
        error_type_class = ChannelError
        error_type_field = "channel_errors"

    @classmethod
    def validate_input(cls, origin_channel, target_channel):
        if origin_channel.id == target_channel.id:
            raise ValidationError(
                {
                    "target_channel": ValidationError(
                        "channelID and targetChannelID cannot be the same. "
                        "Use different target channel ID.",
                        code=ChannelErrorCode.CHANNEL_TARGET_ID_MUST_BE_DIFFERENT,
                    )
                }
            )
        origin_channel_currency = origin_channel.currency_code
        target_channel_currency = target_channel.currency_code
        if origin_channel_currency != target_channel_currency:
            raise ValidationError(
                {
                    "target_channel": ValidationError(
                        f"Cannot migrate from {origin_channel_currency} "
                        f"to {target_channel_currency}. "
                        "Migration are allowed between the same currency",
                        code=ChannelErrorCode.CHANNELS_CURRENCY_MUST_BE_THE_SAME,
                    )
                }
            )

    @classmethod
    def perform_delete(cls, origin_channel, target_channel):
        cls.validate_input(origin_channel, target_channel)

        with transaction.atomic():
            origin_channel_id = origin_channel.id
            cls.migrate_orders_to_target_channel(origin_channel_id, target_channel.id)
            cls.delete_checkouts(origin_channel_id)

    @classmethod
    def migrate_orders_to_target_channel(cls, origin_channel, target_channel):
        Order.objects.select_for_update().filter(channel_id=origin_channel).update(
            channel=target_channel
        )

    @classmethod
    def delete_checkouts(cls, origin_channel):
        Checkout.objects.select_for_update().filter(channel_id=origin_channel).delete()

    @classmethod
    def perform_mutation(cls, _root, info, **data):
        origin_channel = cls.get_node_or_error(info, data["id"], only_type=Channel)
        target_channel = cls.get_node_or_error(
            info, data["input"]["target_channel"], only_type=Channel
        )

        cls.perform_delete(origin_channel, target_channel)

        return super().perform_mutation(_root, info, **data)


ErrorType = DefaultDict[str, List[ValidationError]]


class BaseChannelListingMutation(BaseMutation):
    """Base channel listing mutation with basic channel validation."""

    class Meta:
        abstract = True

    @classmethod
    def validate_duplicated_channel_ids(
        cls,
        add_channels_ids: Iterable[str],
        remove_channels_ids: Iterable[str],
        errors: ErrorType,
        error_code,
    ):
        duplicated_ids = get_duplicates_ids(add_channels_ids, remove_channels_ids)
        if duplicated_ids:
            error_msg = (
                "The same object cannot be in both lists "
                "for adding and removing items."
            )
            errors["input"].append(
                ValidationError(
                    error_msg,
                    code=error_code,
                    params={"channels": list(duplicated_ids)},
                )
            )

    @classmethod
    def validate_duplicated_channel_values(
        cls, channels_ids: Iterable[str], field_name: str, errors: ErrorType, error_code
    ):
        duplicates = get_duplicated_values(channels_ids)
        if duplicates:
            errors[field_name].append(
                ValidationError(
                    "Duplicated channel ID.",
                    code=error_code,
                    params={"channels": duplicates},
                )
            )

    @classmethod
    def clean_channels(cls, info, input, errors: ErrorType, error_code) -> Dict:
        add_channels = input.get("add_channels", [])
        add_channels_ids = [channel["channel_id"] for channel in add_channels]
        remove_channels_ids = input.get("remove_channels", [])
        cls.validate_duplicated_channel_ids(
            add_channels_ids, remove_channels_ids, errors, error_code
        )
        cls.validate_duplicated_channel_values(
            add_channels_ids, "add_channels", errors, error_code
        )
        cls.validate_duplicated_channel_values(
            remove_channels_ids, "remove_channels", errors, error_code
        )

        if errors:
            return {}
        channels_to_add: List["models.Channel"] = []
        if add_channels_ids:
            channels_to_add = cls.get_nodes_or_error(  # type: ignore
                add_channels_ids, "channel_id", Channel
            )
        _, remove_channels_pks = resolve_global_ids_to_primary_keys(
            remove_channels_ids, Channel
        )

        cleaned_input = {"add_channels": [], "remove_channels": remove_channels_pks}

        for channel_listing, channel in zip(add_channels, channels_to_add):
            channel_listing["channel"] = channel
            cleaned_input["add_channels"].append(channel_listing)

        return cleaned_input