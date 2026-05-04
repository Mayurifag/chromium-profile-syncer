from __future__ import annotations


def version_display() -> str:
    try:
        from src import _build_info
    except ImportError:
        return "from source"
    tag = getattr(_build_info, "BUILD_TAG", "")
    if tag:
        return tag
    sha = getattr(_build_info, "BUILD_SHA", "")
    short = sha[:7] if sha and sha != "unknown" else ""
    return f"from source ({short})" if short else "from source"
