import unittest
from unittest.mock import patch

from src.api.routes import (
    get_cache_status,
    search_inventory,
    bulk_search_inventory,
    search_by_codes,
    refresh_cache,
    odoo_service,
)
from src.models.schemas import BulkSearchRequest, CodesRequest


class TestInventoryAPI(unittest.TestCase):
    def setUp(self):
        self.sample_products = [
            {
                "id": 201,
                "tmpl_id": 2001,
                "code": "XIA-NOTE13-BLK",
                "barcode": "6941812750011",
                "name": "Xiaomi Redmi Note 13 Black",
                "list_price": 1200000.0,
                "stock": 10.0,
            },
            {
                "id": 202,
                "tmpl_id": 2002,
                "code": "OUK-WP19-PRO",
                "barcode": "6950376701122",
                "name": "Oukitel WP19 Pro Rugged Phone",
                "list_price": 2800000.0,
                "stock": 4.0,
            },
            {
                "id": 203,
                "tmpl_id": 2003,
                "code": "1002706",
                "barcode": "7840001009999",
                "name": "Cargador Rapido 67W Xiaomi",
                "list_price": 150000.0,
                "stock": 40.0,
            },
        ]
        # Pre-poblar la tabla hash del servicio
        odoo_service.catalog_index.load(self.sample_products)

    def test_cache_status_endpoint(self):
        res = get_cache_status()
        data = res.model_dump()
        self.assertTrue(data["is_loaded"])
        self.assertEqual(data["item_count"], 3)
        self.assertIn("loaded_at", data)

    def test_inventory_search_via_hash(self):
        # Búsqueda por código
        items = search_inventory(q="XIA-NOTE13-BLK")
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "XIA-NOTE13-BLK")

        # Búsqueda por token en nombre
        items_name = search_inventory(q="oukitel rugged")
        self.assertEqual(len(items_name), 1)
        self.assertEqual(items_name[0]["tmpl_id"], 2002)

    def test_bulk_search_endpoint(self):
        req = BulkSearchRequest(
            queries=[
                "XIA-NOTE13-BLK",
                "6950376701122",
                "Cargador Rapido 67W",
                "PRODUCTO-INEXISTENTE-12345",
            ],
            match_mode="smart",
            refresh_cache=False,
        )
        res = bulk_search_inventory(req)
        data = res.model_dump()

        self.assertIn("items", data)
        self.assertIn("not_found", data)
        self.assertIn("stats", data)

        # 3 encontrados, 1 no encontrado
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(len(data["not_found"]), 1)
        self.assertEqual(data["not_found"][0], "PRODUCTO-INEXISTENTE-12345")

        stats = data["stats"]
        self.assertEqual(stats["total_queries"], 4)
        self.assertEqual(stats["found_count"], 3)
        self.assertEqual(stats["unique_found_count"], 3)
        self.assertEqual(stats["not_found_count"], 1)
        self.assertGreaterEqual(stats["execution_time_ms"], 0)

    def test_bulk_search_with_duplicates_and_empty_queries(self):
        req = BulkSearchRequest(
            queries=[
                "XIA-NOTE13-BLK",
                "XIA-NOTE13-BLK",
                "",
                "-",
                "--",
                "None",
                "null",
                "nan",
                "6950376701122",
                "UNKNOWN-CODE",
            ],
            match_mode="smart",
        )
        res = bulk_search_inventory(req)
        data = res.model_dump()
        stats = data["stats"]
        self.assertEqual(stats["total_queries"], 3)  # 2 unique existing + 1 unknown
        self.assertEqual(stats["found_count"], 2)
        self.assertEqual(stats["unique_found_count"], 2)
        self.assertEqual(stats["not_found_count"], 1)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(len(data["not_found"]), 1)

    def test_by_codes_endpoint(self):
        req = CodesRequest(codes=["XIA-NOTE13-BLK", "6950376701122", "CODE-FAIL"])
        items = search_by_codes(req)
        self.assertEqual(len(items), 2)

    def test_cache_refresh_endpoint(self):
        with patch.object(odoo_service, "fetch_catalog") as mock_fetch:
            mock_fetch.return_value = (self.sample_products, {}, {})
            res = refresh_cache()
            data = res.model_dump()
            self.assertTrue(data["is_loaded"])
            mock_fetch.assert_called_once_with(active_only=True)


if __name__ == "__main__":
    unittest.main()
