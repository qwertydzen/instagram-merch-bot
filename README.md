# Instagram Merch Bot

An Instagram **DM chatbot** that designs merch on demand with the
[Arcade API](https://docs.arcade.ai) and replies with the product image + a
checkout link. Message the bot *"a canvas tote with desert cacti"* and it
generates a pattern, builds the physical product, and hands back a buy link.

```
Instagram DM ──────▶ │                   │
                    │  /webhook  ─▶ intent ─▶ Arcade (pattern→product→checkout) ─▶ reply
Instagram COMMENT ─▶ │  (private reply DM to the commenter — no prior message needed)
```

## Layout

| Path | Role |
| --- | --- |
| `app/main.py` | FastAPI app: webhooks, `/simulate*`, `/api/*`, and the pages |
| `app/builder.py` | Product-builder service (categories → options → generate → checkout) |
| `app/bot.py` | Chat orchestration + reply formatting |
| `app/intent.py` | DM/comment text → prompt + category hint + quantity |
| `app/arcade.py` | Arcade API client (`platform.arcade.ai/v1`) |
| `app/mock_arcade.py` | Offline Arcade stub (no key needed) |
| `app/instagram.py` | Meta Graph sender, webhook verify, message/comment extraction |
| `app/static/builder.html` | **The mini-app** (product builder) — served at `/` |
| `app/static/chat.html` | Chat tester (DM / comment simulator) — served at `/chat` |
| `tests/test_flow.py` | Offline end-to-end tests |

**Mock by default.** With no `ARCADE_API_KEY` and no IG token, the whole flow
runs against fakes — the bot still replies with a (placeholder) image and a
(fake) checkout link. Add real credentials to go live; no code changes needed.

## Run it

```bash
cd ~/instagram-merch-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit .env and add your keys

# start the server
.venv/bin/uvicorn app.main:app --reload --port 8000

# in another shell: fire a fake DM at the bot and see the reply
curl -s localhost:8000/simulate -H 'content-type: application/json' \
  -d '{"sender_id":"u1","text":"make me a poster with neon mountains"}' | python3 -m json.tool
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

## Going live

### 1. Arcade (the merch engine)

- Create a key: **Arcade Studio → Settings → API keys**.
- Put it in `.env` as `ARCADE_API_KEY=...`.
- Set a real `ARCADE_USER_AGENT` (app name + contact) — Arcade's edge returns
  `403` for blank/default user agents.

Flow used (`app/arcade.py`):
`patterns/create {prompt, categoryId}` → poll `jobs/get` → `products/get` →
`checkouts/create {lines, dropShip:true}`. Every work-starting call takes a
unique `requestId` (idempotency key).

### 2. Instagram (the DM side)

Prerequisites — this is the part that isn't code:

1. Instagram **Professional** (Business/Creator) account.
2. Linked to a **Facebook Page**.
3. A **Meta developer app** with the **Instagram** product added.
4. Permissions: `instagram_business_basic`,
   `instagram_business_manage_messages` (and `instagram_business_manage_comments`
   if you add comment triggers).
5. A **public HTTPS** URL for `/webhook` (use a tunnel like ngrok/Cloudflare in dev).
6. In the app dashboard: subscribe the webhook to `messages`, set the callback
   URL to `https://<host>/webhook`, and set the **verify token** to match
   `IG_VERIFY_TOKEN` in `.env`.
7. Longer-lived **page access token** → `IG_ACCESS_TOKEN`; IG user id → `IG_USER_ID`.
8. **App Review** is required before the bot works for anyone other than
   admins/testers. The bot must respect the 24-hour messaging window and
   standard messaging policies.

### 3. Hosting

Any always-on host with HTTPS works (Fly.io, Render, a VPS). `uvicorn` behind a
reverse proxy is enough for the scaffold; put the job polling on a queue if you
expect volume (Arcade jobs take a minute or two — see below).

## Product builder (mini-app)

Open **http://127.0.0.1:8000** — a mobile-first, 4-step wizard built for an in-app webview (Instagram / Telegram): `100dvh` + safe-area insets, large touch targets, no hover states, one self-contained file with no libraries, auto light/dark.

1. **Choose a product** — every category Arcade offers (`categories/list`), with its world badge (print / jewelry). Tap to advance.
2. **Customize** — size & option values from `categories/get`; availability is re-checked on every pick, so impossible combinations are greyed out, and the resolved manufacture-able selection shows live.
3. **Amount** — quantity stepper with live unit price and total.
4. **Checkout** — summary + total → **Open checkout** (`checkouts/create` → hosted checkout URL).

**Entry point — Instagram post.** If the app is opened with no post link, it starts on a gate asking for one; pass it in the URL (`/?post=https://www.instagram.com/p/…`, alias `?url=`) and the gate is skipped. Accepted forms: `instagram.com/p|reel|reels|tv/<code>`, with or without scheme; tracking query strings are stripped.

**The post's picture becomes the design source.** The server reads the post's `og:image`, uploads those bytes to Arcade as a reference (`references/createUpload` → `PUT` → `references/create`) and passes `referenceIds` to `patterns/create` — so the pattern is generated *from* the post's art, not just a prompt. Step 1 shows the picture in the post chip with its status (*reading image… / using this post's art / couldn't read image*).

> Reading a post is **best-effort**: it works for public posts (Instagram serves `og:image` to crawler user-agents — verified live) but fails on private posts, age/region gates or rate limits. On failure the wizard continues with the house design and says so; **the purchase is never blocked**. Feeding an image directly also works — call `POST /api/generate` with `referenceId`, or upload via `references/*` yourself.

**No design prompt is shown.** Each product carries a fixed house design (the prompt sent to Arcade). Edit `HOUSE_DESIGNS` in `app/static/builder.html`, or override at runtime with `?design=your+prompt` in the URL.

**When a post picture is driving, the house style is suppressed.** A strong `modelId` (art style) drowns out a reference, and a descriptive prompt overrides its subject matter — so with a reference present the wizard sends **no `modelId`** (style defaults to *"No style — follow the post picture"*) and a **neutral prompt**. Verified: the same test image produced florals-with-an-X when a style model was sent, but the reference's actual stripe/cross structure when it wasn't.

How generation honours Arcade's two worlds (see `app/builder.py`):

- **print** → `patterns/create` (pattern only) → `products/create` with the **complete** selection from `categories/get`. If the picks can't be manufactured, the builder returns a 400 naming the conflicting options.
- **jewelry** → a single `patterns/create` with the `categoryId` designs the piece and builds its product (`products/create` refuses jewelry).

Builder API:

| Method | Path | Body → does |
| --- | --- | --- |
| GET | `/api/health` | liveness + live/mock mode |
| GET | `/api/categories` | `categories/list` |
| GET | `/api/models` | `models/list` |
| POST | `/api/options` | `{categoryId, selections}` → `categories/get` (options + availability + resolved selection) |
| POST | `/api/generate` | `{categoryId, prompt, selections?, modelId?, colors?, quantity?}` → product |
| POST | `/api/checkout` | `{productId, quantity}` → `{checkoutId, checkoutUrl}` |
| POST | `/api/reference/from-post` | `{postUrl}` → `{referenceId, imageUrl, thumbnailUrl, …}` |
| GET | `/api/products` | `products/list` |
| GET | `/api/cache` | cache status (entries, TTL, file) |
| POST | `/api/cache/refresh` | drop + rebuild the cached catalogue |

## Caching

Categories, styles and option trees are read-only and change rarely, but the webview asks for them on every open. They're cached **in memory + on disk** (`cache/arcade.json`), so a cold open costs **zero API calls** and responds in ~1 ms.

- **Read-through**: `CachedArcade` wraps the client and memoises only `list_categories`, `list_models` and `get_category`.
- **Never cached**: `patterns/create`, `products/create`, `checkouts/create`, `jobs/get` — they have side effects, and product image URLs are short-lived.
- **Persisted**: surviving restarts, so the app is warm immediately after deploy.
- **Warmed automatically** at startup (`WARM_CACHE_ON_START`), and on demand:

```bash
env -u PYTHONPATH .venv/bin/python scripts/precache.py   # build/refresh the cache
curl -s localhost:8000/api/cache                          # inspect it
curl -sX POST localhost:8000/api/cache/refresh            # drop + rebuild
```

`/api/categories` and `/api/models` also send `Cache-Control: public, max-age=<TTL>` so the webview caches them too.

Config: `CACHE_ENABLED`, `CACHE_TTL_SECONDS` (default 43200 = 12h), `CACHE_DIR`, `WARM_CACHE_ON_START`.

> Rebuilding the cache when Arcade's catalogue changes is expected: the same code in dev/prod diverges only by that TTL window.

## Comment-to-DM

Instead of (or as well as) DMs, people can **comment on your posts** and the bot
DMs them the product. The mechanism is a **private reply**: `POST
/{ig-id}/messages` with `recipient: {comment_id}` — this reaches someone who
never messaged you first. Rules: **once per comment**, and **within 7 days** of
the comment. Everything after that is normal 24-hour messaging.

Flow, once wired:
1. User comments on a post → Meta sends a `comments` webhook.
2. `extract_comments()` pulls `{comment_id, commenter_id, text}`.
3. `bot.handle_comment()` classifies the comment:
   - **build** — has a real brief (e.g. *"link please — a tote with cacti"* or just *"a tote with cacti"*) → generate a product, private-reply the image + caption + checkout link.
   - **ask** — triggered but no brief (e.g. *"send me the link"*) → replies asking for a brief rather than inventing a random product.
   - **ignore** — no trigger word and no product category (e.g. *"love this 😍"*).
4. Optionally also posts a public "check your DMs" reply (set `PUBLIC_REPLY=true`).

Config (`.env`):

| Var | Meaning | Default |
| --- | --- | --- |
| `COMMENT_TRIGGERS` | comma list; a comment containing any of these fires | `link,buy,shop,order,want,make,send,please,dm,🙋` |
| `PUBLIC_REPLY` | also post a public reply on the comment | `false` |
| `PUBLIC_REPLY_TEXT` | the public reply text | `Sent you a DM with the design + buy link! 📩` |

Extra Meta setup for comments:
- Add the **`comments`** webhook field (plus webhook field `messages` for DMs).
- Permission **`instagram_business_manage_comments`** (in addition to
  `instagram_business_manage_messages`) — App Review covers both.
- Comment webhooks only fire for posts on the connected IG account.

## Troubleshooting the live API

**Name collision:** this integration uses **Arcade.ai** (`docs.arcade.ai` — on‑demand manufacturing). **Arcade.dev** (`docs.arcade.dev`, `github.com/ArcadeAI/*`) is a different company's MCP/agent runtime — its skills don't apply here.

Run the live smoke test to see exactly which call fails and why:

```bash
cd ~/instagram-merch-bot
env -u PYTHONPATH .venv/bin/python scripts/smoke.py
```

Error map (from Arcade's errors doc):

| HTTP | Meaning | Fix |
| --- | --- | --- |
| `401` | key missing / invalid / revoked | mint a new key in Arcade Studio |
| `403` (non-JSON body) | edge rejection before the API | set a real `ARCADE_USER_AGENT` (`app/ver (contact)`) |
| `409` | `requestId` conflict | reuse an id only for the identical body |
| `412` | a quantity-1 line needs `dropShip` | the client sets it automatically |
| `422` | invalid body | read `data.fieldErrors` |
| `429` | rate limited | wait `Retry-After`, retry same `requestId` |
| `503` | temporary outage | retried with backoff |

Client contract handled in `app/arcade.py`: retries `429`/`5xx` with backoff (honouring `Retry-After`) and transient `409`s; parses `{code,status,message}`; re-reads `products/get` while `unitPrice` is null; sets `dropShip` **only** when a line has quantity 1; clamps prompt ≤1000 chars, colours ≤10, references ≤3.

## Gotchas (from Arcade's docs)

- **Jobs are async.** `patterns/create` returns immediately; poll `jobs/get`
  until `succeeded`/`failed`. A *succeeded* job can still have `result: null`
  (e.g. a deformity fix that found nothing) — handle it.
- **`products/create` needs the COMPLETE `selections` object** as returned by
  `categories/get`, not the partial one you sent. The one-shot path
  (`patterns/create` *with* a `categoryId`) sidesteps this by letting Arcade
  pick options.
- **Image URLs are short-lived.** `products/get` again to renew them; don't
  cache them.
- **Jewelry is special.** A jewelry category makes the pattern+product in one
  `patterns/create` call; `products/create` and refines refuse it. `intent.py`
  already routes rings/necklaces to a jewelry category when one exists.
- **`checkouts/create` with `quantity:1`** needs `dropShip:true` (or an active
  subscription), else `412 PRECONDITION_FAILED`.
- **Ack fast.** Meta wants a quick `200`; the handler does that and does the slow
  Arcade work inline. Move it to a background worker for scale.

## Next steps

- Add comment-to-DM triggers (`instagram_business_manage_comments`).
- Feed publishing via the Content Publishing API (`instagram_content_publish`)
  to post generated merch to the grid.
- Swap `intent.py`'s keyword router for an LLM call for richer briefs.
