"""Mock Arcade client — lets the whole flow run with no API key.

Returns deterministic fake patterns/products/checkouts shaped exactly like the
real API responses, so the bot, webhook and reply formatting can be exercised
offline. Swap to ArcadeClient by setting ARCADE_API_KEY.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .arcade import new_request_id

# A few plausible print categories, mirroring categories/list shape.
_MOCK_CATEGORIES: List[Dict[str, Any]] = [
    {"categoryId": "mock-cat-tote", "name": "Canvas Tote", "imageUrl": None, "world": "print"},
    {"categoryId": "mock-cat-tee", "name": "T-Shirt", "imageUrl": None, "world": "print"},
    {"categoryId": "mock-cat-mug", "name": "Ceramic Mug", "imageUrl": None, "world": "print"},
    {"categoryId": "mock-cat-poster", "name": "Art Poster", "imageUrl": None, "world": "print"},
    {"categoryId": "mock-cat-napkin", "name": "Dinner Napkin", "imageUrl": None, "world": "print"},
    {"categoryId": "mock-cat-ring", "name": "Dainty Ring", "imageUrl": None, "world": "jewelry"},
]


# option templates per category name: key -> [values] (first is default)
_MOCK_OPTIONS: Dict[str, Dict[str, List[str]]] = {
    "Canvas Tote": {"material": ["canvas"], "size": ["one-size"], "color": ["natural", "black"]},
    "T-Shirt": {"size": ["s", "m", "l", "xl"], "color": ["white", "black"]},
    "Ceramic Mug": {"size": ["11oz", "15oz"], "color": ["white"]},
    "Art Poster": {"size": ["12x18", "18x24"], "finish": ["matte", "glossy"]},
    "Dinner Napkin": {"size": ["20x20"], "fabric": ["cotton", "linen"]},
    "Dainty Ring": {"metal": ["gold", "silver"], "stone": ["none", "lab-diamond"]},
}


def _seed(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


class MockArcadeClient:
    """Same interface as ArcadeClient, no network."""

    def __init__(self, *_, **__):
        self._products: Dict[str, Dict[str, Any]] = {}
        self._patterns: Dict[str, Dict[str, Any]] = {}
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def list_categories(self) -> List[Dict[str, Any]]:
        return list(_MOCK_CATEGORIES)

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"modelId": "mock-model-flux", "name": "Mock Flux"}]

    def get_category(self, category_id: str, selections: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        selections = selections or {}
        cat = next((c for c in _MOCK_CATEGORIES if c["categoryId"] == category_id), None)
        name = cat["name"] if cat else "Canvas Tote"
        spec = _MOCK_OPTIONS.get(name, {"color": ["natural"]})
        options = []
        complete: Dict[str, str] = {}
        for key, vals in spec.items():
            chosen = selections.get(key)
            options.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "values": [
                    {"value": v, "label": v, "isDefault": i == 0, "available": True}
                    for i, v in enumerate(vals)
                ],
            })
            complete[key] = chosen if chosen in vals else vals[0]
        return {
            "categoryId": category_id,
            "name": name,
            "options": options,
            "selections": complete,
            "canManufacture": True,
            "world": cat["world"] if cat else "print",
        }

    def create_pattern(self, prompt: str, category_id: Optional[str] = None, **_kw) -> Dict[str, Any]:
        job_id = "mock-job-" + _seed(prompt, category_id or "")
        result: Dict[str, Any] = {"patternId": "mock-pat-" + _seed(prompt, "pat")}
        if category_id:
            product_id = "mock-prd-" + _seed(prompt, category_id)
            result["productId"] = product_id
            self._products[product_id] = self._make_product(product_id, prompt, category_id)
        self._jobs[job_id] = {
            "jobId": job_id,
            "kind": "pattern",
            "status": "succeeded",
            "result": result,
            "error": None,
        }
        return self._jobs[job_id]

    def create_product(self, pattern_id: str, category_id: str, selections, request_id: Optional[str] = None, fit=None) -> Dict[str, Any]:
        product_id = "mock-prd-" + _seed(pattern_id, category_id, str(sorted((selections or {}).items())))
        self._products[product_id] = self._make_product(product_id, "custom design", category_id, selections)
        job_id = "mock-job-prd-" + _seed(product_id)
        self._jobs[job_id] = {
            "jobId": job_id, "kind": "product", "status": "succeeded",
            "result": {"productId": product_id}, "error": None,
        }
        return self._jobs[job_id]

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return self._jobs.get(
            job_id,
            {"jobId": job_id, "kind": "pattern", "status": "failed", "result": None,
             "error": {"message": "unknown mock job"}},
        )

    def wait_job(self, job_id: str, **_kw) -> Dict[str, Any]:
        return self.get_job(job_id)

    def get_product(self, product_id: str, quantity: int = 1) -> Dict[str, Any]:
        prod = self._products.get(product_id)
        if prod is None:
            prod = self._make_product(product_id, "generated design", "mock-cat-tote")
        out = dict(prod)
        out["unitPrice"] = {"amount": "28.00", "currency": "USD"}
        return out

    def list_products(self) -> List[Dict[str, Any]]:
        return list(self._products.values())

    def create_checkout(self, lines, dropship: bool = True, request_id: Optional[str] = None) -> Dict[str, Any]:
        cid = _seed(str(lines))
        return {
            "checkoutId": "mock-checkout-" + cid,
            "checkoutUrl": f"https://checkout.arcade.ai/checkouts/mock-{cid}",
        }

    # -- references --------------------------------------------------------
    def create_reference(self, image, media_type=None, request_id=None) -> Dict[str, Any]:
        rid = "mock-ref-" + _seed(str(len(image or b"")), media_type or "")
        return {
            "referenceId": rid,
            "imageUrl": f"https://picsum.photos/seed/{rid}/600/600",
            "thumbnailUrl": f"https://picsum.photos/seed/{rid}/200/200",
            "width": 512, "height": 512, "mediaType": media_type or "image/png",
        }

    def get_reference(self, reference_id: str) -> Dict[str, Any]:
        return {
            "referenceId": reference_id,
            "imageUrl": f"https://picsum.photos/seed/{reference_id}/600/600",
            "thumbnailUrl": f"https://picsum.photos/seed/{reference_id}/200/200",
            "width": 512, "height": 512, "mediaType": "image/png",
        }

    def build_product_from_prompt(self, prompt: str, category_id: str, quantity: int = 1) -> Dict[str, Any]:
        job = self.create_pattern(prompt, category_id=category_id)
        job = self.wait_job(job["jobId"])
        product_id = job["result"]["productId"]
        return {
            "product": self.get_product(product_id, quantity),
            "checkout": self.create_checkout([{"productId": product_id, "quantity": quantity}]),
        }

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _make_product(product_id: str, prompt: str, category_id: str, selections: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        seed = _seed(product_id, prompt)
        selections = selections or {"size": "one-size", "color": "natural"}
        label = " / ".join(str(v) for v in selections.values())
        return {
            "productId": product_id,
            "title": f'"{prompt[:40]}" custom piece',
            "categoryId": category_id,
            "maker": {"makerId": "mock-maker", "name": "Arcade Mock Maker"},
            "selections": selections,
            "selectionLabel": label,
            "imageUrl": f"https://picsum.photos/seed/{seed}/600/600",
            "createdAt": "2026-10-11T00:00:00Z",
            "images": [{"url": f"https://picsum.photos/seed/{seed}/900/900"},
                       {"url": f"https://picsum.photos/seed/{seed}b/900/900"}],
            "description": f"Mock product generated from prompt: {prompt}",
            "careInstructions": "Machine wash cold.",
            "volumeDiscounts": None,
            "patternId": "mock-pat-" + _seed(prompt, "pat"),
            "unitPrice": {"amount": "28.00", "currency": "USD"},
        }
