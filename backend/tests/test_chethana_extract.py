"""Backend regression tests for the CHETHANA color-stock bug fix.

Covers:
  * Auth (shubhada/2612 → JWT)
  * GET /api/targets (find CHIRAG PHARMA + others)
  * POST /api/extract for CHIRAG PHARMA (CHETHANA) — verify stockStatus,
    resultsScreenshot filename, item.available_qty mapping.
  * GET /api/screenshots/{filename} returns image/png
  * SUNSHOP regression: same product against a SUNSHOP distributor.
  * LIVECONNECT session status.
"""
import os
import re
import time
import pytest
import requests

def _load_backend_url():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if v:
        return v.rstrip("/")
    envf = "/app/frontend/.env"
    if os.path.exists(envf):
        for line in open(envf):
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not set")

BASE_URL = _load_backend_url()
API = f"{BASE_URL}/api"
USERNAME = "shubhada"
PASSWORD = "2612"
PRODUCT = "PROLOMET XL 25"
QTY = 10
EXTRACT_TIMEOUT = 180


@pytest.fixture(scope="session")
def token():
    r = requests.post(f"{API}/auth/login", json={"username": USERNAME, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "access_token" in data or "token" in data, data
    return data.get("access_token") or data.get("token")


@pytest.fixture(scope="session")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def targets(auth_headers):
    r = requests.get(f"{API}/targets", headers=auth_headers, timeout=30)
    assert r.status_code == 200, r.text
    docs = r.json()
    assert isinstance(docs, list) and len(docs) > 0
    return docs


def _find(targets, portal_type=None, name_contains=None):
    for d in targets:
        if portal_type and (d.get("portalType") or "").upper() != portal_type:
            continue
        if name_contains and name_contains.lower() not in (d.get("name") or "").lower():
            continue
        return d
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


# ---------- CHETHANA / CHIRAG PHARMA (the bug fix under test) ----------
class TestChethanaColorStatus:
    def test_chirag_pharma_target_exists(self, targets):
        d = _find(targets, portal_type="CHETHANA", name_contains="CHIRAG")
        assert d is not None, "CHIRAG PHARMA (CHETHANA) target not found in db"
        assert d.get("id")

    def test_extract_returns_stock_status_and_color_screenshot(self, targets, auth_headers):
        d = _find(targets, portal_type="CHETHANA", name_contains="CHIRAG")
        assert d is not None
        body = _extract_and_wait(auth_headers, d["id"])
        history_id = body.get("id")
        assert history_id
        results = body.get("results") or []
        assert len(results) == 1
        result = results[0]
        status = result.get("status")
        assert status in ("SUCCESS", "NOT_FOUND"), f"unexpected status {status}: {result.get('detail')}"

        # stockStatus must be present in debug and not null
        debug = result.get("debug") or {}
        stock = debug.get("stockStatus")
        assert stock in ("AVAILABLE", "INSUFFICIENT", "UNAVAILABLE"), (
            f"stockStatus must be AVAILABLE/INSUFFICIENT/UNAVAILABLE, got {stock!r}. detail={result.get('detail')}"
        )

        # resultsScreenshot filename starts with history_id and contains 'results-with-color'
        shot = result.get("resultsScreenshot")
        assert shot, "resultsScreenshot missing"
        assert shot.startswith(history_id), f"screenshot {shot!r} does not start with history_id {history_id!r}"
        assert "results-with-color" in shot, f"screenshot {shot!r} missing 'results-with-color'"

        # item[0].available_qty mapping
        items = result.get("items") or []
        assert len(items) >= 1
        aq = items[0].get("available_qty")
        expected = {"AVAILABLE": "in-stock", "INSUFFICIENT": "partial", "UNAVAILABLE": "0"}[stock]
        assert aq == expected, f"available_qty {aq!r} != expected {expected!r} for stock {stock}"

        # Verify screenshot is reachable
        sr = requests.get(f"{API}/screenshots/{shot}", timeout=30)
        assert sr.status_code == 200, f"screenshot fetch returned {sr.status_code}"
        ctype = sr.headers.get("content-type", "")
        assert "image/png" in ctype, f"unexpected content-type {ctype}"
        assert len(sr.content) > 500, "screenshot content too small"

        # expose for later tests
        pytest.chethana_result = result
        pytest.chethana_history_id = history_id


# ---------- SUNSHOP regression ----------
class TestSunshopRegression:
    def test_sunshop_extract_no_error(self, targets, auth_headers):
        d = _find(targets, portal_type="SUNSHOP")
        if not d:
            # try by name
            d = _find(targets, name_contains="SAROJ") or _find(targets, name_contains="HEGDE")
        if not d:
            pytest.skip("No SUNSHOP distributor configured")
        body = _extract_and_wait(auth_headers, d["id"])
        result = (body.get("results") or [{}])[0]
        assert result.get("status") in ("SUCCESS", "NOT_FOUND"), (
            f"SUNSHOP status regressed to {result.get('status')}: {result.get('detail')}"
        )


# ---------- LIVECONNECT session status ----------
class TestLiveconnect:
    def test_session_status_active(self, auth_headers):
        r = requests.get(f"{API}/liveconnect/session", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        # Just ensure endpoint responds with active flag; the request says active=true
        assert "active" in data, data
        assert data.get("active") is True, f"LIVECONNECT session not active: {data}"
