"""Match the filament a site listing recommends against the spools you own."""
import re
from typing import Optional

from app.models import Filament


def _color_key(value: Optional[str]) -> str:
    text = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    return text.replace("grey", "gray")


def _material_key(value: Optional[str]) -> str:
    parts = (value or "").upper().split()
    return parts[0] if parts else ""


def match_spool(suggestion: dict, spools: list) -> Optional[Filament]:
    """The best of your spools for a suggested {material, color}: same material,
    and a colour that is the same (exact) or contains / is contained in the
    suggested one. Ties go to the spool with the most filament left."""
    material = _material_key(suggestion.get("material"))
    color = _color_key(suggestion.get("color"))
    if not material:
        return None
    same_material = [s for s in spools if _material_key(s.material) == material]

    def best(candidates: list) -> Optional[Filament]:
        return max(candidates, key=lambda s: s.remaining_g, default=None)

    if not color:
        return None
    exact = [s for s in same_material if _color_key(s.color) == color]
    if exact:
        return best(exact)
    loose = [s for s in same_material if _color_key(s.color) and (_color_key(s.color) in color or color in _color_key(s.color))]
    return best(loose)


def spool_label(spool: Filament) -> str:
    return " ".join(x for x in (spool.material, spool.brand, spool.color) if x)
