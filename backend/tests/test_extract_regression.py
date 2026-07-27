"""Regression tests for CHETHANA bug fix and new VARDHAMAN adapter.

Coverage:
  1. Auth (shubhada / 2612)
  2. GET /api/targets — discover CHIRAG PHARMA, VARDHAMAN, SAROJ, KAPILA
  3. CHETHANA: stockStatus must be non-null after Tab-based qty entry fix
  4. VARDHAMAN: SUCCESS on 'PROLOMET XL 25' (Avl), NOT_FOUND on fake drug
  5. SUNSHOP: no ERROR regression
  6. Manual-pick still works via /api/extract/manual-pick
"""
import os, time, pytest, requests

def _base():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if not v:
        for line in open("/app/frontend/.env"):
            if line.startswith("REACT_APP_BACKEND_URL="):
                v = line.split("=", 1)[1].strip()
                break
    return v.rstrip("/")

BASE = _base()
API = f"{BASE}/api"
PRODUCT = "PROLOMET XL 25"
QTY = 10
EXTRACT_TIMEOUT = 240


@pytest.fixture(scope="session")
def headers():
    r = requests.post(f"{API}/auth/login",
                      json={"username": "shubhada", "password": "2612"}, timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json().get("token") or r.json().get("access_token")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
def targets(headers):
    r = requests.get(f"{API}/targets", headers=headers, timeout=30)
    assert r.status_code == 200
    return r.json()


def _find(targets, name_sub, portal_type=None):
    for t in targets:
        if name_sub.lower() in (t.get("name") or "").lower():
            if portal_type and (t.get("portalType") or "").upper() != portal_type.upper():
                continue
            return t
    return None


def _extract_and_wait(headers, tid, product=PRODUCT, quantity=QTY, timeout=EXTRACT_TIMEOUT):
    """POST /api/extract only kicks off a background task (fire-and-poll, to
    dodge Cloudflare's ~100s edge timeout) — poll /api/extract/status/{task_id}
    for the actual history-entry result."""
    r = requests.post(f"{API}/extract",
                      json={"product": product, "quantity": quantity, "target_ids": [tid]},
                      headers=headers, timeout=30)
    assert r.status_code == 200, f"extract kickoff HTTP {r.status_code}: {r.text[:400]}"
    task_id = r.json().get("task_id")
    assert task_id, f"no task_id in kickoff response: {r.text[:400]}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = requests.get(f"{API}/extract/status/{task_id}", headers=headers, timeout=30)
        assert sr.status_code == 200, f"extract/status HTTP {sr.status_code}: {sr.text[:400]}"
        data = sr.json()
        if data.get("status") == "done":
            assert not data.get("error"), f"extract task failed: {data['error']}"
            return data.get("result") or {}
        time.sleep(3)
    raise AssertionError(f"extract task {task_id} did not complete within {timeout}s")


def _extract(headers, tid, product=PRODUCT, quantity=QTY):
    body = _extract_and_wait(headers, tid, product=product, quantity=quantity)
    return body, (body.get("results") or [{}])[0]


# ---------- CHETHANA color-status regression ----------
class TestChethanaColor:
    def test_stock_status_and_screenshot(self, headers, targets):
        d = _find(targets, "CHIRAG", "CHETHANA")
        assert d, "CHIRAG PHARMA (CHETHANA) target not found"
        body, res = _extract(headers, d["id"])
        history_id = body.get("id")
        assert res.get("status") == "SUCCESS", f"status={res.get('status')} detail={res.get('detail')}"
        debug = res.get("debug") or {}
        stock = debug.get("stockStatus")
        assert stock in ("AVAILABLE", "INSUFFICIENT", "UNAVAILABLE"), (
            f"debug.stockStatus must not be null, got {stock!r}. detail={res.get('detail')}"
        )
        shot = res.get("resultsScreenshot") or ""
        assert "results-with-color" in shot, f"screenshot name {shot!r} missing 'results-with-color'"
        assert shot.startswith(history_id or ""), f"{shot} !startswith {history_id}"
        items = res.get("items") or []
        assert items, "no items returned"
        aq = items[0].get("available_qty")
        assert aq is not None and aq in ("in-stock", "partial", "0"), (
            f"available_qty {aq!r} not mapped"
        )
        # (d/e) The screenshot filename should end with .png and be fetchable
        assert shot.endswith(".png"), f"screenshot {shot!r} not .png"
        r = requests.get(f"{API}/screenshots/{shot}", headers=headers, timeout=30)
        assert r.status_code == 200, f"GET /api/screenshots/{shot} -> {r.status_code}"
        assert r.headers.get("content-type", "").startswith("image/"), (
            f"content-type={r.headers.get('content-type')!r}"
        )


# ---------- VARDHAMAN new adapter ----------
class TestVardhamanSuccess:
    def test_prolomet_available(self, headers, targets):
        d = _find(targets, "VARDHAMAN", "VARDHAMAN")
        assert d, "VARDHAMAN target not found"
        body, res = _extract(headers, d["id"])
        assert res.get("status") == "SUCCESS", (
            f"expected SUCCESS, got {res.get('status')}: {res.get('detail')}. debug={res.get('debug')}"
        )
        items = res.get("items") or []
        assert items, "no items returned"
        it = items[0]
        assert "PROLOMET XL 25" in (it.get("matched_name") or "").upper(), (
            f"matched_name mismatch: {it.get('matched_name')!r}"
        )
        assert (it.get("pack") or "").strip(), f"pack empty: {it.get('pack')!r}"
        assert (it.get("mrp") or "").strip(), f"mrp empty: {it.get('mrp')!r}"
        debug = res.get("debug") or {}
        assert (debug.get("avl") or "").strip().lower() == "avl", f"debug.avl={debug.get('avl')!r}"
        shot = res.get("resultsScreenshot") or ""
        assert "results-with-avl" in shot, f"screenshot {shot!r} missing 'results-with-avl'"


class TestVardhamanNotFound:
    def test_fake_drug(self, headers, targets):
        d = _find(targets, "VARDHAMAN", "VARDHAMAN")
        assert d
        body, res = _extract(headers, d["id"], product="XYZFAKEDRUG 99", quantity=1)
        assert res.get("status") == "NOT_FOUND", (
            f"expected NOT_FOUND, got {res.get('status')}: {res.get('detail')}"
        )
        cands = (res.get("debug") or {}).get("candidates")
        assert isinstance(cands, list), f"debug.candidates should be list, got {type(cands).__name__}"


# ---------- SUNSHOP regression ----------
class TestSunshopRegression:
    def test_saroj_no_error(self, headers, targets):
        d = _find(targets, "SAROJ", "SUNSHOP")
        assert d, "SAROJ PHARMA (SUNSHOP) target not found"
        body, res = _extract(headers, d["id"])
        assert res.get("status") in ("SUCCESS", "NOT_FOUND"), (
            f"SUNSHOP regressed to {res.get('status')}: {res.get('detail')}"
        )


# ---------- LIVECONNECT regression ----------
class TestLiveconnectRegression:
    def test_liveconnect_no_error(self, headers, targets):
        lc = [t for t in targets if (t.get("portalType") or "").upper() == "LIVECONNECT"]
        assert lc, "no LIVECONNECT targets found"
        for d in lc:
            body, res = _extract(headers, d["id"])
            assert res.get("status") in ("SUCCESS", "NOT_FOUND"), (
                f"LIVECONNECT {d.get('name')} regressed to {res.get('status')}: {res.get('detail')}"
            )


# ---------- Distributor location field ----------
class TestTargetsLocation:
    def test_location_present(self, targets):
        assert isinstance(targets, list) and targets
        for t in targets:
            # location must be present (string or None) — should not raise on access
            assert "location" in t, f"target {t.get('name')} missing 'location' key"
            loc = t.get("location")
            assert loc is None or isinstance(loc, str), (
                f"location for {t.get('name')} must be str/None, got {type(loc).__name__}"
            )


# ---------- Manual pick ----------
class TestManualPick:
    def test_manual_pick_after_not_found(self, headers, targets):
        d = _find(targets, "KAPILA PHARMA", "SUNSHOP")
        assert d
        body, res = _extract(headers, d["id"])
        # If SUCCESS on first hit, skip (we need a NOT_FOUND to have candidates)
        history_id = body.get("id")
        if res.get("status") != "NOT_FOUND":
            pytest.skip(f"KAPILA didn't return NOT_FOUND for PROLOMET, got {res.get('status')}; can't test manual-pick")
        payload = {
            "history_id": history_id,
            "target_id": d["id"],
            "candidate_name": "PROLYTE ORS APPLE LIQ",
        }
        r = requests.post(f"{API}/extract/manual-pick", json=payload, headers=headers, timeout=EXTRACT_TIMEOUT)
        assert r.status_code == 200, f"manual-pick HTTP {r.status_code}: {r.text[:400]}"
        mp = r.json()
        target_result = mp.get("result") or mp
        # Fallback for older shape returning full history
        if "status" not in target_result and isinstance(mp.get("results"), list):
            target_result = next((rr for rr in mp["results"] if rr.get("targetId") == d["id"]), mp["results"][0])
        assert target_result.get("status") == "SUCCESS", (
            f"manual-pick status={target_result.get('status')}: {target_result.get('detail')}"
        )
