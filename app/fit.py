"""Will a model fit on a printer's bed? A model's footprint may be turned a quarter turn; its height must fit as it is.
Unknown when the model has no size yet or the printer's bed is not filled in."""
from typing import Optional


def fits(model, printer) -> Optional[bool]:
    sizes = (model.bbox_x, model.bbox_y, model.bbox_z)
    if printer is None or not printer.bed_x or not printer.bed_y or any(v is None for v in sizes):
        return None
    x, y, z = sizes
    flat = (x <= printer.bed_x and y <= printer.bed_y) or (x <= printer.bed_y and y <= printer.bed_x)
    return bool(flat and (not printer.bed_z or z <= printer.bed_z))
