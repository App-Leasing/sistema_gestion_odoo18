import unittest
from datetime import datetime, timedelta

from src.services.hash_index import (
    CatalogHashIndex,
    normalize_text,
    normalize_code,
    tokenize,
)


class TestCatalogHashIndex(unittest.TestCase):
    def setUp(self):
        self.sample_products = [
            {
                "id": 101,
                "tmpl_id": 1001,
                "code": "OUK-BT103-BLACK",
                "barcode": "6950376707031",
                "name": "OUKITEL BT103 Smartwatch Black",
                "list_price": 250000.0,
                "stock": 15.0,
            },
            {
                "id": 102,
                "tmpl_id": 1002,
                "code": "XIA-REDMI-N13-BLU",
                "barcode": "6941812753018",
                "name": "Xiaomi Redmi Note 13 8GB 256GB Azul",
                "list_price": 1350000.0,
                "stock": 8.0,
            },
            {
                "id": 103,
                "tmpl_id": 1003,
                "code": "RED-K552-RGB",
                "barcode": "6950376709999",
                "name": "Teclado Mecanico Redragon Kumara K552 RGB Switch Red",
                "list_price": 320000.0,
                "stock": 25.0,
            },
            {
                "id": 104,
                "tmpl_id": 1004,
                "code": "CAB-USB-C-1M",
                "barcode": "7840001002003",
                "name": "Cable USB Type C a Type C Carga Rapida 1 Metro",
                "list_price": 35000.0,
                "stock": 120.0,
            },
            {
                "id": 105,
                "tmpl_id": 1005,
                "code": "1002706",
                "barcode": "",
                "name": "Funda Protectora Silicona Transparente Redmi Note 13",
                "list_price": 45000.0,
                "stock": 50.0,
            },
        ]
        self.index = CatalogHashIndex(ttl_seconds=3600)
        self.index.load(self.sample_products)

    def test_normalization_utilities(self):
        self.assertEqual(normalize_text("Café con Leche!"), "cafe con leche!")
        self.assertEqual(normalize_code(" ouk - bt103 _ black / "), "OUKBT103BLACK")
        self.assertIn("redmi", tokenize("Xiaomi Redmi Note 13"))
        self.assertIn("note", tokenize("Xiaomi Redmi Note 13"))
        self.assertIn("13", tokenize("Xiaomi Redmi Note 13"))

    def test_exact_lookup_by_code(self):
        # Exact code
        item = self.index.lookup_exact("OUK-BT103-BLACK")
        self.assertIsNotNone(item)
        self.assertEqual(item["product_id"], 101)

        # Uppercase / lowercase normalized
        item_lower = self.index.lookup_exact("ouk-bt103-black")
        self.assertIsNotNone(item_lower)
        self.assertEqual(item_lower["product_id"], 101)

        # Code without dashes / spaces
        item_stripped = self.index.lookup_exact("OUKBT103BLACK")
        self.assertIsNotNone(item_stripped)
        self.assertEqual(item_stripped["product_id"], 101)

    def test_exact_lookup_by_barcode(self):
        item = self.index.lookup_exact("6950376707031")
        self.assertIsNotNone(item)
        self.assertEqual(item["code"], "OUK-BT103-BLACK")

        item_spaces = self.index.lookup_exact("6950 3767 07031")
        self.assertIsNotNone(item_spaces)
        self.assertEqual(item_spaces["product_id"], 101)

    def test_exact_lookup_by_numeric_id(self):
        item_id = self.index.lookup_exact("102")
        self.assertIsNotNone(item_id)
        self.assertEqual(item_id["name"], "Xiaomi Redmi Note 13 8GB 256GB Azul")

        item_tmpl = self.index.lookup_exact("1003")
        self.assertIsNotNone(item_tmpl)
        self.assertEqual(item_tmpl["code"], "RED-K552-RGB")

    def test_lookup_non_existent(self):
        self.assertIsNone(self.index.lookup_exact("NON-EXISTENT-SKU"))
        self.assertIsNone(self.index.lookup_exact(""))
        self.assertIsNone(self.index.lookup_exact("-"))

    def test_search_token_and_substring(self):
        # Empty query returns catalog subset
        all_items = self.index.search("", limit=10)
        self.assertEqual(len(all_items), 5)

        # Token search
        redmi_results = self.index.search("redmi note", limit=10)
        self.assertEqual(len(redmi_results), 2)
        tmpl_ids = [r["tmpl_id"] for r in redmi_results]
        self.assertIn(1002, tmpl_ids)
        self.assertIn(1005, tmpl_ids)

        # Single brand search
        oukitel_results = self.index.search("oukitel", limit=5)
        self.assertEqual(len(oukitel_results), 1)
        self.assertEqual(oukitel_results[0]["product_id"], 101)

        # Substring / partial keyword search
        teclado = self.index.search("teclado", limit=5)
        self.assertEqual(len(teclado), 1)
        self.assertEqual(teclado[0]["code"], "RED-K552-RGB")

    def test_bulk_search_smart_mode(self):
        queries = [
            "OUK-BT103-BLACK",            # Code exact
            "6941812753018",              # Barcode exact
            "Kumara K552",                # Name keywords (smart)
            "1002706",                    # Numeric code
            "ITEM-QUE-NO-EXISTE-XYZ",     # Missing
            "9999999999999",              # Missing barcode
        ]
        found, not_found, stats = self.index.bulk_search(queries, match_mode="smart")

        self.assertEqual(len(found), 4)
        self.assertEqual(len(not_found), 2)
        self.assertIn("ITEM-QUE-NO-EXISTE-XYZ", not_found)
        self.assertIn("9999999999999", not_found)

        self.assertEqual(stats["total_queries"], 6)
        self.assertEqual(stats["found_count"], 4)
        self.assertEqual(stats["not_found_count"], 2)
        self.assertGreater(stats["execution_time_ms"], 0.0)

    def test_bulk_search_exact_code_mode(self):
        queries = [
            "OUK-BT103-BLACK",       # Code
            "6941812753018",         # Barcode
            "Teclado Kumara",        # Name (should fail in exact_code mode)
        ]
        found, not_found, stats = self.index.bulk_search(queries, match_mode="exact_code")
        self.assertEqual(len(found), 2)
        self.assertIn("Teclado Kumara", not_found)

    def test_cache_expiration_and_validity(self):
        self.assertTrue(self.index.is_valid())

        # Simulate expired TTL
        self.index._loaded_at = datetime.now() - timedelta(seconds=5000)
        self.assertFalse(self.index.is_valid())

    def test_single_character_tokens(self):
        # Cable USB Type C has single-letter token 'c'
        c_results = self.index.search("type c", limit=5)
        self.assertGreaterEqual(len(c_results), 1)
        self.assertEqual(c_results[0]["code"], "CAB-USB-C-1M")

    def test_bulk_search_variants_with_same_tmpl_id(self):
        variant_products = [
            {
                "id": 501,
                "tmpl_id": 5000,
                "code": "TSHIRT-BLK-M",
                "barcode": "1111111111111",
                "name": "Remera Algodon Negra Talla M",
                "list_price": 50000.0,
            },
            {
                "id": 502,
                "tmpl_id": 5000,
                "code": "TSHIRT-BLK-L",
                "barcode": "2222222222222",
                "name": "Remera Algodon Negra Talla L",
                "list_price": 50000.0,
            },
        ]
        self.index.load(variant_products)
        found, not_found, stats = self.index.bulk_search(["TSHIRT-BLK-M", "TSHIRT-BLK-L"], match_mode="exact_code")
        self.assertEqual(len(found), 2)
        self.assertEqual(len(not_found), 0)
        self.assertEqual(stats["total_queries"], 2)
        self.assertEqual(stats["found_count"], 2)
        self.assertEqual(stats["unique_found_count"], 2)

    def test_bulk_search_stats_consistency(self):
        queries = ["OUK-BT103-BLACK", "OUK-BT103-BLACK", "6941812753018", "UNKNOWN-1", "UNKNOWN-2"]
        found, not_found, stats = self.index.bulk_search(queries, match_mode="smart")
        self.assertEqual(stats["found_count"] + stats["not_found_count"], stats["total_queries"])
        self.assertEqual(stats["total_queries"], 4) # deduplicated clean queries
        self.assertEqual(stats["found_count"], 2)
        self.assertEqual(stats["not_found_count"], 2)

    def test_file_service_compare_compatibility(self):
        import pandas as pd
        from src.services.file_service import FileService

        _, code_idx, barcode_idx = self.index.get_indices()
        df = pd.DataFrame([
            {"codigo": "OUK-BT103-BLACK", "barcode": "", "precio": "270000"},
            {"codigo": "", "barcode": "6941812753018", "precio": "1400000"},
            {"codigo": "INEXISTENTE", "barcode": "", "precio": "10000"},
        ])
        col_mapping = {"code_col": "codigo", "barcode_col": "barcode", "price_col": "precio"}
        to_update, unchanged, not_found, all_audit = FileService.compare_with_odoo(
            df, col_mapping, code_idx, barcode_idx
        )
        self.assertEqual(len(to_update), 2)
        self.assertEqual(len(not_found), 1)
        self.assertEqual(len(all_audit), 3)
        self.assertEqual(all_audit[0]["product_id"], 101)
        self.assertEqual(all_audit[0]["tmpl_id"], 1001)

    def test_get_indices_compatibility(self):
        products, code_idx, barcode_idx = self.index.get_indices()
        self.assertEqual(len(products), 5)
        self.assertIn("OUK-BT103-BLACK", code_idx)
        self.assertIn("6950376707031", barcode_idx)

    def test_get_stats(self):
        stats = self.index.get_stats()
        self.assertTrue(stats["is_loaded"])
        self.assertEqual(stats["item_count"], 5)
        self.assertGreater(stats["code_keys"], 0)
        self.assertGreater(stats["barcode_keys"], 0)

    def test_dirty_and_null_inputs(self):
        # lookup_exact rejects dirty values
        self.assertIsNone(self.index.lookup_exact("None"))
        self.assertIsNone(self.index.lookup_exact("null"))
        self.assertIsNone(self.index.lookup_exact("nan"))
        self.assertIsNone(self.index.lookup_exact(None))

        # bulk_search filters dirty queries without polluting not_found
        found, not_found, stats = self.index.bulk_search(
            ["OUK-BT103-BLACK", "None", "null", "nan", "-", "--", ""],
            match_mode="smart",
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(len(not_found), 0)
        self.assertEqual(stats["total_queries"], 1)

    def test_barcode_substring_and_anti_false_positive(self):
        # Barcode substring matches corresponding product
        barcode_sub = self.index.search("7840001002", limit=5)
        self.assertGreaterEqual(len(barcode_sub), 1)
        self.assertEqual(barcode_sub[0]["product_id"], 104)

        # Non-existent query terms return empty and do not match random catalog items
        self.assertEqual(len(self.index.search("ITEM-QUE-NO-EXISTE-XYZ")), 0)
        self.assertEqual(len(self.index.search("UNKNOWN-1")), 0)
        self.assertEqual(len(self.index.search("9999999999999")), 0)


if __name__ == "__main__":
    unittest.main()
