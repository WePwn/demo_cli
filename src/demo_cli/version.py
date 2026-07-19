"""Single source of truth for the package version."""

import re

__version__ = "0.4.0b8"


def release_tag() -> str:
    """The git tag matching this version: 0.4.0b8 -> v0.4.0-beta.8.

    Derived from __version__ so any command we print for a user to run stays
    pinned to the exact code they are looking at, and cannot drift back to a
    moving branch when the version is bumped.
    """
    m = re.match(r"^(\d+\.\d+\.\d+)(?:b(\d+))?$", __version__)
    if not m:
        return "v" + __version__
    base, beta = m.groups()
    return f"v{base}-beta.{beta}" if beta else f"v{base}"
