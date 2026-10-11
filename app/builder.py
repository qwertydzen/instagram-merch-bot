"""Product-builder service: browse Arcade categories, configure options, build.

This is the engine behind the mini-app (not the chat). It honours Arcade's two
"worlds":

  print   — pattern + products.create with a COMPLETE selection from
            categories/get. The builder lets the user pick option values, then
            resolves a manufacture-able full selection before building.
  jewelry — one patterns.create call (categoryId) designs the piece and builds
            its product; products.create refuses it. Options come from the prompt.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("builder")


class BuildError(ValueError):
    """Raised when a selection can't be manufactured or a job fails."""


def get_options(arcade, category_id: str, selections: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """categories/get — options with availability + a resolved full selection."""
    return arcade.get_category(category_id, selections or {})


def _fail_from_job(job: Dict[str, Any], what: str) -> BuildError:
    err = (job.get("error") or {}).get("message", "unknown error")
    return BuildError(f"{what} failed: {err}")


def generate_product(
    arcade,
    categoryId: str,
    prompt: str,
    selections: Optional[Dict[str, str]] = None,
    modelId: Optional[str] = None,
    colors: Optional[List[str]] = None,
    quantity: int = 1,
    reference_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a product from a category + prompt (+ optional option picks).

    `reference_ids` are Arcade reference images (e.g. the Instagram post's
    picture) that guide the pattern generation.
    Returns {"product": ..., "category": ..., "patternId": ...}.
    """
    selections = selections or {}
    if not prompt or not prompt.strip():
        raise BuildError("A design prompt is required.")

    cat = arcade.get_category(categoryId, selections)
    world = cat.get("world", "print")
    complete = cat.get("selections") or {}

    if world == "jewelry":
        # One call designs the piece and builds its product.
        job = arcade.create_pattern(prompt, category_id=categoryId)
        job = arcade.wait_job(job["jobId"])
        if job.get("status") != "succeeded" or not job.get("result"):
            raise _fail_from_job(job, "design job")
        product_id = job["result"].get("productId")
        pattern_id = job["result"].get("patternId")
        if not product_id:
            raise BuildError("design job succeeded but produced no product")
    else:
        if not cat.get("canManufacture", True):
            # surface the conflicting options so the UI can guide the user back
            conflicts = []
            for opt in cat.get("options", []):
                key = opt.get("key")
                if key in selections and selections[key] not in {
                    v["value"] for v in opt.get("values", []) if v.get("available")
                }:
                    conflicts.append(key)
            raise BuildError(
                "That combination can't be made. Try changing: "
                + (", ".join(conflicts) if conflicts else "your options")
            )
        # 1) generate just the pattern (guide it with the reference, if any)
        pjob = arcade.create_pattern(
            prompt, model_id=modelId, colors=colors or [], reference_ids=reference_ids or []
        )
        pjob = arcade.wait_job(pjob["jobId"])
        if pjob.get("status") != "succeeded" or not pjob.get("result"):
            raise _fail_from_job(pjob, "pattern job")
        pattern_id = pjob["result"].get("patternId")
        # 2) build the product from the complete selection
        prjob = arcade.create_product(pattern_id, categoryId, complete)
        prjob = arcade.wait_job(prjob["jobId"])
        if prjob.get("status") != "succeeded" or not prjob.get("result"):
            raise _fail_from_job(prjob, "product job")
        product_id = prjob["result"].get("productId")

    product = arcade.get_product(product_id, quantity)
    return {
        "product": product,
        "patternId": pattern_id,
        "category": {"categoryId": cat.get("categoryId"), "name": cat.get("name"), "world": world},
    }


def create_checkout(arcade, product_id: str, quantity: int = 1) -> Dict[str, Any]:
    # dropShip is decided by the client: true only when quantity is 1.
    return arcade.create_checkout([{"productId": product_id, "quantity": quantity}])
