"""The one token-count approximation every component uses for budgets like D18's 500."""

import math

CHARACTERS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARACTERS_PER_TOKEN)
