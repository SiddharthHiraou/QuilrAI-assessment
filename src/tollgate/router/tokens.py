"""Token estimates for reservations.

These run high on purpose. An overestimate only holds budget for the few
seconds until the provider reports real usage and the row is settled; an
underestimate lets a burst of requests overshoot the limit before anyone knows.
"""

import math

# Roughly 4 characters per token for English on current tokenizers. 3 is
# pessimistic on purpose, and covers code and non-English text better.
CHARS_PER_TOKEN = 3
PER_MESSAGE_OVERHEAD = 4
DEFAULT_MAX_TOKENS = 1024


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def estimate_prompt_tokens(messages):
    total = 3
    for message in messages:
        if isinstance(message, dict):
            text = _text_of(message.get("content"))
            total += PER_MESSAGE_OVERHEAD + math.ceil(len(text) / CHARS_PER_TOKEN)
    return total


def tokens_for_chars(n):
    return math.ceil(n / CHARS_PER_TOKEN)
