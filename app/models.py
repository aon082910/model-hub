from datetime import datetime
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship


class ModelTagLink(SQLModel, table=True):
    model_id: Optional[int] = Field(default=None, foreign_key="model3d.id", primary_key=True)
    tag_id: Optional[int] = Field(default=None, foreign_key="tag.id", primary_key=True)


class ModelCollectionLink(SQLModel, table=True):
    model_id: Optional[int] = Field(default=None, foreign_key="model3d.id", primary_key=True)
    collection_id: Optional[int] = Field(default=None, foreign_key="collection.id", primary_key=True)


class ProjectModelLink(SQLModel, table=True):
    project_id: Optional[int] = Field(default=None, foreign_key="project.id", primary_key=True)
    model_id: Optional[int] = Field(default=None, foreign_key="model3d.id", primary_key=True)


class Tag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    ai_generated: bool = False
    models: List["Model3D"] = Relationship(back_populates="tags", link_model=ModelTagLink)


class Collection(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    parent_id: Optional[int] = Field(default=None, foreign_key="collection.id")
    models: List["Model3D"] = Relationship(back_populates="collections", link_model=ModelCollectionLink)


class SmartCollection(SQLModel, table=True):
    """Rule-based auto-filing collection. rule_json example:
    {"match": "all", "conditions": [{"field": "tag", "op": "contains", "value": "vase"}]}
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    rule_json: str


class Model3D(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    path: str = Field(index=True, unique=True)
    extension: str
    size_bytes: int
    content_hash: str = Field(index=True)
    geometry_hash: Optional[str] = Field(default=None, index=True)
    thumbnail_path: Optional[str] = None
    vertex_count: Optional[int] = None
    face_count: Optional[int] = None
    bbox_x: Optional[float] = None
    bbox_y: Optional[float] = None
    bbox_z: Optional[float] = None
    volume_mm3: Optional[float] = None
    is_watertight: Optional[bool] = None

    # roadmap: metadata / provenance tracking
    source_url: Optional[str] = None
    designer: Optional[str] = None
    license: Optional[str] = None
    # matched listing on a model site (see app/sources.py)
    source_provider: Optional[str] = None  # printables, makerworld
    source_id: Optional[str] = None
    source_title: Optional[str] = None
    source_description: Optional[str] = None
    source_tags: Optional[str] = None      # JSON list
    source_images: Optional[str] = None    # JSON list of file names in CONFIG_PATH/source_images/<model id>/
    source_synced_at: Optional[datetime] = None
    source_filaments: Optional[str] = None  # JSON list: filament the listing suggests (MakerWorld)
    source_linked_by: Optional[str] = None  # manual, auto (matching job), extension
    notes: Optional[str] = None             # your own notes on this model

    is_duplicate_of: Optional[int] = Field(default=None, foreign_key="model3d.id")

    ai_tagged: bool = False
    ai_description: Optional[str] = None
    embedding: Optional[bytes] = None  # float32 vector, packed

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_scanned_at: datetime = Field(default_factory=datetime.utcnow)

    tags: List[Tag] = Relationship(back_populates="models", link_model=ModelTagLink)
    collections: List[Collection] = Relationship(back_populates="models", link_model=ModelCollectionLink)


class Filament(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    material: str  # PLA, PETG, ABS, ...
    brand: Optional[str] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    spool_weight_g: float = 1000
    remaining_g: float = 1000
    purchase_url: Optional[str] = None
    notes: Optional[str] = None


class QueueItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="model3d.id")
    position: int = 0
    status: str = "queued"  # queued, printing, done, failed
    filament_id: Optional[int] = Field(default=None, foreign_key="filament.id")
    notes: Optional[str] = None
    estimated_grams: Optional[float] = None
    estimated_minutes: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Project(SQLModel, table=True):
    """A build that combines printed models with non-printed parts (electronics,
    hardware, supplies). The parts list lives in ProjectPart."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    status: str = "planning"  # planning, building, printed, done
    notes: Optional[str] = None
    # True once this project's filament has been subtracted from inventory
    # (set when it reaches printed/done, cleared when it's moved back).
    filament_deducted: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectModelFilament(SQLModel, table=True):
    """Filament planned for one model in a project: grams from one spool.
    A model printed in several colors/materials has several rows.
    deducted_g records what was actually taken from the spool so a revert
    restores exactly that (the spool's remaining_g is clamped at zero)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    model_id: int = Field(foreign_key="model3d.id")
    filament_id: int = Field(foreign_key="filament.id")
    grams: float = 0
    deducted_g: float = 0


class ProjectPart(SQLModel, table=True):
    """One line of a project's bill of materials. quantity is how many the
    project needs; quantity_owned is how many are already on hand."""
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    name: str
    category: str = "electronics"  # electronics, parts, supplies
    quantity: int = 1
    quantity_owned: int = 0
    unit_cost: Optional[float] = None
    purchase_url: Optional[str] = None
    notes: Optional[str] = None


class SourceMatchState(SQLModel, table=True):
    """Where a library model stands in the 'match my whole library' job."""
    model_id: Optional[int] = Field(default=None, foreign_key="model3d.id", primary_key=True)
    status: str = "candidates"     # candidates (awaiting review), none (nothing close found), skipped (you said no)
    query: Optional[str] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class SourceCandidate(SQLModel, table=True):
    """A possible listing for a model, found by the matching job, for you to accept."""
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="model3d.id", index=True)
    provider: str
    source_id: str
    title: str
    designer: Optional[str] = None
    license: Optional[str] = None
    thumbnail: Optional[str] = None
    url: Optional[str] = None
    score: float = 0


class InventoryItem(SQLModel, table=True):
    """Parts, electronics and supplies on hand, independent of any project."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    category: str = "electronics"  # electronics, parts, supplies
    quantity: int = 0
    min_quantity: int = 0          # flag as low stock at or below this (0 = never)
    location: Optional[str] = None
    unit_cost: Optional[float] = None
    purchase_url: Optional[str] = None
    notes: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AppSettings(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    value: str
