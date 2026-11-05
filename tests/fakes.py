"""Fake Crustdata client with the same method signatures and response shapes as the real one."""
from bgv.crustdata_client import CrustdataError


def company(cid, name, domain=None, year=None, total=None, timeseries=None):
    return {
        "crustdata_company_id": cid,
        "basic_info": {"name": name, "crustdata_company_id": cid, "primary_domain": domain,
                       "all_domains": [domain] if domain else [], "profile_name": name, "year_founded": year},
        "headcount": {"total": total, "timeseries": timeseries or []},
    }


def web(title, url, snippet=""):
    return {"title": title, "url": url, "snippet": snippet}


class FakeClient:
    def __init__(self, companies=(), web_results=None, people=None, search_results=None, fail=()):
        self.companies = list(companies)
        self.web_results = web_results or {}        # query substring -> results
        self.people = people or {}                  # linkedin url -> person_data
        self.search_results = search_results or []  # list of profiles, or callable(filters) -> list
        self.fail = set(fail)                       # method names that raise CrustdataError
        self.calls = []
        self.searches = []

    def _maybe_fail(self, method):
        self.calls.append(method)
        if method in self.fail:
            raise CrustdataError(f"/{method} returned 503: upstream unavailable")

    def identify_company(self, name=None, domain=None):
        self._maybe_fail("identify_company")
        out = []
        for c in self.companies:
            b = c["basic_info"]
            if (domain and domain in b["all_domains"]) or (name and name.split()[0].lower() in b["name"].lower()):
                out.append({"confidence_score": 0.6,
                            "company_data": {k: c[k] for k in ("crustdata_company_id", "basic_info")}})
        return out

    def enrich_company(self, crustdata_company_id, fields=None):
        self._maybe_fail("enrich_company")
        return next((c for c in self.companies if c["crustdata_company_id"] == crustdata_company_id), None)

    def web_search(self, query):
        self._maybe_fail("web_search")
        for key, results in self.web_results.items():
            if key.lower() in query.lower():
                return results
        return []

    def enrich_person(self, linkedin_url):
        self._maybe_fail("enrich_person")
        return self.people.get(linkedin_url)

    def search_people(self, filters, limit=10, fields=None):
        self._maybe_fail("search_people")
        self.searches.append(filters)
        rows = self.search_results(filters) if callable(self.search_results) else self.search_results
        return {"profiles": rows[:limit], "total_count": len(rows)}
