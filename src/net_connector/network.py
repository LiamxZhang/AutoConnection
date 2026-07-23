"""Legacy-compatible network protocol helpers."""


def rc4_hex(source: str, key_text: str) -> str:
    """Return the legacy RC4 transformation as lowercase hexadecimal."""
    source = str(source).strip()
    key_text = str(key_text)
    if not key_text:
        raise ValueError("key must not be empty")

    state = list(range(256))
    key_index = 0
    for index in range(256):
        key_index = (key_index + state[index] + ord(key_text[index % len(key_text)])) % 256
        state[index], state[key_index] = state[key_index], state[index]

    index = key_index = 0
    encrypted = []
    for character in source:
        index = (index + 1) % 256
        key_index = (key_index + state[index]) % 256
        state[index], state[key_index] = state[key_index], state[index]
        encrypted.append(f"{ord(character) ^ state[(state[index] + state[key_index]) % 256]:02x}")
    return "".join(encrypted)
