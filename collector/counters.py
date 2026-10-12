"""File reading and unsigned counter parsing for passive collectors."""
MAX_COUNTER = 2**64 - 1


def counter(text):
    """An unsigned 64-bit counter in ASCII digits; ValueError otherwise (sign, decimals, other digits)."""
    value = int(text) if text.isascii() and text.isdigit() else -1
    if not 0 <= value <= MAX_COUNTER:
        raise ValueError(text)
    return value


def read_file(path):
    with open(path, "rb") as f:
        return f.read()
