"""Import-context false-positive filter for vendor pattern matches."""

from __future__ import annotations

from pathlib import Path

VENDOR_MARKERS = {
    "openai": ("openai", "OpenAI", "OPENAI_API_KEY", "chat.completions", "gpt-"),
    "stripe": ("stripe", "Stripe", "STRIPE_"),
}


def file_has_vendor_context(path: Path, content: str, vendor: str) -> bool:
    """
    Return True if the file looks related to the vendor (imports, client usage,
    env keys). Config / env files are always treated as in-context.
    """
    name = path.name.lower()
    if name.startswith(".env") or name.endswith(
        (".yaml", ".yml", ".json", ".toml", ".ini")
    ):
        return True

    markers = VENDOR_MARKERS.get(vendor.lower(), (vendor.lower(),))
    lowered = content.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return True

    # Broad import scan
    for line in content.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and vendor.lower() in stripped.lower():
            return True
        if "require(" in stripped and vendor.lower() in stripped.lower():
            return True
    return False
