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
    cover_model_id: Optional[int] = None    # the model whose picture stands for the collection
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
    source_category: Optional[str] = None  # the site's category for the listing
    source_images: Optional[str] = None    # JSON list of file names in CONFIG_PATH/source_images/<model id>/
    source_synced_at: Optional[datetime] = None
    source_filaments: Optional[str] = None  # JSON list: filament the listing suggests (MakerWorld)
    source_linked_by: Optional[str] = None  # manual, auto (matching job), extension
    source_fingerprint: Optional[str] = None  # JSON: a hash per part of the listing, to notice changes
    source_checked_at: Optional[datetime] = None
    source_change: Optional[str] = None       # JSON list of the parts that changed since (title, files...)
    source_changed_at: Optional[datetime] = None
    notes: Optional[str] = None             # your own notes on this model
    family_id: Optional[int] = Field(default=None, index=True)   # versions of the same model share a family
    version_label: Optional[str] = None                          # v1, v2, final...
    print_settings: Optional[str] = None    # JSON: what worked (material, layer height, infill, supports, temperatures...)

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
    cost: Optional[float] = None   # what the spool cost; with spool_weight_g it gives the price per gram
    external_id: Optional[str] = None   # the same spool elsewhere, like "spoolman:12"
    opened_at: Optional[datetime] = None    # when the spool was opened, for the drying reminder
    dried_at: Optional[datetime] = None     # when it was last dried


class QueueItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="model3d.id")
    position: int = 0
    status: str = "queued"  # queued, printing, done, failed
    printer_id: Optional[int] = None   # the printer this job is meant for
    filament_id: Optional[int] = Field(default=None, foreign_key="filament.id")
    notes: Optional[str] = None
    estimated_grams: Optional[float] = None
    estimated_minutes: Optional[float] = None
    actual_minutes: Optional[float] = None     # what the printer reported when it finished
    estimate_basis: Optional[str] = None       # manual, history, adjusted, estimate: where estimated_minutes came from
    planned_date: Optional[str] = None         # YYYY-MM-DD, for the calendar
    slot: Optional[int] = None                 # the spool slot of printer_id the job is printed from
    order_id: Optional[int] = Field(default=None, index=True)       # the order this print is for
    order_item_id: Optional[int] = None
    uses: Optional[str] = None                 # JSON [{slot|filament_id, grams}]: a multicolour job's spools (else filament_id/slot above)
    priority: Optional[int] = None             # -1 low, 0 or empty normal, 1 high
    held: Optional[bool] = None                # waiting for someone to look at it: not suggested, assigned or sent until released
    printer_tag: Optional[str] = None          # only a printer with this tag may print it
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


class WishlistItem(SQLModel, table=True):
    """A listing on an online site that you want to keep for later, with a note.
    Downloading it (when the site allows) adds it to the library."""
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    source_id: str = Field(index=True)
    title: str
    thumbnail: Optional[str] = None
    url: Optional[str] = None
    designer: Optional[str] = None
    license: Optional[str] = None
    note: Optional[str] = None
    priority: int = 1               # 0 low, 1 normal, 2 high (rows from before this column read as normal)
    status: str = "wanted"          # wanted, got (have it elsewhere), skip (changed my mind)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FollowedDesigner(SQLModel, table=True):
    """A designer on a site whose new uploads you want to hear about. handle is what the site
    needs to list their work (Printables: the user id; Sketchfab and Thingiverse: the username)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    handle: str = Field(index=True)
    name: Optional[str] = None
    known_ids: Optional[str] = None          # JSON list: listings already seen (newest first)
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DesignerUpload(SQLModel, table=True):
    """A new listing from a followed designer, waiting for you to look at it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    designer_id: int = Field(foreign_key="followeddesigner.id", index=True)
    provider: str
    source_id: str
    title: str
    thumbnail: Optional[str] = None
    url: Optional[str] = None
    license: Optional[str] = None
    seen: bool = False
    found_at: datetime = Field(default_factory=datetime.utcnow)


