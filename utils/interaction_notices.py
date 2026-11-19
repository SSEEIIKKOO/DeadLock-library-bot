"""discord.py interaction ephemeral notices with delayed deletion.

Do not pass delete_after to InteractionResponse.send_message or Webhook.send.
"""
from __future__ import annotations
import asyncio
import logging
import discord

log = logging.getLogger(__name__)

async def _erase_original(interaction: discord.Interaction, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

async def _erase_followup(message: discord.WebhookMessage, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

async def temp_response(interaction: discord.Interaction, *args, delete_after: float = 20, **kwargs) -> None:
    await interaction.response.send_message(*args, **kwargs)
    asyncio.create_task(_erase_original(interaction, delete_after))

async def temp_followup(interaction: discord.Interaction, *args, delete_after: float = 20, **kwargs):
    # Webhook.send accepts wait=True but not delete_after.
    # delete_after is for our own timer; never forward it to Discord's Webhook.send.
    kwargs.pop("delete_after", None)
    kwargs.pop("wait", None)
    message = await interaction.followup.send(*args, wait=True, **kwargs)
    asyncio.create_task(_erase_followup(message, delete_after))
    return message
