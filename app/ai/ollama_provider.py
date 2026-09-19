import base64
import json
from typing import List

from app.config import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_VISION_MODEL, DEFAULT_OLLAMA_EMBED_MODEL
from app.ai import AIProvider
from app.ai.http_utils import post_json_bounded


class OllamaProvider(AIProvider):
    """Runs entirely against a local (or LAN) Ollama instance. No data leaves the network."""

    def __init__(self, host: str = None, vision_model: str = None, embed_model: str = None):
        self.host = (host or DEFAULT_OLLAMA_HOST).rstrip("/")
        self.vision_model = vision_model or DEFAULT_OLLAMA_VISION_MODEL
        self.embed_model = embed_model or DEFAULT_OLLAMA_EMBED_MODEL

    def tag_image(self, image_path: str) -> dict:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        prompt = (
            "Look at this 3D printable model render. Reply ONLY with JSON: "
            '{"tags": ["...", "..."], "description": "one sentence"}. '
            "Tags should be short, lowercase, e.g. object type, category, use-case."
        )
        body = post_json_bounded(
            f"{self.host}/api/generate",
            {
                "model": self.vision_model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
                "format": "json",
                # We only ever want a couple of short tags plus one sentence.
                # Without a cap, a model that struggles to close out the
                # forced JSON grammar (a known failure mode especially on
                # smaller/quantized vision models) can keep generating far
                # past anything usable -- bounding it here caps how long one
                # model can block the rest of the batch. post_json_bounded's
                # own byte cap is the real backstop for memory: it protects
                # even if a model or a non-Ollama-compliant endpoint ignores
                # this option entirely.
                "options": {"num_predict": 200},
            },
            timeout=120,
        )
        raw = body.get("response", "{}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"tags": [], "description": raw.strip()[:200]}
        return {
            "tags": [t.lower().strip() for t in data.get("tags", []) if t],
            "description": data.get("description", ""),
        }

    def embed_text(self, text: str) -> List[float]:
        body = post_json_bounded(
            f"{self.host}/api/embeddings",
            {"model": self.embed_model, "prompt": text},
            timeout=60,
        )
        return body.get("embedding", [])