class PrintLog(SQLModel, table=True):
    """One print of a model: when, with which spool and how much, how it turned out.
    deducted_g is what was actually taken from the spool, so deleting the entry
    puts back exactly that."""
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="model3d.id", index=True)
    printed_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    filament_id: Optional[int] = Field(default=None, foreign_key="filament.id")
    grams: Optional[float] = None
    deducted_g: float = 0
    minutes: Optional[float] = None
    rating: Optional[int] = None            # 1-5
    notes: Optional[str] = None
    source: str = "manual"                  # manual, queue
    queue_item_id: Optional[int] = None
    outcome: Optional[str] = None           # failed, or empty for a print that worked
    failure_reason: Optional[str] = None    # a key of app.print_outcomes.REASONS
    printer_id: Optional[int] = None        # the printer that made it, when a printer reported it
    uses: Optional[str] = None              # JSON [{filament_id, grams}] when several spools were used
    timelapse_url: Optional[str] = None     # a link to its time-lapse video
    energy_kwh: Optional[float] = None      # what the printer's smart plug measured for this print
    measured: bool = False                  # minutes is a real time (a printer reported it, or you typed it), not an estimate
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Printer(SQLModel, table=True):
    """A printer on your network that Model Hub can show the status of and send G-code to.
    api_key is never returned by the API and is left out of backups."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str                       # moonraker, octoprint
    url: str
    api_key: Optional[str] = None
    serial: Optional[str] = None         # a Bambu Lab printer's serial number (its LAN access code is api_key)
    bed_x: Optional[float] = None        # its print bed in mm, for the "will it fit" checks
    bed_y: Optional[float] = None
    bed_z: Optional[float] = None
    plug_kind: Optional[str] = None      # a smart plug that measures its power: tasmota or shelly
    plug_host: Optional[str] = None      # its address on your network
    tags: Optional[str] = None           # comma-separated labels (a room, a group, a nozzle size) a queue entry can ask for
    pause_on_failure: Optional[bool] = None   # with watching on: pause the print after three bad looks in a row
    watch_failures: Optional[bool] = None   # look at its camera now and then for a failing print (needs a camera and a local vision model)
    slot_count: Optional[int] = None     # how many spool slots it has (an AMS, an MMU, a toolchanger...); none or 0 = one spool
    snapshot_url: Optional[str] = None   # the printer camera's still-picture address, for a photo when a print finishes
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Order(SQLModel, table=True):
    """Prints made for someone: who, what, how many, by when, and what it brings in."""
    id: Optional[int] = Field(default=None, primary_key=True)
    customer: str
    contact: Optional[str] = None
    status: str = "quote"                  # quote, accepted, printing, ready, delivered, cancelled
    due_date: Optional[str] = None         # YYYY-MM-DD
    notes: Optional[str] = None
    paid: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(index=True)
    model_id: int
    quantity: int = 1
    unit_price: Optional[float] = None     # empty: the price the cost calculator suggests


class MaintenanceTask(SQLModel, table=True):
    """Something a printer needs now and then (oil the rails, change the nozzle): due after so many print hours and/or days."""
    id: Optional[int] = Field(default=None, primary_key=True)
    printer_id: int = Field(index=True)
    name: str
    every_hours: Optional[float] = None           # of printing on that printer
    every_days: Optional[int] = None
    last_done_at: datetime = Field(default_factory=datetime.utcnow)
    last_done_hours: float = 0                    # the printer's print hours when it was last done
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SpoolSlot(SQLModel, table=True):
    """Which spool sits in which slot of a printer (slot 1..slot_count). A spool is in at most one slot."""
    id: Optional[int] = Field(default=None, primary_key=True)
    printer_id: int = Field(index=True)
    slot: int
    filament_id: Optional[int] = Field(default=None, index=True)
    label: Optional[str] = None
    loaded_at: datetime = Field(default_factory=datetime.utcnow)


class PrinterJob(SQLModel, table=True):
    """A file Model Hub sent to a printer, so that when the printer reports it finished the
    print can be logged against the right model."""
    id: Optional[int] = Field(default=None, primary_key=True)
    printer_id: int = Field(index=True)
    filename: str
    model_id: Optional[int] = None
    started: bool = False
    print_file_id: Optional[int] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    outcome: Optional[str] = None            # done, stopped


class SavedSearch(SQLModel, table=True):
    """A set of library filters kept under a name."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    params: str = "{}"               # JSON
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FilamentPrice(SQLModel, table=True):
    """What a spool cost, noted whenever its price is set or changed (so you can see prices move)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    filament_id: int = Field(index=True)
    cost: float
    spool_weight_g: float = 1000
    at: datetime = Field(default_factory=datetime.utcnow)


class ModelFamily(SQLModel, table=True):
    """Several files that are versions of one model (v1, v2, a repaired copy...)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShareLink(SQLModel, table=True):
    """A secret link that shows one model or project, read-only, to someone without a login."""
    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    kind: str                                  # model, project
    target_id: int
    allow_downloads: bool = False
    show_costs: bool = False
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None


class ApiToken(SQLModel, table=True):
    """A long-lived key for scripts and other programs. Only a hash is stored; the key is shown once."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    token_hash: str = Field(index=True, unique=True)
    prefix: str                                  # the first characters, so you can tell tokens apart
    scope: str = "read"                          # read, write, import
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class PrintFile(SQLModel, table=True):
    """A sliced file (G-code, or a sliced 3MF) kept with a model, with what the slicer said about it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(index=True)
    filename: str                           # the name it was uploaded with
    stored_name: str                        # where it lives in CONFIG_PATH/print_files
    kind: str                               # gcode, gco, g, bgcode, 3mf
    size_bytes: int = 0
    sha256: str = Field(default="", index=True)
    slicer: Optional[str] = None
    est_minutes: Optional[float] = None
    est_grams: Optional[float] = None
    filament_type: Optional[str] = None
    layer_height: Optional[str] = None
    filaments: Optional[str] = None       # JSON [{index, type, color, grams}]: each filament the file uses
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ActivityLog(SQLModel, table=True):
    """Who changed what, and (for bulk edits) how to take it back."""
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=datetime.utcnow, index=True)
    actor: str = "system"
    action: str = Field(index=True)
    summary: str
    undo_json: Optional[str] = None
    undone: bool = False
    undone_at: Optional[datetime] = None
    undone_by: Optional[str] = None


class FilingRule(SQLModel, table=True):
    """When a linked listing's category, tags, title or designer contains some text, add a tag or file the model in a collection."""
    id: Optional[int] = Field(default=None, primary_key=True)
    field: str                                  # category, tag, title, designer
    match: str                                  # text to look for (not case sensitive)
    action: str                                 # add_tag, add_collection
    value: str                                  # the tag, or the collection's name (created when missing)
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppUser(SQLModel, table=True):
    """A login besides the first (admin) account, which lives in the settings. role is
    "member" (uses everything except settings, backups and user management) or
    "viewer" (read-only)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = "member"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppSettings(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    value: str


class Favorite(SQLModel, table=True):
    """A model someone starred. Each login has its own stars."""
    id: Optional[int] = Field(default=None, primary_key=True)
    owner: str = Field(index=True)
    model_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
