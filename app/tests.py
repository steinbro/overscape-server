import hashlib
import gzip
import json
import math
from pathlib import Path

import pytest
from requests.exceptions import ConnectTimeout
import responses

from cache import CompressedJSONCache
from overpass import OverpassClient, OverpassResponse


class TestCompressedJSONCache:
    @pytest.fixture
    def cache_dir(self):
        return Path(__file__).parent / "_test_cache"

    @pytest.fixture
    def cache(self, cache_dir):
        return CompressedJSONCache(cache_dir, max_days=0, max_entries=1)

    def test_corrupt_gzip(self, cache_dir, cache):
        with open(cache_dir / "foo.json.gz", "w") as f:
            f.write("not gzipped")
        assert "" == cache.get("foo", lambda: "")

    def test_corrupt_json(self, cache_dir, cache):
        with gzip.open(cache_dir / "foo.json.gz", "w") as f:
            f.write(b"not json")
        assert "" == cache.get("foo", lambda: "")


@pytest.fixture
def overpass_client():
    return OverpassClient(
        "https://overpass.kumi.systems/api/interpreter/",
        "Overscape/0.1",
        cache_dir=Path("_test_cache"),
        cache_days=7,
        cache_size=1e5,
    )


class TestOverpassClient:
    @responses.activate
    def test_connection_error(self, overpass_client, caplog):
        # trigger an (instantaneous) tiemout error on all requests
        responses.add(
            responses.GET,
            overpass_client.server,
            body=ConnectTimeout(),
        )
        q = overpass_client._build_query(1, 1)
        assert overpass_client._execute_query(q) is None
        assert len(caplog.records) == 1
        assert "error connecting" in caplog.records[0].message

    @responses.activate
    def test_server_error(self, overpass_client, caplog):
        # trigger a 500 error on all requests
        responses.add(
            responses.GET,
            overpass_client.server,
            json={"error": "something went wrong"},
            status=500,
        )
        q = overpass_client._build_query(2, 2)
        assert overpass_client._execute_query(q) is None
        assert len(caplog.records) == 1
        assert "received 500" in caplog.records[0].message


class TestGeoJSON:
    def overpass_response(self, x, y, overpass_client):
        """Outside of tests, we cache our transformed GeoJSON. But in tests,
        since we want to test the transformation, we only cache the response
        from Overpass.
        """
        q = overpass_client._build_query(x, y)
        overpass_json = overpass_client.cache.get(
            hashlib.sha256(q.encode("utf-8")).hexdigest(),
            lambda: overpass_client._execute_query(q).overpass_json,
        )
        return OverpassResponse(overpass_json)

    @pytest.mark.parametrize(
        "x,y",
        [
            [18741, 25054],
            [18747, 25074],
            [18751, 25065],
        ],
    )
    def test_geojson_schema(self, x, y, overpass_client):
        """Check that we match the Soundscape GeoJSON format described at
        https://github.com/steinbro/soundscape/blob/main/docs/services/data-plane-schema.md

        This might be checkable with jsonschema validation.
        """
        json_data = self.overpass_response(
            x, y, overpass_client
        ).as_soundscape_geojson()

        assert len(json_data.keys()) == 2
        assert json_data["type"] == "FeatureCollection"
        assert len(json_data["features"]) > 0
        for feature in json_data["features"]:
            assert "feature_type" in feature
            assert "feature_value" in feature
            assert "geometry" in feature
            assert "osm_ids" in feature
            assert "properties" in feature
            assert "type" in feature

            assert "coordinates" in feature["geometry"]
            assert "type" in feature["geometry"]

    def find_features_by_attrs(attrs, geojson):
        for n in geojson["features"]:
            if any(n[k] != v for (k, v) in attrs.items()):
                continue
            yield n

    def compare_features(a, b):
        for key in ("feature_type", "feature_value", "osm_ids", "properties"):
            assert a[key] == b[key]
        assert a["geometry"]["type"] == b["geometry"]["type"]
        for a_val, b_val in zip(
            a["geometry"]["coordinates"], b["geometry"]["coordinates"]
        ):
            assert math.isclose(a_val, b_val, rel_tol=1e-7)

    @pytest.mark.parametrize(
        "feature_type,feature_value",
        [
            # ['amenity', 'post_office'],
            # ["building", "yes"],
            ["highway", "bus_stop"],
            # ["highway", "primary"],
            ["historic", "memorial"],
            ["office", "insurance"],
        ],
    )
    @pytest.mark.parametrize(
        "x,y",
        [
            [18741, 25054],
            [18747, 25074],
            [18751, 25065],
        ],
    )
    def test_geojson_compare(self, x, y, feature_type, feature_value, overpass_client):
        """Test against some sample JSON responses generated by the original
        Soundscape tile server.
        """
        our_geojson = self.overpass_response(
            x, y, overpass_client
        ).as_soundscape_geojson()

        with open(
            Path(__file__).parent.parent / "test_reference" / f"{x}_{y}.json"
        ) as f:
            reference_geojson = json.load(f)

        reference_nodes = list(
            TestGeoJSON.find_features_by_attrs(
                {"feature_type": feature_type, "feature_value": feature_value},
                reference_geojson,
            )
        )
        if len(reference_nodes) == 0:
            return pytest.skip(
                f'no "{feature_type}": "{feature_value}" in {x}_{y}.json'
            )

        for reference_node in reference_nodes:
            our_node = list(
                TestGeoJSON.find_features_by_attrs(
                    {"osm_ids": reference_node["osm_ids"]}, our_geojson
                )
            )
            if len(our_node) == 1:
                TestGeoJSON.compare_features(reference_node, our_node[0])
            else:
                pytest.fail(
                    f"{len(our_node)} nodes with osm_id {reference_node['osm_ids']}"
                )

    @pytest.mark.parametrize(
        "x,y",
        [
            [18741, 25054],
            [18747, 25074],
            [18751, 25065],
        ],
    )
    def test_intersections(self, x, y, overpass_client):
        """Check that each road in an intersection also appears as a feature."""
        overpass_response = self.overpass_response(x, y, overpass_client)
        our_geojson = overpass_response.as_soundscape_geojson()

        intersections = list(overpass_response._compute_intersections())
        assert len(intersections) > 0
        for intersection in intersections:
            for id in intersection["osm_ids"]:
                assert 1 == len(
                    list(
                        TestGeoJSON.find_features_by_attrs(
                            {"osm_ids": [id]}, our_geojson
                        )
                    )
                )
