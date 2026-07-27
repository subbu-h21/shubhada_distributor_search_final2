# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**PharmaScrape / Shubhada Distributor Search** — a FastAPI + MongoDB + React app that logs into pharma-distributor web portals (via Playwright) and searches multiple of them in parallel for a product's stock/price, so a pharmacy can quickly see who can fulfill an order. It also has a "price-list vault" (bulk-upload distributor price lists from Excel/PDF, auto-mapped and searchable), a "product master" catalog (bulk import + search-as-you-type), and a PO-placement automation against Shubhada Pharma's own ERP.

This repo is scaffolded and actively developed via the emergent.sh agent platform (`.emergent/emergent.yml`); commit history is machine-generated ("auto-commit for `<job-id>`" / "Auto-generated changes"). Expect `backend/server.py` and `backend/adapters/` to change shape frequently as new distributor portals are added — treat any file/line-number reference below as approximate, not pinned.

**Repo history note**: this repo (`shubhada_distributor_search_final2`) was created because pushes to the original `shubhada_distributor_search` repo started failing. The cause was **repo bloat, not a content conflict** — `.pw-browsers/` (a committed ~229MB Linux Chromium download) and `backend/data/screenshots/` (1,647+ accumulated PNGs) had been tracked in git since the start, ballooning `.git` past what pushes could reliably complete. Both are now gitignored and untracked here (see "Gotchas" below) — if push failures recur, check `git count-objects -v` / `du -sh .git` before assuming it's a code problem.

## Quick start (Windows): `setup.bat` then `start.bat`
Both live at the repo root and automate everything in the "Running locally" section below — idempotent, safe to re-run (they skip anything already done and never overwrite an existing `.env`). `setup.bat` needs Docker Desktop, Python (via the `py` launcher, whatever version it defaults to — no version gate; if the venv creation later fails, see "Backend venv" below for the one known-bad case), and Node.js already installed; it creates the backend venv, installs deps (stripping the unresolvable `emergentintegrations` package), installs Playwright's Chromium, generates both `.env` files with fresh secrets, creates/starts the Mongo container, and runs `yarn install`. `start.bat` starts Docker Desktop if needed, starts the Mongo container, then opens the backend and frontend each in their own `cmd` window. Two batch gotchas worth remembering if editing these: (1) parentheses inside `echo` text break cmd.exe's parser when that `echo` sits inside a parenthesized `if (...)` block — rephrase without parens or move the text outside the block; (2) don't `goto` a label defined inside a parenthesized block (used for the "wait for Docker to be ready" retry loop) — put the label and loop at the top level instead, as both scripts now do.

## Running locally (verified on Windows — Python 3.14 default, no yarn/mongo preinstalled)

This is the exact sequence that produces a working local stack from a fresh clone. Re-run only the steps that are missing (venv/`.env`/node_modules/mongo container persist between sessions).

1. **MongoDB** — nothing installed locally; used Docker instead (Docker Desktop present but not auto-started):
   ```bash
   docker run -d --name pharmascrape-mongo -p 27017:27017 -v pharmascrape-mongo-data:/data/db mongo:7
   # next time: docker start pharmascrape-mongo
   ```
