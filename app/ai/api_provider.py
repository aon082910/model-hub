import base64
import json
from typing import List

import httpx

from app.ai import AIProvider


class APIProvider(AIProvider):
    """OpenAI-compatible API (OpenRouter, OpenAI, etc). User supplies their own key."""

    def __init__(self, api_key: str, api_base: str = "https://openrouter.ai/api/v1",
                 model: str = "openai/gpt-4o-mini", embed_model: str = "openai/text-embedding-3-small"):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.embed_model = embed_model

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def tag_image(self, image_path: str) -> dict:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Look at this 3D printable model render. Reply ONLY with JSON: "
                            '{"tags": ["...", "..."], "description": "one sentence"}.'
                        )},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
        resp = httpx.post(f"{self.api_base}/chat/completions", headers=self._headers(),
                           json=payload, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"tags": [], "description": content.strip()[:200]}
        return {
            "tags": [t.lower().strip() for t in data.get("tags", []) if t],
            "description": data.get("description", ""),
        }

    def embed_text(self, text: str) -> List[float]:
        resp = httpx.post(f"{self.api_base}/embeddings", headers=self._headers(),
                           json={"model": self.embed_model, "input": text}, timeout=60)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
