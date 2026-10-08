import os
from pathlib import Path

LIBRARY_PATH = Path(os.environ.get("LIBRARY_PATH", "/data"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config"))
CONFIG_PATH.mkdir(parents=True, exist_ok=True)

DB_PATH = CONFIG_PATH / "modelhub.db"
THUMB_DIR = CONFIG_PATH / "thumbnails"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# What counts as a model. Only these are scanned into the library, listed, and
# accepted by import; zips, notes, images, CAD sketches and the like in the
# library folder are left alone.
MODEL_EXTENSIONS = {".stl", ".3mf", ".obj", ".step", ".stp", ".fbx"}
SUPPORTED_EXTENSIONS = MODEL_EXTENSIONS   # older name, kept for code that still imports it
# Archives are accepted by import only: the model files inside are extracted
# and imported, everything else in the archive is ignored.
ARCHIVE_EXTENSIONS = {".zip"}
MESH_EXTENSIONS = {".stl", ".3mf", ".obj", ".fbx"}

# AI provider defaults, overridden at runtime via the Settings table
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
DEFAULT_OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "llava")
DEFAULT_OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "300"))
