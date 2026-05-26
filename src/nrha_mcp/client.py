"""Node-RED Admin API HTTP client with auth, optimistic locking, optional TLS skip.

Ported from nr-mcp (MIT) + PR #3. Constructor takes a Settings object.
"""

import httpx
from .config import Settings


class NRError(Exception):
    """Base Node-RED error."""


class NRAuthError(NRError):
    """401/403 — bad credentials."""


class NRConflictError(NRError):
    """409 — rev mismatch, flow changed outside MCP."""


class NRNotFoundError(NRError):
    """Node, flow, or context key not found."""


class NRClient:
    def __init__(self, settings: Settings):
        self.url = settings.nr_url
        self._token: str | None = settings.nr_token or None
        self._user: str | None = settings.nr_user or None
        self._pass: str | None = settings.nr_pass or None

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers=headers,
            timeout=30.0,
            verify=settings.nr_verify_ssl,
        )

    async def _ensure_auth(self) -> None:
        """Lazily exchange NR_USER/NR_PASS for a bearer token on first call."""
        if self._token or not self._user:
            return
        r = await self._client.post(
            "/auth/token",
            data={
                "client_id": "node-red-admin",
                "grant_type": "password",
                "scope": "*",
                "username": self._user,
                "password": self._pass or "",
            },
        )
        if r.status_code != 200:
            raise NRAuthError(
                f"Login failed at /auth/token ({r.status_code}). Check NR_USER/NR_PASS."
            )
        body = r.json()
        token = body.get("access_token")
        if not token:
            raise NRAuthError(
                "Login failed at /auth/token (200 but no access_token). Check NR_USER/NR_PASS."
            )
        self._token = token
        self._client.headers["Authorization"] = f"Bearer {self._token}"

    async def get_flows(self) -> tuple[list[dict], str]:
        await self._ensure_auth()
        r = await self._client.get("/flows", headers={"Node-RED-API-Version": "v2"})
        self._check_response(r)
        data = r.json()
        return data.get("flows", []), data.get("rev", "")

    async def post_flows(self, flows: list[dict], rev: str) -> str:
        await self._ensure_auth()
        headers = {
            "Node-RED-API-Version": "v2",
            "Node-RED-Deployment-Type": "full",
            "Content-Type": "application/json",
        }
        r = await self._client.post("/flows", json={"rev": rev, "flows": flows}, headers=headers)
        if r.status_code == 409:
            raise NRConflictError("Flow modified by another client (rev mismatch). Retry with fresh GET.")
        self._check_response(r)
        data = r.json() if r.content else {}
        return data.get("rev", "")

    async def get_context(self, flow_id: str) -> dict:
        await self._ensure_auth()
        r = await self._client.get(f"/context/flow/{flow_id}")
        self._check_response(r)
        return r.json()

    async def get_global_context(self) -> dict:
        await self._ensure_auth()
        r = await self._client.get("/context/global")
        self._check_response(r)
        return r.json()

    async def inject(self, node_id: str) -> bool:
        await self._ensure_auth()
        r = await self._client.post(f"/inject/{node_id}")
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            raise NRNotFoundError(f"Inject node '{node_id}' not found")
        raise NRError(f"Inject failed: {r.status_code} {r.text}")

    async def get_nodes(self) -> list[dict]:
        await self._ensure_auth()
        r = await self._client.get(
            "/nodes", headers={"Accept": "application/json", "Node-RED-API-Version": "v2"}
        )
        self._check_response(r)
        return r.json()

    async def install_module(self, module_name: str) -> dict:
        await self._ensure_auth()
        r = await self._client.post(
            "/nodes",
            json={"module": module_name},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=120.0,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 400:
            raise NRError(f"Module '{module_name}' not found or invalid")
        if r.status_code == 409:
            raise NRError(f"Module '{module_name}' already installed")
        raise NRError(f"Install failed: {r.status_code} {r.text}")

    def _check_response(self, r: httpx.Response):
        if r.status_code in (401, 403):
            raise NRAuthError(f"Authentication failed ({r.status_code}). Check NR_TOKEN or NR_USER/NR_PASS and NR_URL.")
        if r.status_code == 404:
            raise NRNotFoundError(f"Not found: {r.url}")
        if r.status_code >= 400:
            raise NRError(f"Node-RED API error {r.status_code}: {r.text[:200]}")

    async def close(self):
        await self._client.aclose()
