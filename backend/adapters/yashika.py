"""YASHIKA AGENCIES adapter — https://www.yashikaagencies.in

Verified flow:
  1. GET /Customer (login form: input[name=username] = Customer ID,
     input[name=password] = password, button Sign In).
  2. On success we land on /components/customer/CustomerDashboard
     ("Place Order" page) which has a single text input with placeholder
     "Search Product".
  3. Type the product name — Yashika renders matching SKUs directly in a
     table (columns: Product Name, Company, Pack, Scheme, Stock, MRP,
     Rate). No autocomplete dropdown, no drill-in required.
  4. Parse each row → one ExtractedItem per row.

Stock column shows a green "YES" chip or red "NO" chip. Expiry is embedded
in the Pack column ("15. Exp: 04/28").
"""
from __future__ import annotations
import re
from typing import List, Optional
from .base import BaseAdapter, ExtractionOutcome, ExtractedItem
from .match import canon, score, ACCEPT_THRESHOLD


LOGIN_PATH = "/Customer"
SEARCH_PATH = "/components/customer/CustomerDashboard"


def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").strip())


class YashikaAdapter(BaseAdapter):
    portal_type = "YASHIKA"

    async def test_login(self, page, url: str, username: str, password: str):
        try:
            await page.goto(url.rstrip("/") + LOGIN_PATH, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            await page.fill("input[name='username']", username)
            await page.fill("input[name='password']", password)
            await page.click("button:has-text('Sign In')")
            await page.wait_for_timeout(4500)
            if "CustomerDashboard" in (page.url or ""):
                return True, "Login OK"
            body = ""
            try: body = (await page.inner_text("body"))[:200].lower()
            except Exception: pass
            if "invalid" in body or "wrong" in body or "incorrect" in body:
                return False, "Invalid customer ID or password"
            return False, f"Login didn't reach dashboard (landed on {page.url})"
        except Exception as e:
            return False, f"{e.__class__.__name__}: {e}"

    async def extract(self, page, url: str, username: str, password: str,
                      product: str, quantity: int, distributor_name: str = "",
                      force_candidate_name: Optional[str] = None) -> ExtractionOutcome:
        out = ExtractionOutcome()
        out.requested_qty = quantity or None

        try:
            # 1) Login
            await page.goto(url.rstrip("/") + LOGIN_PATH, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)
            try: await page.fill("input[name='username']", username)
            except Exception:
                out.status = "ERROR"; out.detail = "Login form not found"
                out.login_screenshot = await self._screenshot(page, "no-login-form")
                return out
            await page.fill("input[name='password']", password)
            await page.click("button:has-text('Sign In')")
            await page.wait_for_timeout(5000)

            if "CustomerDashboard" not in (page.url or ""):
                body = ""
                try: body = (await page.inner_text("body"))[:200]
                except Exception: pass
                out.status = "LOGIN_FAILED"
                out.detail = f"Login failed. URL={page.url}. Body: {body[:100]}"
                out.login_screenshot = await self._screenshot(page, "login-failed")
                return out
            out.login_screenshot = await self._screenshot(page, "logged-in")

            # 2) Find the "Search Product" input
            search_el = await page.query_selector("input[placeholder='Search Product']")
            if not search_el:
                out.status = "ERROR"
                out.detail = "Search Product input not found"
                out.results_screenshot = await self._screenshot(page, "no-search")
                return out

            try: await search_el.click()
            except Exception: pass
            try: await search_el.fill("")
            except Exception: pass

            raw_tokens = re.findall(r"[a-z0-9]+", product.lower())
            if not raw_tokens:
                out.status = "NOT_FOUND"; out.detail = "Empty product query"
                return out
            query_canon = canon(product)

            # 3) Type word-by-word (matches the pattern used for other portals)
            for i, tok in enumerate(raw_tokens):
                piece = (" " if i > 0 else "") + tok
                try:
                    await search_el.type(piece, delay=90)
                except Exception:
                    try: await page.keyboard.type(piece, delay=90)
                    except Exception: break
                await page.wait_for_timeout(700 if i < len(raw_tokens) - 1 else 2500)

            out.search_screenshot = await self._screenshot(page, "search-populated")

            # 4) Parse the results table. Structure (verified):
            #    Product Name | Company | Pack | Scheme | Stock | MRP | Rate
            rows_info = await page.evaluate(r"""() => {
                // The table appears to be a set of divs rendered as a grid.
                // We look for rows containing 7 cells: name, company, pack,
                // scheme (may be empty), stock (YES/NO), mrp, rate.
                // Robust fallback: read the visible innerText after the
                // header row and split by lines.
                const t = (document.body.innerText || '');
                const headerIdx = t.indexOf('Product Name\nCompany\nPack');
                if (headerIdx < 0) return null;
                // Slice from AFTER the header labels until the next known
                // section ("Priority", "#\tProduct Name", or "Approx Order").
                const after = t.slice(headerIdx + 'Product Name\nCompany\nPack\nScheme\nStock\nMRP\nRate'.length);
                const endMarkers = ['#\tProduct Name', '#\nProduct Name', 'Priority\n', 'Approx Order'];
                let endIdx = after.length;
                for (const m of endMarkers) {
                    const i = after.indexOf(m);
                    if (i > -1 && i < endIdx) endIdx = i;
                }
                const chunk = after.slice(0, endIdx).trim();
                if (!chunk) return { rows: [] };
                const lines = chunk.split(/\n/).map(s => s.trim()).filter(Boolean);
                // Each row is 6 lines: name, company, pack, stock, mrp, rate
                // (scheme is usually absent/empty, so it's omitted from lines).
                // But sometimes scheme appears making it 7 lines. Detect by
                // looking for YES/NO which marks the "stock" cell.
                const rows = [];
                let i = 0;
                while (i < lines.length) {
                    // Skip forward until we find a plausible product-name line
                    const name = lines[i]; i++;
                    if (!name || /^(YES|NO)$/i.test(name) || /^\d/.test(name)) continue;
                    // Look ahead for the stock marker to determine row width
                    let stockPos = -1;
                    for (let j = i; j < Math.min(i + 6, lines.length); j++) {
                        if (/^(YES|NO)$/i.test(lines[j])) { stockPos = j; break; }
                    }
                    if (stockPos < 0) break;
                    // Between name(idx-1) and stockPos there are: company, pack, [scheme]
                    const midCount = stockPos - i;
                    let company = '', pack = '', scheme = '';
                    if (midCount === 2) { company = lines[i]; pack = lines[i+1]; }
                    else if (midCount === 3) { company = lines[i]; pack = lines[i+1]; scheme = lines[i+2]; }
                    else if (midCount === 4) { company = lines[i] + ' ' + lines[i+1]; pack = lines[i+2]; scheme = lines[i+3]; }
                    else { company = lines.slice(i, stockPos).join(' '); }
                    const stock = lines[stockPos];
                    // MRP/Rate cells render no text node at all for out-of-stock rows,
                    // so only consume the following lines as mrp/rate when they actually
                    // look like a price — otherwise they belong to the NEXT row's
                    // name/company and must be left for the next iteration.
                    const isPrice = (s) => /^\d+(\.\d+)?$/.test((s || '').trim());
                    let mrp = '', rate = '', next = stockPos + 1;
                    if (isPrice(lines[stockPos + 1])) {
                        mrp = lines[stockPos + 1];
                        next = stockPos + 2;
                        if (isPrice(lines[stockPos + 2])) {
                            rate = lines[stockPos + 2];
                            next = stockPos + 3;
                        }
                    }
                    rows.push({ name, company, pack, scheme, stock, mrp, rate });
                    i = next;
                }
                return { rows };
            }""") or {"rows": []}

            rows = rows_info.get("rows") or []
            out.debug["row_count"] = len(rows)

            if not rows:
                out.status = "NOT_FOUND"
                out.detail = "No matching products in Yashika"
                out.results_screenshot = await self._screenshot(page, "no-rows")
                return out

            # 5) Score rows by name match
            scored = []
            for r in rows:
                s = score(query_canon, r.get("name") or "")
                scored.append((s, r))
            scored.sort(key=lambda x: -x[0])
            out.debug["candidates"] = [{"name": r.get("name"), "score": s} for s, r in scored[:10]]

            # Manual pick override
            if force_candidate_name:
                f_canon = canon(force_candidate_name)
                for i, (s, r) in enumerate(scored):
                    n = r.get("name") or ""
                    if canon(n) == f_canon or force_candidate_name.lower() in n.lower():
                        scored.insert(0, (55, r))
                        scored.pop(i + 1)
                        break

            best_s = scored[0][0] if scored else -1000
            if best_s < ACCEPT_THRESHOLD:
                out.status = "NOT_FOUND"
                out.detail = f"No matching product variant in Yashika (best score {best_s})"
                out.debug["autocomplete_candidates"] = [r.get("name") for _, r in scored[:15]]
                out.results_screenshot = await self._screenshot(page, "no-match")
                return out

            # Accept all rows near the best score (variants)
            accepted = [r for s, r in scored if s >= max(ACCEPT_THRESHOLD, best_s - 25)]

            items: List[ExtractedItem] = []
            for r in accepted:
                pack_txt = r.get("pack") or ""
                exp_m = re.search(r"Exp[:\s]*([\d/]+)", pack_txt, re.I)
                expiry = exp_m.group(1) if exp_m else None
                pack_clean = re.sub(r"\.?\s*Exp[:\s]*[\d/]+\.?", "", pack_txt, flags=re.I).strip().rstrip(".").strip() or None
                scheme = (r.get("scheme") or "").strip() or None
                stock_yes = re.match(r"^\s*YES\s*$", r.get("stock") or "", re.I) is not None
                items.append(ExtractedItem(
                    product=product,
                    matched_name=r.get("name") or None,
                    pack=pack_clean,
                    expiry=expiry,
                    scheme=scheme,
                    mrp=(r.get("mrp") or "").strip() or None,
                    ptr=(r.get("rate") or "").strip() or None,
                    manufacturer=(r.get("company") or "").strip() or None,
                    available_qty=("1" if stock_yes else "0"),
                ))

            out.items = items
            out.results_screenshot = await self._screenshot(page, "results")

            in_stock = sum(1 for it in items if (it.available_qty or "").isdigit() and int(it.available_qty) > 0)
            out.status = "SUCCESS"
            out.detail = f"{len(items)} variant(s), {in_stock} in stock"
            if quantity:
                out.can_fulfill = in_stock > 0
            return out
        except Exception as e:
            out.status = "ERROR"
            out.detail = f"{e.__class__.__name__}: {e}"
            try: out.results_screenshot = await self._screenshot(page, "error")
            except Exception: pass
            return out
