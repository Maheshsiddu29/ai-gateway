"""Input token estimation for token-per-minute reservations.

The gateway cannot run each provider's tokenizer, so it estimates. The estimate
divides the UTF-8 size of each message by ``ESTIMATED_BYTES_PER_TOKEN`` and adds a
fixed per-message and per-request overhead for role and formatting tokens.

Three bytes per token is deliberately below the usual figure for English text
(about four), so English prompts are over-estimated; for scripts that use three
UTF-8 bytes per character it is roughly one token per character. Any difference is
corrected when the reservation is settled with the usage the provider reports.
"""

from collections.abc import Sequence
from math import ceil

from gateway.schemas.completions import ChatMessage

ESTIMATED_BYTES_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 4
REQUEST_OVERHEAD_TOKENS = 3


def estimate_input_tokens(messages: Sequence[ChatMessage]) -> int:
    return REQUEST_OVERHEAD_TOKENS + sum(
        ceil(len(message.content.encode()) / ESTIMATED_BYTES_PER_TOKEN) + MESSAGE_OVERHEAD_TOKENS
        for message in messages
    )
