from __future__ import annotations


def normalize_text_mention_aliases(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    aliases = set()
    for item in value:
        if not isinstance(item, str):
            continue
        alias = item.strip()
        if not alias or len(alias) > 128:
            continue
        if any(character in "@＠\r\n" or ord(character) < 32 for character in alias):
            continue
        aliases.add(alias)
    return tuple(sorted(aliases, key=lambda alias: (-len(alias), alias)))


def strip_text_mention_prefix(content: str, aliases: tuple[str, ...]) -> tuple[str, str]:
    spacing = "\u200b\ufeff"
    separators = ":：,，"
    text = content.lstrip().lstrip(spacing).lstrip()
    if not text.startswith(("@", "＠")):
        return content, ""
    for alias in aliases:
        if not text[1:].startswith(alias):
            continue
        remainder = text[len(alias) + 1:]
        if remainder and not (remainder[0].isspace() or remainder[0] in spacing + separators):
            continue
        while remainder and (remainder[0].isspace() or remainder[0] in spacing + separators):
            remainder = remainder[1:]
        return remainder, alias
    return content, ""