2. **Backend venv — Python 3.11 or 3.12, not 3.14.** Tested directly (not guessed): `pip install -r requirements.local.txt` succeeds cleanly on both 3.11 and 3.12 (real prebuilt wheels for every package, nothing compiled from source). On 3.14 it fails with a genuine, reproducible dependency conflict, not a missing-wheel issue: `google-api-core[grpc]` requires `grpcio-status>=1.75.1` specifically when `python_version >= "3.14"`, but `requirements.txt` pins `grpcio-status==1.71.2`, an unresolvable version clash. That conflict comes from `google-api-core`/`google-generativeai`/`litellm` — unused Google/LLM boilerplate from the emergent.sh template, nothing this app's code actually imports — so it's not fixable by touching this codebase, only by using 3.11/3.12 instead of 3.14+. (3.13 untested; given the conflict is explicitly gated on `python_version >= "3.14"`, it would very likely also work, but don't take that on faith either.) `requirements.txt` also lists `emergentintegrations==0.2.0`, a private package only resolvable from emergent.sh's internal index — it is **not imported anywhere in the code**, so it's safe to strip before installing:
   ```bash
   cd backend
   py -3.12 -m venv .venv   # or py -3.11 — either works, prefer the newer
   grep -v "^emergentintegrations" requirements.txt > requirements.local.txt
   ./.venv/Scripts/python.exe -m pip install -r requirements.local.txt
   ./.venv/Scripts/python.exe -m playwright install chromium
   ```
   Note: the repo ships a `.pw-browsers/` directory with a **Linux** Chromium build (from emergent's container) — useless on Windows/macOS. Ignore it; `playwright install chromium` above puts a real Windows build inside the venv's own cache, and `_get_browser()` in `server.py` will fall back to that correctly (see Gotchas).
3. **`backend/.env`** (gitignored, not committed — generate your own):
   ```bash
   MONGO_URL=mongodb://localhost:27017
   DB_NAME=pharmascrape
   JWT_SECRET=<openssl rand -hex 32, or: python -c "import secrets;print(secrets.token_hex(32))">
   ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())">
   JWT_EXPIRE_DAYS=30
   ```
   **If reusing an existing Mongo volume/data from a previous local setup**, don't generate a fresh `ENCRYPTION_KEY` — reuse the old one. Distributor passwords already stored in that DB were encrypted with the old key; a new key makes `decrypt_secret()` raise `InvalidToken` on every existing distributor with `hasCredentials: true`. (`JWT_SECRET` is safe to regenerate — it only invalidates existing login sessions.)
4. **Start backend**:
   ```bash
   cd backend && ./.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8001
   ```
   On first boot against an empty DB it self-seeds users (`shubhada/2612`, `manju/6387`, `abhishek/5555`, `narendra/6666`), portals, and distributors — no manual seeding needed. A passlib/bcrypt `AttributeError: module 'bcrypt' has no attribute '__about__'` line in the log is a harmless known version-string mismatch between `passlib==1.7.4` and `bcrypt>=4.1`; caught internally, doesn't affect login.
5. **Frontend**: no `yarn` binary present, but `package.json` pins `packageManager: yarn@1.22.22` — `corepack enable` fetches that exact version on first use.
   ```bash
   cd frontend
   echo "REACT_APP_BACKEND_URL=http://localhost:8001" > .env
   corepack enable
   yarn install
   yarn start   # craco start, dev server on :3000
   ```
   `REACT_APP_BACKEND_URL` is required — the frontend always calls `${REACT_APP_BACKEND_URL}/api/...` (see `src/lib/api.js`); there is no dev proxy, so frontend and backend must be started separately (`yarn start` does not launch the backend).
6. Open `http://localhost:3000` and log in with one of the seeded users above.

### Backend env vars (full list)
- `MONGO_URL`, `DB_NAME` — Mongo connection (via Motor)
- `JWT_SECRET` — required at import time; app raises `RuntimeError` without it
- `ENCRYPTION_KEY` — Fernet key for encrypting stored distributor passwords; also required at import time
- `JWT_EXPIRE_DAYS` (optional, default 30)
- `SCREENSHOTS_DIR`, `SCREENSHOT_RETENTION_DAYS` (optional)
- `PLAYWRIGHT_BROWSERS_PATH` (optional, defaults to `/pw-browsers` — that path only makes sense inside the emergent.sh container; see Gotchas)

### Tests
Two unrelated test setups exist — don't confuse them:
- `backend/tests/*.py` — pytest suites that hit a **running backend** over HTTP (they read `REACT_APP_BACKEND_URL` from the env or from `/app/frontend/.env`, an emergent.sh container path that won't exist on a plain local checkout — export `REACT_APP_BACKEND_URL` yourself before running). Run from `backend/`:
  ```bash
  pytest tests/test_extract_regression.py -k some_test
  ```
  `backend/pytest.ini` pins `addopts = -n 2 --dist loadscope` (pytest-xdist, 2 workers, one worker per test class/module because generated suites share state). **Do not change these addopts** — the file has an explicit `# AGENT: do NOT modify addopts` comment. To run serially use `-n 0` (not `-p no:xdist`, which errors since addopts already passes `-n`).
  `test_extract_regression.py` and `test_chethana_extract.py` poll the async `/api/extract` contract correctly (via a shared `_extract_and_wait()` helper) — `/api/extract` returns `{task_id, status: "running"}` immediately; real results come from `GET /api/extract/status/{task_id}` (see "Long-running automation" below). If you see an assertion reading `results`/`status` straight off a raw `POST /api/extract` response anywhere (new test, new script), that's the same stale-contract mistake — route it through `_extract_and_wait()`/polling instead.
  Also note `backend/tests/test_shubhada_po_live.py`, `inspect_add_new_medicine.py`, and `inspect_shubhada_dialog.py` are **not sandboxed tests** — they log into the real production Shubhada ERP (`shubhadahealth.com:7007`, hardcoded live credentials) and `test_shubhada_po_live.py` actually **places a real purchase order**. Never run these without explicit confirmation from whoever owns that ERP account.
- Root-level `*_test.py` scripts (`backend_test.py`, `regression_test.py`, `sunshop_regression_test.py`, `pharmascrape_regression_test.py`) — standalone scripts (not pytest, run with `python <file>.py`), each hardcoding or reading a `BASE_URL` for a **live preview deployment**, not localhost. These are point-in-time regression logs from past feature work, kept for reference; check the `BASE_URL`/env lookup at the top of a given file before running it.

### Fixed: YashikaAdapter used to drop rows after an out-of-stock variant (false NOT_FOUND)
`adapters/yashika.py`'s results-table parser (`extract()`, the `page.evaluate` block) used to unconditionally consume the two lines following each row's `stock` marker (`YES`/`NO`) as that row's `mrp`/`rate`. But Yashika renders **no MRP/Rate text at all** for out-of-stock rows — so whenever a `NO`-stock row preceded another row, the parser swallowed the next row's `name`/`company` as fake mrp/rate, and the real next row got lost entirely, filtered out by the `/^(YES|NO)$/`/`/^\d/` guards. Reproduced live: "PROLOMET XL 25" against YASHIKA returned only the out-of-stock `Prolomet Xl 12.5Mg` row and `NOT_FOUND`, even though the site's actual table had a second row (`Prolomet Xl 25Mg`, in stock, MRP 68.03) the parser never reached. **Fixed** by only consuming the following lines as mrp/rate when they look like an actual price (`/^\d+(\.\d+)?$/`); otherwise they're left for the next row. Verified live post-fix: same search now returns `SUCCESS`, `row_count: 2`, correct MRP/PTR, `canFulfill: true`.

### Fixed: CHIRAG PHARMA / VARDHAMAN MEDISALES weren't using their dedicated adapters
`seed_if_empty()` in `server.py` used to hardcode `"portalType": "GENERIC"` for both `CHIRAG PHARMA` (`portal: "CHETHANA"`) and `VARDHAMAN MEDISALES PVT LTD` (`portal: "VARDHAMAN"`), so `infer_portal_type()` was never consulted and both silently ran through `GenericAdapter`. **Fixed** in two places: the seed dict now sets the correct `portalType` for fresh databases, and a startup migration (next to the existing `CHETHANA PHARMA` name-based one) now repoints *any* distributor stuck on `portalType: "GENERIC"` when `infer_portal_type(portal)` disagrees — self-healing for already-seeded databases (verified against ours: both got repointed on restart) and for any future case of the same mistake, not just these two names.

### Auth hardening: login rate-limiting (added for internet exposure via Cloudflare Tunnel)
`POST /api/auth/login` now rate-limits via an in-memory (per-process, resets on restart) tracker keyed by **both** the caller's IP and the attempted username — 5 failed attempts within 15 minutes returns `429` for that IP, and separately for that username, so one account can't be brute-forced from many IPs and one IP can't spray many accounts. `_login_rate_limit_keys()` in `server.py` prefers the `CF-Connecting-IP` header (falling back to `X-Forwarded-For`, then the raw socket) — **this matters** because behind Cloudflare Tunnel `request.client.host` is always `127.0.0.1` (the tunnel connects locally), so without reading that header every visitor would collapse into one shared IP-bucket and one person's typo could lock out the whole team. Trusting these headers is safe only because nothing but `cloudflared` (or a local caller) can reach this port. `POST /api/auth/change-password` also now requires an 8-char minimum (was 4) — the seeded accounts still have their original 4-digit PINs and need real passwords set via that endpoint before this is exposed to the internet; that's not something to do from code, it's a per-account decision for whoever owns each login.

`test_result.md` at the repo root is the emergent.sh main-agent/testing-agent handoff log (YAML-in-markdown). The header block marked `START/END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION` must be preserved verbatim; only the data below it is meant to be appended to.

### Production hosting (self-hosted, not emergent.sh) — single-origin build served by the backend
This app is being moved off emergent.sh onto self-hosting (local network / Cloudflare Tunnel to a custom domain), independent of anything emergent-specific above. The frontend and backend used to be two separate origins (`:3000` dev server → `:8001` API) — fine on one machine, broken for anyone else, since a public visitor's browser would try to reach *their own* `localhost:8001`, not the server's. Fixed by making the backend serve the built frontend directly, so there's exactly one origin/port:

- `frontend/.env.production` sets `REACT_APP_BACKEND_URL=` (empty) — CRA/craco picks this over `.env` automatically for `yarn build` (NOT for `yarn start`, which still uses `.env`'s `http://localhost:8001` for local dev). Empty means `src/lib/api.js`'s `API_BASE` becomes the relative path `/api`, so the built bundle always calls back to whatever origin served it. **This file is deliberately un-gitignored** (see the `!frontend/.env.production` exception in `.gitignore`) despite the blanket `.env.*` rule — it holds no secrets (`REACT_APP_*` vars are inlined into the public JS bundle regardless of what serves them), and losing it silently breaks every future production build for anyone but whoever's machine still has the old file.
- `server.py` mounts `frontend/build/static` and adds a catch-all `GET /{full_path:path}` route (registered *after* `app.include_router(api_router)`, so `/api/*` always matches first) that serves real static files when they exist and falls back to `index.html` otherwise — required for CRA client-side routes like `/search`/`/history` to work on direct navigation instead of 404ing. This route only activates if `frontend/build/` exists, so plain `uvicorn` + no build present (i.e. normal local dev, where you're using the CRA dev server instead) is unaffected.
- Rebuild-and-restart flow when you change frontend code: `cd frontend && yarn build`, then restart the backend (`cd backend && ./.venv/Scripts/python.exe -m uvicorn server:app --host 127.0.0.1 --port 8001`) — the build isn't hot-reloaded, it's a static snapshot served until you rebuild.
- Backend stays bound to `127.0.0.1` (not `0.0.0.0`) — a Cloudflare Tunnel connects *outbound* from this machine to Cloudflare's edge, so nothing needs to accept inbound connections and no firewall port needs opening. Point the tunnel's ingress rule at `http://localhost:8001`; that one port now serves everything (API + frontend).

### Production build
```bash
cd frontend && yarn build   # craco build
```

## Architecture

### Backend — mostly single-file FastAPI app (`backend/server.py`) + adapter/module plugins
Everything routes through one `api_router` (prefix `/api`) in `server.py`, with a couple of features split into their own router modules registered from there (see "Price-list vault" below — that's the one deviation from "it's all in server.py"). Data lives in MongoDB collections: `users`, `portals`, `targets` (= distributors, name kept for backwards compat), `history` (extraction runs), `products` (bulk-uploaded catalog), `pricelist_uploads`/`pricelist_rows`/`pricelist_mappings`, plus per-portal OTP-session collections (`liveconnect_session`, `retailio_session`, and MARG's equivalent).

- **Auth**: JWT (`auth.py`, PyJWT + passlib/bcrypt). A `@app.middleware("http")` in `server.py` guards every `/api/*` route except `/api/auth/*`, `/api/screenshots/*` and the bare `/api/` root — so any new route is auth-gated by default; don't add a manual `Depends` for that. Seeded users (dev-only, plaintext in `seed_if_empty()`) are `shubhada/2612`, `manju/6387`, `abhishek/5555`, `narendra/6666`.
- **Credentials at rest**: distributor `username`/`password` are stored with `password` Fernet-encrypted (`security.py`, `ENCRYPTION_KEY`) as `encryptedPassword`; `strip_mongo()` strips that field from every outbound response — the API never returns raw passwords. Preserve that when touching distributor endpoints.
- **Distributor/adapter pattern** (`backend/adapters/`): `BaseAdapter` (`adapters/base.py`) defines `test_login()` and `extract()`; each portal has a concrete subclass (`SunshopAdapter`, `ChethanaAdapter`, `VardhamanAdapter`, `LiveconnectAdapter`, `RetailioAdapter`, `YashikaAdapter`, `MargAdapter`), and `GenericAdapter` is the fallback for portals without a dedicated adapter. `adapters/__init__.py:get_adapter(portal_type, **kwargs)` is the registry — new portals are added there and in `infer_portal_type()` in `server.py` (which maps a distributor's `portal` string to a `portalType`). `adapters/match.py` and `adapters/probe.py` hold shared fuzzy product-name matching and autocomplete-probing helpers reused across adapters (see the module docstrings — they encode nontrivial rules for combo-drug strength matching, e.g. `ECOSPRIN AV 75/10`).
- **Extraction flow**: `POST /api/extract` fans out to a per-distributor runner concurrently (bounded `asyncio.Semaphore`), each in its own Playwright browser context, and persists one `history` document with per-distributor results embedded. `POST /api/extract/manual-pick` reruns a single distributor against a forced candidate name and splices the new result back into an existing history entry.
- **OTP-session portals** (LIVECONNECT, RETAILIO, MARG): login requires an SMS OTP, so each has a dedicated begin/verify session manager (`liveconnect_session.py`, `retailio_session.py`, `marg_session.py`) that keeps an in-memory pending Playwright page keyed by a `pendingId` (short TTL) and, once verified, persists cookies (+ localStorage where relevant) into Mongo so `extract()` can reuse the session without re-prompting for OTP.
- **Long-running automation avoids edge timeouts via fire-and-poll**: both `POST /api/extract` and `POST /api/order/place` (Shubhada PO placement, `shubhada_po.py`) don't await their Playwright automation inline — each kicks off `asyncio.create_task()`, returns `{task_id, status: "running"}` immediately, and the frontend polls `GET /api/extract/status/{task_id}` / `GET /api/order/status/{task_id}` until `status == "done"`. The code comments are explicit about why: Cloudflare's ~100s edge timeout would otherwise kill the request. Follow the same pattern for any new automation that might run long — and if you're calling `/api/extract` from a script (tests included), poll the status endpoint rather than reading `results` off the initial `POST` response.
- **Browser launch** (`_get_browser()` in `server.py`) has a 3-tier fallback: bundled Playwright Chromium → a system Chromium at a few hardcoded paths → on-the-fly `playwright install chromium`. This exists because the emergent.sh container's filesystem can reset between sessions; don't "simplify" it away without checking that constraint still applies wherever this deploys. It always launches `headless=True` — there is no code path that opens a visible browser window, locally or in the cloud; every meaningful step is instead captured via `page.screenshot()` and served back through `GET /api/screenshots/{filename}`.
- **Price-list vault** (`pricelist.py`, registered via `register_routes(api_router, db)` at the bottom of `server.py`): the one module with its own `APIRouter` instead of living inline. Lets a pharmacist upload a distributor's raw Excel/PDF price list, auto-suggests a column mapping, and makes the parsed rows searchable across all distributors. Pending uploads are pickled to disk (`backend/data/pricelist_pending/`) between upload and confirm so a backend restart mid-review doesn't lose the parse.
- **Product master**: `POST /api/products/upload` accepts .xlsx/.csv, fuzzy-matches columns by header aliases (`_pick_col`), **replaces** the entire `products` collection on every upload, and indexes a normalized `norm` field for `GET /api/products/search` prefix/substring matching.

### Frontend — CRA + craco, shadcn/ui, monochrome brutalist/monospace UI
- `src/App.js` wires `AuthProvider` → (login gate) → `AppProvider` → router. Routes include `/search`, `/portals`, `/history`, and a price-list page, all under `Layout`.
- `src/context/AuthContext.jsx` owns the JWT (`localStorage['ps.token']`) and current user; `src/context/AppContext.js` owns product/quantity search state, the distributor list, and history, calling the API layer directly (no react-query/SWR despite both being dependencies).
- `src/lib/api.js` is the single axios instance: attaches the bearer token to every request, and auto-logs-out + redirects on any `401` (except from `/auth/login` itself). Route new backend calls through the typed `*API` objects there rather than calling axios directly.
- `src/components/ui/` is shadcn/ui (`components.json`: style "new-york", baseColor "neutral", JS not TSX). Regenerate/add components with the shadcn CLI rather than hand-rolling primitives.
- `src/constants/testIds/` is a registry of `data-testid` values consumed by an external QA/testing agent ("qabot") to drive the UI end-to-end. When adding interactive UI, add a testid there and wire it in rather than leaving elements unaddressable — see the file header comment for the convention.

## Gotchas

- **Never commit `.pw-browsers/` or `backend/data/screenshots/`.** Both are gitignored now (see repo history note above) after they bloated `.git` to 350MB+ and broke pushes. If you see either showing up as untracked-and-large again, that's a regression — check `.gitignore` before investigating anything else.
- **Reusing an existing Mongo volume across a fresh checkout**: keep the same `ENCRYPTION_KEY`. A mismatched key doesn't error at startup — it only surfaces later as `decrypt_secret()` failures ("Password decrypt failed") on distributors that already have stored credentials.
- **`emergentintegrations==0.2.0`** in `requirements.txt` is unresolvable outside emergent.sh's private index and isn't imported anywhere — strip it before `pip install`, don't try to find a substitute.
- **`.pw-browsers/` in the repo is a Linux build** — irrelevant on Windows/macOS dev machines; always run `playwright install chromium` locally rather than pointing `PLAYWRIGHT_BROWSERS_PATH` at it.
