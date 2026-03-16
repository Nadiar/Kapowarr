# -*- coding: utf-8 -*-

"""
Publisher imprint mapping and category groupings for the calendar feature.

All lookups are **name-based** (not ID-based) because the CV proxy
returns different publisher IDs than real ComicVine.  Publisher names
are consistent across backends.
"""

from typing import Dict, List, Optional

from backend.base.logging import LOGGER

# ---------------------------------------------------------------------------
# Preset publisher names (canonical list)
# ---------------------------------------------------------------------------
PRESET_PUBLISHER_NAMES = {
    'Marvel',
    'DC Comics',
    'Image',
    'IDW Publishing',
    'Dark Horse Comics',
    'BOOM! Studios',
    'Dynamite Entertainment',
    'VIZ Media',
    'Oni Press',
    'Kodansha Comics',
    'Archie Comics',
    'Titan Comics',
    'Fantagraphics Books',
    'Drawn and Quarterly',
    'Zenescope Entertainment',
    'Vault Comics',
    'AfterShock Comics',
    'AWA Studios',
    'Antarctic Press',
    'Mad Cave Studios',
}

# ---------------------------------------------------------------------------
# Name aliases  (variant name → canonical preset name)
# Normalizes naming differences between CV sources.
# ---------------------------------------------------------------------------
NAME_ALIASES: Dict[str, str] = {
    # Case / punctuation variants
    'Boom! Studios': 'BOOM! Studios',
    'BOOM Studios': 'BOOM! Studios',
    'Boom Studios': 'BOOM! Studios',
    # Short / alternate names
    'Viz': 'VIZ Media',
    'VIZ': 'VIZ Media',
    'Viz Media': 'VIZ Media',
    'Kodansha': 'Kodansha Comics',
    'Kodansha Comics USA': 'Kodansha Comics',
    'Image Comics': 'Image',
    'Aftershock Comics': 'AfterShock Comics',
    'Aftershock': 'AfterShock Comics',
    'IDW': 'IDW Publishing',
    'Oni': 'Oni Press',
    'Oni Press Inc.': 'Oni Press',
    'Dark Horse': 'Dark Horse Comics',
    'Titan': 'Titan Comics',
    'Titan Books': 'Titan Comics',
    'Archie': 'Archie Comics',
    'Archie Comic Publications': 'Archie Comics',
    'Mad Cave': 'Mad Cave Studios',
    'AWA': 'AWA Studios',
    'Vault': 'Vault Comics',
    'Dynamite': 'Dynamite Entertainment',
    'Fantagraphics': 'Fantagraphics Books',
    'Zenescope': 'Zenescope Entertainment',
}


def normalize_publisher_name(
    name: Optional[str]
) -> Optional[str]:
    """Normalize a publisher name to its canonical preset form.

    Checks NAME_ALIASES first (exact match), then falls back to
    the original name unchanged.

    Args:
        name: Raw publisher name from the CV API.

    Returns:
        Canonical name if an alias exists, otherwise the
        original name.  ``None`` if input is ``None``.
    """
    if not name:
        return name
    return NAME_ALIASES.get(name, name)


# ---------------------------------------------------------------------------
# Imprint → parent preset mapping  (imprint name → parent name)
# ---------------------------------------------------------------------------
IMPRINT_MAP: Dict[str, str] = {
    # Image imprints
    'Todd McFarlane Productions': 'Image',
    'Top Cow': 'Image',
    'Top Cow Productions': 'Image',
    'Skybound': 'Image',
    'Skybound Entertainment': 'Image',
    'WildStorm': 'Image',
    'WildStorm Productions': 'Image',
    'Shadowline': 'Image',
    'Image/Skybound': 'Image',

    # DC Comics imprints
    'Vertigo': 'DC Comics',
    'DC Black Label': 'DC Comics',
    'DC/Vertigo': 'DC Comics',
    'WildStorm/DC': 'DC Comics',
    'DC Ink': 'DC Comics',
    'DC Zoom': 'DC Comics',
    'CMX': 'DC Comics',
    'The New 52': 'DC Comics',
    'Zuda Comics': 'DC Comics',
    'DC/Young Animal': 'DC Comics',

    # Marvel imprints
    'MAX': 'Marvel',
    'MAX Comics': 'Marvel',
    'Icon Comics': 'Marvel',
    'Ultimate Comics': 'Marvel',
    'Marvel Knights': 'Marvel',
    'Marvel/Icon': 'Marvel',
    'Epic Comics': 'Marvel',
    'Marvel Soleil': 'Marvel',
    'Marvel UK': 'Marvel',
    'Star Comics': 'Marvel',
    'Marvel Digital Comics': 'Marvel',
}

# ---------------------------------------------------------------------------
# Publisher categories  (category name → list of preset names)
# ---------------------------------------------------------------------------
PUBLISHER_CATEGORIES: Dict[str, List[str]] = {
    'western': [
        'Marvel', 'DC Comics', 'Image', 'IDW Publishing',
        'Dark Horse Comics', 'BOOM! Studios',
        'Dynamite Entertainment', 'Oni Press', 'Archie Comics',
        'Titan Comics', 'Fantagraphics Books',
        'Drawn and Quarterly', 'Zenescope Entertainment',
        'Vault Comics', 'AfterShock Comics', 'AWA Studios',
        'Antarctic Press', 'Mad Cave Studios',
    ],
    'manga': ['VIZ Media', 'Kodansha Comics'],
}

# ---------------------------------------------------------------------------
# Unknown publisher negative cache
# ---------------------------------------------------------------------------
_unknown_publisher_cache: Dict[str, None] = {}


def resolve_parent_publisher(
    publisher_name: Optional[str]
) -> Optional[str]:
    """Resolve a publisher name to its parent preset publisher.

    Args:
        publisher_name: Publisher name from the CV/proxy API.

    Returns:
        Parent preset name if the publisher is a known imprint,
        or ``None`` if it is a preset itself, unknown, or cached
        as independent.
    """
    if not publisher_name:
        return None

    # Already a preset publisher — not an imprint
    if publisher_name in PRESET_PUBLISHER_NAMES:
        return None

    # Known imprint
    parent = IMPRINT_MAP.get(publisher_name)
    if parent is not None:
        return parent

    # Already checked and found independent
    if publisher_name in _unknown_publisher_cache:
        return None

    # First encounter — cache as independent
    _unknown_publisher_cache[publisher_name] = None
    LOGGER.debug(
        'Calendar: publisher %r cached as independent',
        publisher_name
    )
    return None


def get_publisher_categories() -> Dict[str, List[str]]:
    """Return publisher categories for the frontend group toggles.

    Returns:
        Dict mapping category name to list of publisher name strings.
    """
    return PUBLISHER_CATEGORIES
