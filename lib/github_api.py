"""Read GitHub REST records with bounded retries, repository-scoped pagination, and drift guards."""

import json
import sys
from http.client import BadStatusLine, IncompleteRead
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


READ_ATTEMPTS = 3
SCAN_ATTEMPTS = 3
TRANSIENT_FAULTS = (IncompleteRead, BadStatusLine, ConnectionError, TimeoutError, URLError)


def query(url, **values):
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params.update(values)
    return urlunsplit(parts._replace(query=urlencode(params)))


class HistoryDrift(ValueError):
    pass


class ApiError(ValueError):
    def __init__(self, code, method, resource):
        self.code = code
        super().__init__(f"GitHub API {method} {resource} failed (HTTP {code})")


class GitHub:
    label = "github-api"

    def __init__(self, api_url, repository, token, graphql_url=None):
        self.api_root = api_url.rstrip('/')
        self.root = f"{self.api_root}/repos/{repository}"
        self.repository_id = None
        self.graphql_url = graphql_url or f"{self.api_root}/graphql"
        if urlsplit(self.graphql_url).netloc != urlsplit(api_url).netloc:
            raise ValueError("GitHub GraphQL endpoint left the API host")
        self.token = token

    def transport(self, request, timeout):
        return urlopen(request, timeout=timeout)

    def pause(self, seconds):
        sleep(seconds)

    def request(self, method, resource, payload=None):
        if not self.token:
            raise ValueError("github-token is required to read GitHub records")
        url = resource if resource.startswith("https://") else self.root + resource
        allowed = url in (self.root, self.graphql_url) or url.startswith(self.root + "/")
        if not allowed and url.startswith(self.api_root + "/repositories/"):
            if self.repository_id is None:
                repository = self.get("")
                if type(repository.get("id")) is not int or repository["id"] <= 0:
                    raise ValueError("GitHub returned an invalid repository identity")
                self.repository_id = repository["id"]
            allowed = url.startswith(f"{self.api_root}/repositories/{self.repository_id}/")
        if not allowed:
            raise ValueError("GitHub pagination left the repository API")
        request = Request(url, method=method,
                          data=json.dumps(payload or {}).encode() if method == "POST" else None,
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        path = urlsplit(url).path
        reads = READ_ATTEMPTS if method == "GET" or url == self.graphql_url else 1
        for attempt in range(1, reads + 1):
            try:
                with self.transport(request, 30) as response:
                    body = response.read()
                    return (json.loads(body) if body else {}, response.headers.get("Link", ""))
            except HTTPError as error:
                error.close()
                if error.code < 500 or attempt == reads:
                    raise ApiError(error.code, method, path) from None
                fault = f"HTTP {error.code}"
            except TRANSIENT_FAULTS as error:
                reason = getattr(error, "reason", None)
                fault = type(error).__name__ + (f"({type(reason).__name__})"
                                                if isinstance(reason, BaseException) else "")
                if reads == 1:
                    raise ValueError(f"GitHub API {method} {path} outcome is unknown ({fault}); "
                                     "check the run before retrying it") from None
                if attempt == reads:
                    raise ValueError(f"GitHub API is unavailable: {method} {path} failed "
                                     f"{reads} times ({fault})") from None
            print(f"::warning::{self.label}: rereading {method} {path} after {fault} "
                  f"(attempt {attempt}/{reads})", file=sys.stderr)
            self.pause(attempt)

    def get(self, resource):
        return self.request("GET", resource)[0]

    def pages(self, resource, field, total_field="total_count", cap=None):
        for attempt in range(1, SCAN_ATTEMPTS + 1):
            try:
                return self.scan(resource, field, total_field, cap)
            except HistoryDrift as drift:
                if attempt == SCAN_ATTEMPTS:
                    raise
                print(f"::warning::{self.label}: rereading {urlsplit(resource).path} from its first page "
                      f"after {drift} (scan {attempt}/{SCAN_ATTEMPTS})", file=sys.stderr)

    def scan(self, resource, field, total_field, cap):
        url = query(resource if resource.startswith("https://") else self.root + resource, per_page=100)
        seen = set()
        identities = set()
        rows = []
        total = None
        while url:
            if url in seen:
                raise ValueError("GitHub pagination repeated a page")
            seen.add(url)
            data, links = self.request("GET", url)
            if (not isinstance(data, dict) or not isinstance(data.get(field), list)
                    or type(data.get(total_field)) is not int or data[total_field] < 0):
                raise ValueError("GitHub returned an incomplete history response")
            if total is None:
                total = data[total_field]
            elif data[total_field] != total:
                raise HistoryDrift("GitHub history count changed during pagination")
            for row in data[field]:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                    raise ValueError("GitHub history contains invalid record identities")
                if row["id"] in identities:
                    raise HistoryDrift("GitHub history contains duplicate record identities")
                identities.add(row["id"])
            rows.extend(data[field])
            if len(rows) > total:
                raise HistoryDrift("GitHub history contains more records than its declared count")
            if cap and len(rows) >= cap:
                break
            url = next((part.split(";", 1)[0].strip()[1:-1]
                        for part in links.split(",") if 'rel="next"' in part), None)
        if len(rows) < min(total, cap or total):
            raise HistoryDrift("GitHub history pagination ended before all records arrived")
        return rows, total
