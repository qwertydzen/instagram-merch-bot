"""Client for the Arcade API (platform.arcade.ai/v1).

Contract notes (from docs.arcade.ai):
  * Every request is POST + JSON body, authed with `Authorization: Bearer <key>`.
  * A `User-Agent` naming your integration is required; a missing/library-default
    one is rejected at the EDGE with a non-JSON 403 before reaching the API.
  * Work-starting calls take a `requestId` (idempotency key, <=128 chars): a
    retry with the same `requestId` + body returns the original result.
  * Errors are JSON `{code, status, message}`. Retry 5xx with backoff; on 429
    wait `Retry-After`; a 409 for a still-starting request may be retried
    unchanged after a short delay, but a body-mismatch 409 must be fixed.

Docs: https://docs.arcade.ai
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

log = logging.getLogger("arcade")

PROMPT_MAX = 1000          # patterns/create prompt maxLength
MAX_LINES = 50             # checkouts/create maxItems
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ArcadeError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Any = None,
                 code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.code = code


def new_request_id(prefix: str = "req") -> str:
    """Idempotency key: unique per distinct request (<=128 chars)."""
    return f"{prefix}-{uuid.uuid4().hex}"[:128]


def dropship_for(lines: List[Dict[str, Any]]) -> bool:
    """dropShip must be true when any line has quantity 1 (unless you have a
    subscription); otherwise omit it rather than paying for the extra service."""
    return any(int(line.get("quantity", 1)) == 1 for line in lines)


class ArcadeClient:
    """Real Arcade client. Requires ARCADE_API_KEY."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ):
        self.api_key = (api_key or Settings.ARCADE_API_KEY).strip()
        self.base_url = (base_url or Settings.ARCADE_BASE_URL).rstrip("/")
        self.user_agent = (user_agent or Settings.ARCADE_USER_AGENT).strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    # -- low level ---------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _parse_error(self, resp: httpx.Response) -> ArcadeError:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and ("message" in payload or "code" in payload):
            code = payload.get("code")
            msg = payload.get("message") or resp.text
            label = f"{code} " if code else ""
            return ArcadeError(f"{label}{msg}".strip(), status=resp.status_code,
                               body=payload, code=code)
        # Non-JSON body: the edge rejected us before the API saw the request.
        text = (payload if isinstance(payload, str) else resp.text) or ""
        hint = ""
        if resp.status_code == 403:
            hint = (" — usually a missing or library-default User-Agent; set "
                    "ARCADE_USER_AGENT to '<app>/<version> (<contact>)'")
        elif resp.status_code == 401:
            hint = " — API key missing, invalid or revoked"
        return ArcadeError(f"HTTP {resp.status_code}: {text.strip()[:300]}{hint}",
                           status=resp.status_code, body=payload)

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise ArcadeError("ARCADE_API_KEY is not set")
        if not self.user_agent:
            raise ArcadeError("ARCADE_USER_AGENT is not set (Arcade's edge rejects blank agents)")

        url = f"{self.base_url}{path}"
        last: Optional[ArcadeError] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout)
            except httpx.HTTPError as exc:
                last = ArcadeError(f"network error calling {path}: {exc}")
                if attempt < self.max_retries:
                    self._sleep(2 ** attempt)
                    continue
                raise last

            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            err = self._parse_error(resp)

            # 429 -> honour Retry-After; 5xx -> exponential backoff; both are
            # safe to retry with the same requestId (idempotent).
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = 2 ** attempt
                if resp.status_code == 429:
                    try:
                        wait = max(wait, float(resp.headers.get("Retry-After", wait)))
                    except Exception:
                        pass
                log.warning("%s -> HTTP %s, retrying in %ss (attempt %s/%s)",
                            path, resp.status_code, wait, attempt + 1, self.max_retries)
                last = err
                self._sleep(wait)
                continue

            raise err
        raise last or ArcadeError(f"{path} failed")

    # -- endpoints ---------------------------------------------------------
    def list_categories(self) -> List[Dict[str, Any]]:
        return self._post("/categories/list", {}).get("items", []) or []

    def list_models(self) -> List[Dict[str, Any]]:
        return self._post("/models/list", {}).get("items", []) or []

    def get_category(self, category_id: str, selections: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return self._post(
            "/categories/get", {"categoryId": category_id, "selections": selections or {}}
        )

    def create_pattern(
        self,
        prompt: str,
        category_id: Optional[str] = None,
        request_id: Optional[str] = None,
        model_id: Optional[str] = None,
        colors: Optional[List[str]] = None,
        reference_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Start a pattern job. With category_id it also builds the product."""
        body: Dict[str, Any] = {
            "requestId": request_id or new_request_id("pat"),
            "prompt": (prompt or "")[:PROMPT_MAX],
        }
        if category_id:
            body["categoryId"] = category_id
        if model_id:
            body["modelId"] = model_id
        if colors:
            body["colors"] = colors[:10]          # maxItems 10
        if reference_ids:
            body["referenceIds"] = reference_ids[:3]  # maxItems 3
        return self._post("/patterns/create", body)

    def create_product(
        self,
        pattern_id: str,
        category_id: str,
        selections: Dict[str, str],
        request_id: Optional[str] = None,
        fit: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "requestId": request_id or new_request_id("prd"),
            "patternId": pattern_id,
            "categoryId": category_id,
            "selections": selections,  # must be the COMPLETE selection from categories/get
        }
        if fit:
            body["fit"] = fit
        return self._post("/products/create", body)

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return self._post("/jobs/get", {"jobId": job_id})

    # -- references (images that guide generation) -------------------------
    def create_reference(
        self,
        image: bytes,
        media_type: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload an image and register it as a reference for patterns/create.

        createUpload -> PUT the bytes -> references/create -> referenceId.
        """
        from .media import detect_image_type

        mt = media_type or detect_image_type(image)
        if mt not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise ArcadeError("unsupported reference image (png / jpeg / webp / gif)")
        if not image:
            raise ArcadeError("empty reference image")
        if len(image) > 10 * 1024 * 1024:
            raise ArcadeError("reference image exceeds 10 MiB")

        upload = self._post(
            "/references/createUpload",
            {
                "requestId": request_id or new_request_id("refup"),
                "mediaType": mt,
                "byteLength": len(image),
            },
        )
        put_headers = dict(upload.get("requiredHeaders") or {})
        put_headers.setdefault("Content-Type", mt)
        try:
            put = httpx.request(
                upload.get("method", "PUT"),
                upload["uploadUrl"],
                content=image,
                headers=put_headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ArcadeError(f"reference upload failed: {exc}") from exc
        if put.status_code >= 400:
            raise ArcadeError(f"reference upload PUT -> HTTP {put.status_code}: {put.text[:200]}")

        ref = self._post(
            "/references/create",
            {"requestId": new_request_id("ref"), "uploadId": upload["uploadId"]},
        )
        log.info("reference created: %s (%sx%s)", ref.get("referenceId"), ref.get("width"), ref.get("height"))
        return ref

    def get_reference(self, reference_id: str) -> Dict[str, Any]:
        return self._post("/references/get", {"referenceId": reference_id})

    def list_references(self) -> List[Dict[str, Any]]:
        return self._post("/references/list", {}).get("items", []) or []

    def wait_job(
        self,
        job_id: str,
        interval: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Poll jobs/get until succeeded/failed. A succeeded job may still have
        a null result — callers must handle that."""
        interval = interval if interval is not None else Settings.JOB_POLL_INTERVAL
        timeout = timeout if timeout is not None else Settings.JOB_TIMEOUT
        deadline = time.time() + timeout
        while True:
            job = self.get_job(job_id)
            status = job.get("status")
            if status in ("succeeded", "failed"):
                return job
            if time.time() >= deadline:
                raise ArcadeError(f"job {job_id} still {status} after {timeout}s")
            time.sleep(interval)

    def get_product(self, product_id: str, quantity: int = 1, pricing_retries: int = 2) -> Dict[str, Any]:
        """products/get. unitPrice can be null while pricing is unavailable, so
        read again rather than showing a blank price."""
        for attempt in range(pricing_retries + 1):
            product = self._post("/products/get", {"productId": product_id, "quantity": quantity})
            if product.get("unitPrice") is not None or attempt == pricing_retries:
                return product
            time.sleep(1.5)
        return product

    def list_products(self, limit: int = 24, cursor: Optional[str] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
        if cursor:
            body["cursor"] = cursor
        return self._post("/products/list", body).get("items", []) or []

    def create_checkout(
        self,
        lines: List[Dict[str, Any]],
        dropship: Optional[bool] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """lines: [{"productId": str, "quantity": int}].

        `dropShip` is auto-set true only when a line has quantity 1 (otherwise
        the request 412s without a subscription; forcing it on adds a paid
        service you don't need).
        """
        if not lines:
            raise ArcadeError("checkout needs at least one line")
        if len(lines) > MAX_LINES:
            raise ArcadeError(f"checkout supports at most {MAX_LINES} lines")
        body: Dict[str, Any] = {
            "requestId": request_id or new_request_id("chk"),
            "lines": lines,
        }
        want_dropship = dropship_for(lines) if dropship is None else dropship
        if want_dropship:
            body["dropShip"] = True
        return self._post("/checkouts/create", body)

    # -- convenience -------------------------------------------------------
    def build_product_from_prompt(
        self,
        prompt: str,
        category_id: str,
        quantity: int = 1,
    ) -> Dict[str, Any]:
        """prompt -> pattern -> product -> checkout. Returns a reply-ready dict.

        Uses the one-shot path (patterns/create WITH categoryId) so Arcade picks
        the product options for a print category.
        """
        job = self.wait_job(self.create_pattern(prompt, category_id=category_id)["jobId"])
        if job.get("status") != "succeeded" or not job.get("result"):
            err = (job.get("error") or {}).get("message", "unknown error")
            raise ArcadeError(f"pattern job failed: {err}", body=job)

        product_id = job["result"].get("productId")
        if not product_id:
            raise ArcadeError("job succeeded but returned no productId", body=job)

        product = self.get_product(product_id, quantity=quantity)
        checkout = self.create_checkout([{"productId": product_id, "quantity": quantity}])
        return {"product": product, "checkout": checkout}
