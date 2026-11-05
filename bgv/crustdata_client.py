"""Thin Crustdata REST client: auth headers, retry on 429/5xx, and a SQLite cache.

The cache means a re-run (after a crash, or during the demo) does not spend credits
twice and continues from where it stopped.

Endpoints and field names were checked against docs.crustdata.com and a live
/person/enrich response on 6 Nov 2026 (API version 2025-11-01).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
CACHE_PATH = os.getenv("BGV_CACHE_PATH", "bgv_cache.sqlite")


class CrustdataError(Exception):
    pass


class CrustdataClient:
    def __init__(self, api_key: str | None = None, timeout: int = 30, max_retries: int = 4):
        self.api_key = api_key or os.getenv("CRUSTDATA_API_KEY")
        if not self.api_key:
            raise CrustdataError("CRUSTDATA_API_KEY is not set (check your .env file).")
        self.timeout = timeout
        self.max_retries = max_retries
        self.db = sqlite3.connect(CACHE_PATH, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS api_cache (key TEXT PRIMARY KEY, path TEXT, body TEXT, "
            "response TEXT, created_at REAL)"
        )
        self.db.commit()

    # ---------- low level ----------

    def _post(self, path: str, body: dict[str, Any], use_cache: bool = True) -> Any:
        key = hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if use_cache:
            row = self.db.execute("SELECT response FROM api_cache WHERE key = ?", (key,)).fetchone()
            if row:
                return json.loads(row[0])

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "x-api-version": API_VERSION,
        }
        last_error = ""
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(BASE_URL + path, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as exc:  # timeout, connection reset
                last_error = f"network error: {exc}"
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 200:
                data = resp.json()
                self.db.execute(
                    "INSERT OR REPLACE INTO api_cache VALUES (?, ?, ?, ?, ?)",
                    (key, path, json.dumps(body), json.dumps(data), time.time()),
                )
                self.db.commit()
                return data
            if resp.status_code == 429:
                wait = float(resp.headers.get("x-ratelimit-reset", 2 ** attempt + 1))
                time.sleep(min(wait, 60))
                continue
            if resp.status_code >= 500:
                last_error = f"{resp.status_code}: {resp.text[:200]}"
                time.sleep(2 ** attempt)
                continue
            # 400/401/402/403: retrying will not help
            raise CrustdataError(f"{path} returned {resp.status_code}: {resp.text[:300]}")

        raise CrustdataError(f"{path} failed after {self.max_retries} attempts ({last_error})")

    # ---------- endpoints used by the verifier ----------

    def identify_company(self, name: str | None = None, domain: str | None = None) -> list[dict]:
        """FREE. Returns ranked candidates [{confidence_score, company_data: {crustdata_company_id, basic_info}}]."""
        if domain:
            body: dict[str, Any] = {"domains": [domain]}
        elif name:
            body = {"names": [name]}
        else:
            raise ValueError("Give a name or a domain.")
        result = self._post("/company/identify", body)
        return result[0]["matches"] if result else []

    def enrich_company(self, crustdata_company_id: int, fields: list[str] | None = None) -> dict | None:
        """2 credits. basic_info has year_founded and website; headcount has headcount.total."""
        body = {
            "crustdata_company_ids": [crustdata_company_id],
            "fields": fields or ["basic_info", "headcount"],
        }
        result = self._post("/company/enrich", body)
        if not result or not result[0].get("matches"):
            return None
        return result[0]["matches"][0]["company_data"]

    def enrich_person(self, linkedin_url: str) -> dict | None:
        """1 credit. Needs a LinkedIn URL.

        person_data.experience.employment_details.{current,past}[] items have:
        name, title, start_date, end_date, crustdata_company_id, company_website_domain,
        company_status, company_headcount_latest.
        person_data.education.schools[] items have: school, degree, field_of_study,
        start_year, end_year.
        """
        body = {
            "professional_network_profile_urls": [linkedin_url],
            "fields": ["basic_profile", "experience", "education"],
        }
        result = self._post("/person/enrich", body)
        if not result or not result[0].get("matches"):
            return None
        return result[0]["matches"][0]["person_data"]

    def search_people(self, filters: dict, limit: int = 10, fields: list[str] | None = None) -> dict:
        """0.03 credits per result, +0.1 per premium field group (experience, education...).

        Useful filter fields: basic_profile.name, experience.employment_details.company_name,
        experience.employment_details.past.company_name,
        experience.employment_details.past.company_website_domain.
        """
        body: dict[str, Any] = {"filters": filters, "limit": limit}
        if fields:
            body["fields"] = fields
        return self._post("/person/search", body)

    def web_search(self, query: str) -> list[dict]:
        """1 credit. Returns results[] with title, url, snippet, position."""
        result = self._post("/web/search/live", {"query": query, "sources": ["web"]})
        return result.get("results", []) if isinstance(result, dict) else []

    def web_fetch(self, urls: list[str]) -> list[dict]:
        """Up to 10 URLs. Each entry: success, url, title, content (HTML)."""
        return self._post("/web/enrich/live", {"urls": urls[:10]})


if __name__ == "__main__":
    # Smoke test with the FREE endpoint: python crustdata_client.py
    client = CrustdataClient()
    matches = client.identify_company(domain="crustdata.com")
    top = matches[0]["company_data"] if matches else {}
    print("crustdata_company_id:", top.get("crustdata_company_id"))
    print("name:", top.get("basic_info", {}).get("name"))
