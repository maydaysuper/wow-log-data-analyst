from core.wcl_client import WCLClient


class CaptureWCL(WCLClient):
    def __init__(self):
        self.calls = []

    def query(self, query, variables=None):
        self.calls.append((query, variables or {}))
        if "expansions" in query:
            return {"worldData": {"expansions": [{"id": 1, "name": "Expansion"}]}}
        if "zones(expansion_id:" in query:
            return {"worldData": {"zones": [{"id": 10, "name": "Dungeon", "encounters": [{"id": 20, "name": "Run"}]}]}}
        return {}


def test_world_expansion_catalog_query():
    cli = CaptureWCL()
    data = cli.world_expansions()
    assert data["worldData"]["expansions"][0]["id"] == 1
    query, variables = cli.calls[-1]
    assert "worldData" in query and "expansions" in query
    assert variables == {}


def test_world_zones_catalog_query_uses_expansion_id():
    cli = CaptureWCL()
    data = cli.world_zones(42)
    assert data["worldData"]["zones"][0]["encounters"][0]["id"] == 20
    query, variables = cli.calls[-1]
    assert "zones(expansion_id: $expansion)" in query
    assert "brackets { min max bucket type }" in query
    assert variables["expansion"] == 42
