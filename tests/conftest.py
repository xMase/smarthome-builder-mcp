"""Shared test fixtures: an in-memory fake Node-RED client and sample flows."""

import copy
import pytest

from nrha_mcp.client import NRConflictError, NRNotFoundError, NRError
from nrha_mcp.config import Settings


SAMPLE_FLOWS = [
    {"id": "tab1", "type": "tab", "label": "Heating"},
    {"id": "n1", "type": "inject", "z": "tab1", "name": "tick", "wires": [["n2"]]},
    {"id": "n2", "type": "function", "z": "tab1", "name": "calc", "func": "return msg;", "wires": [["n3"]]},
    {"id": "n3", "type": "api-call-service", "z": "tab1", "name": "boiler on",
     "domain": "switch", "service": "turn_on", "entityId": "switch.boiler", "wires": [[]]},
    {"id": "g1", "type": "group", "z": "tab1", "name": "grp", "nodes": ["n2", "n3"]},
    {"id": "srv1", "type": "server", "z": "", "name": "Home Assistant"},
]


class FakeNRClient:
    """In-memory stand-in for NRClient. Same method contract."""

    def __init__(self, flows=None, rev="r0"):
        self._flows = copy.deepcopy(flows if flows is not None else SAMPLE_FLOWS)
        self._rev = rev
        self.context = {}          # flow_id -> {"default": {...}, "store": {...}}
        self.global_context = {"default": {}, "store": {}}
        self.nodes = []            # raw /nodes payload
        self.injected = []         # node_ids injected
        self.ws_token = None      # token returned by get_ws_token()
        self.installed = []        # module names installed
        self.post_calls = []       # list of (flows, rev) posted

    async def get_flows(self):
        return copy.deepcopy(self._flows), self._rev

    async def post_flows(self, flows, rev):
        if rev != self._rev:
            raise NRConflictError("rev mismatch")
        self.post_calls.append((copy.deepcopy(flows), rev))
        self._flows = copy.deepcopy(flows)
        self._rev = f"{self._rev}+1"
        return self._rev

    async def get_context(self, flow_id):
        return self.context.get(flow_id, {"default": {}, "store": {}})

    async def get_global_context(self):
        return self.global_context

    async def get_ws_token(self):
        return self.ws_token

    async def inject(self, node_id):
        self.injected.append(node_id)
        return True

    async def get_nodes(self):
        return self.nodes

    async def install_module(self, module_name):
        self.installed.append(module_name)
        return {"version": "1.0.0", "types": ["x"]}

    async def close(self):
        pass


@pytest.fixture
def fake_client():
    return FakeNRClient()


@pytest.fixture
def settings_propose(tmp_path):
    return Settings(deploy_mode="propose", backup_dir=str(tmp_path / "backups"))


@pytest.fixture
def settings_auto(tmp_path):
    return Settings(deploy_mode="auto", backup_dir=str(tmp_path / "backups"))
