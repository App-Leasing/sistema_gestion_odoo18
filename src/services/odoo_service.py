import xmlrpc.client
from threading import RLock
from typing import Dict, List, Optional, Tuple, Any

from src.core.config import settings
from src.core.logger import logger
from src.services.hash_index import CatalogHashIndex


class OdooService:
    """Servicio de integración y operaciones XML-RPC contra Odoo 18."""

    def __init__(
        self,
        url: Optional[str] = None,
        db: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.url = (url or settings.ODOO_URL).rstrip("/")
        self.db = db or settings.ODOO_DB
        self.username = username or settings.ODOO_USERNAME
        self.password = password or settings.ODOO_PASSWORD
        self.uid: Optional[int] = None
        self._common: Optional[xmlrpc.client.ServerProxy] = None
        self._models: Optional[xmlrpc.client.ServerProxy] = None
        # Motor en memoria de Tabla Hash e Índices Invertidos
        self.catalog_index = CatalogHashIndex()
        self._catalog_lock = RLock()


    def connect(self) -> int:
        """Autentica contra Odoo mediante XML-RPC y obtiene el UID."""
        try:
            self._common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common", allow_none=True
            )
            self.uid = self._common.authenticate(
                self.db, self.username, self.password, {}
            )
            if not self.uid:
                raise ConnectionError(
                    "Credenciales inválidas en Odoo (UID retornado vacío)."
                )
            self._models = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object", allow_none=True
            )
            logger.info(
                f"Conectado exitosamente a Odoo: {self.url} (DB: {self.db}, UID: {self.uid})"
            )
            return self.uid
        except Exception as e:
            logger.error(f"Error al conectar con Odoo XML-RPC: {e}")
            raise

    def _ensure_connected(self) -> None:
        """Asegura que la conexión y modelos estén listos."""
        if not self._models or not self.uid:
            self.connect()

    def execute_kw(
        self,
        model: str,
        method: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        retries: int = 2,
    ) -> Any:
        """
        Ejecuta un método XML-RPC en un modelo de Odoo con conexión automática y
        tolerancia a la suspensión (Idle / contenedor dormido) de entornos Odoo.sh.
        """
        args = args or []
        kwargs = kwargs or {}
        for attempt in range(retries + 1):
            try:
                self._ensure_connected()
                return self._models.execute_kw(
                    self.db, self.uid, self.password, model, method, args, kwargs
                )
            except Exception as e:
                err_str = str(e).lower()
                is_idle_or_conn = any(
                    w in err_str for w in ["idle", "connection", "timeout", "refused", "reset", "broken", "503"]
                )
                if attempt < retries and is_idle_or_conn:
                    logger.warning(
                        f"Instancia de Odoo suspendida ({e}) al consultar {model}.{method}. "
                        f"Despertando contenedor Staging (Intento {attempt + 1}/{retries})..."
                    )
                    import time
                    time.sleep(2.0)
                    self._models = None
                    self.uid = None
                    continue
                raise

    def fetch_catalog(
        self, active_only: bool = True, company_id: Optional[int] = None
    ) -> Tuple[
        List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]
    ]:
        """
        Descarga el catálogo de productos filtrado por empresa en Batch Read
        y construye índices en memoria por Referencia Interna (default_code) y Código de Barras (barcode).
        """
        self._ensure_connected()
        target_company = company_id if company_id is not None else settings.ODOO_COMPANY_ID

        domain: List[Any] = []
        if active_only:
            domain.append(["active", "=", True])
        if target_company and target_company > 0:
            co_filter = ["|", ["company_id", "=", False], ["company_id", "=", target_company]]
            domain = ["&"] + domain + [co_filter] if domain else [co_filter]

        fields = [
            "id",
            "name",
            "default_code",
            "barcode",
            "list_price",
            "product_tmpl_id",
            "qty_available",
            "active",
        ]

        logger.info("Obteniendo catálogo de productos desde Odoo en Batch Read...")
        try:
            products = self._models.execute_kw(
                self.db,
                self.uid,
                self.password,
                "product.product",
                "search_read",
                [domain],
                {"fields": fields},
            )
        except Exception as e:
            logger.warning(
                f"Error al solicitar catálogo con qty_available ({e}), reintentando sin stock..."
            )
            fields_no_stock = [f for f in fields if f != "qty_available"]
            products = self._models.execute_kw(
                self.db,
                self.uid,
                self.password,
                "product.product",
                "search_read",
                [domain],
                {"fields": fields_no_stock},
            )

        code_index: Dict[str, Dict[str, Any]] = {}
        barcode_index: Dict[str, Dict[str, Any]] = {}

        for p in products:
            code = (p.get("default_code") or "").strip()
            barcode = (p.get("barcode") or "").strip()

            if code and code not in ["-", "--"]:
                if code not in code_index:
                    code_index[code] = p
                code_clean = code.upper().replace(" ", "")
                if code_clean not in code_index:
                    code_index[code_clean] = p

            if barcode and barcode not in ["-", "--"]:
                if barcode not in barcode_index:
                    barcode_index[barcode] = p
                barcode_clean = barcode.upper().replace(" ", "")
                if barcode_clean not in barcode_index:
                    barcode_index[barcode_clean] = p

        logger.info(
            f"Catálogo cargado: {len(products)} productos. "
            f"Índices creados ({len(code_index)} refs, {len(barcode_index)} barcodes)."
        )
        # Poblamos la Tabla Hash en memoria para búsquedas instantáneas
        self.catalog_index.load(products)
        return products, code_index, barcode_index

    def ensure_catalog_cache(self, force: bool = False, company_id: Optional[int] = None) -> None:
        """Asegura que el catálogo de productos esté cargado en la tabla hash en memoria de forma thread-safe."""
        if force or not self.catalog_index.is_valid():
            with self._catalog_lock:
                if force or not self.catalog_index.is_valid():
                    logger.info("Cargando catálogo en caché de Tablas Hash desde Odoo...")
                    self.fetch_catalog(active_only=True, company_id=company_id)

    def search_inventory(
        self,
        query: str = "",
        limit: int = 100,
        use_cache: bool = True,
        force_refresh: bool = False,
        company_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Busca productos en el inventario por Referencia, Código de barras o Nombre.
        Por defecto utiliza la Tabla Hash en memoria para respuesta sub-milisegundo.
        """
        if use_cache:
            try:
                self.ensure_catalog_cache(force=force_refresh, company_id=company_id)
                return self.catalog_index.search(query, limit=limit)
            except Exception as e:
                logger.warning(
                    f"Fallo al buscar en tabla hash en memoria ({e}), recurriendo a Odoo XML-RPC directo..."
                )

        self._ensure_connected()

        target_company = company_id if company_id is not None else settings.ODOO_COMPANY_ID
        query_clean = query.strip()
        base_domain: List[Any] = [["active", "=", True]]
        if target_company and target_company > 0:
            base_domain = ["&", ["active", "=", True], "|", ["company_id", "=", False], ["company_id", "=", target_company]]

        if not query_clean:
            domain = base_domain
        else:
            search_clause = [
                "|",
                ["name", "ilike", query_clean],
                "|",
                ["default_code", "ilike", query_clean],
                ["barcode", "ilike", query_clean],
            ]
            domain = ["&"] + base_domain + [search_clause]

        products = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.product",
            "search_read",
            [domain],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "barcode",
                    "list_price",
                    "product_tmpl_id",
                    "qty_available",
                ],
                "limit": limit if limit > 0 else 2000,
            },
        )

        formatted = []
        for p in products:
            tmpl_id = (
                p["product_tmpl_id"][0]
                if isinstance(p.get("product_tmpl_id"), (list, tuple))
                else p["id"]
            )
            code = p.get("default_code") or ""
            if code in ["-", "--"]:
                code = ""
            barcode = p.get("barcode") or ""
            if barcode in ["-", "--"]:
                barcode = ""

            formatted.append(
                {
                    "product_id": p["id"],
                    "tmpl_id": tmpl_id,
                    "code": code,
                    "barcode": barcode,
                    "name": p["name"],
                    "list_price": float(p.get("list_price") or 0.0),
                    "stock": float(p.get("qty_available") or 0.0),
                }
            )
        return formatted

    def bulk_search_inventory(
        self,
        queries: List[str],
        match_mode: str = "smart",
        force_refresh: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
        """
        Búsqueda masiva ultra-eficiente utilizando la tabla hash en memoria.
        Devuelve: (items_encontrados, queries_no_encontrados, stats).
        """
        self.ensure_catalog_cache(force=force_refresh)
        return self.catalog_index.bulk_search(queries, match_mode=match_mode)

    def get_products_by_codes(
        self, codes: List[str], use_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """Busca una lista de códigos o barcodes pegados por el usuario."""
        if use_cache:
            try:
                items, _, _ = self.bulk_search_inventory(codes, match_mode="exact_code")
                return items
            except Exception as e:
                logger.warning(
                    f"Fallo en bulk search por tabla hash ({e}), recurriendo a Odoo XML-RPC..."
                )

        self._ensure_connected()

        clean_codes = [c.strip() for c in codes if c and c.strip()]
        if not clean_codes:
            return []

        domain = [
            "&",
            ["active", "=", True],
            "|",
            ["default_code", "in", clean_codes],
            ["barcode", "in", clean_codes],
        ]

        products = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.product",
            "search_read",
            [domain],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "barcode",
                    "list_price",
                    "product_tmpl_id",
                    "qty_available",
                ],
                "limit": len(clean_codes) * 2,
            },
        )

        formatted = []
        for p in products:
            tmpl_id = (
                p["product_tmpl_id"][0]
                if isinstance(p.get("product_tmpl_id"), (list, tuple))
                else p["id"]
            )
            code = p.get("default_code") or ""
            if code in ["-", "--"]:
                code = ""
            barcode = p.get("barcode") or ""
            if barcode in ["-", "--"]:
                barcode = ""

            formatted.append(
                {
                    "product_id": p["id"],
                    "tmpl_id": tmpl_id,
                    "code": code,
                    "barcode": barcode,
                    "name": p["name"],
                    "list_price": float(p.get("list_price") or 0.0),
                    "stock": float(p.get("qty_available") or 0.0),
                }
            )
        return formatted

    def get_catalog_indices(
        self, force_refresh: bool = False
    ) -> Tuple[
        List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]
    ]:
        """Devuelve productos e índices hash en memoria evitando llamadas XML-RPC redundantes."""
        self.ensure_catalog_cache(force=force_refresh)
        return self.catalog_index.get_indices()

    def get_cache_stats(self) -> Dict[str, Any]:
        """Devuelve el estado y métricas de la tabla hash en memoria."""
        return self.catalog_index.get_stats()

    def refresh_catalog_cache(self) -> Dict[str, Any]:
        """Fuerza la recarga del catálogo desde Odoo y reconstrucción de la tabla hash."""
        self.fetch_catalog(active_only=True)
        return self.catalog_index.get_stats()


    def get_companies(self) -> List[Dict[str, Any]]:
        """Obtiene las empresas activas en Odoo."""
        try:
            return self.execute_kw(
                "res.company",
                "search_read",
                [[["active", "=", True]]],
                {"fields": ["id", "name", "currency_id"], "order": "id asc"},
            )
        except Exception as e:
            logger.warning(f"Error obteniendo empresas ({e}), usando fallback.")
            return [{"id": 1, "name": "Mergal SA"}]

    def get_pricelists(self, company_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Obtiene las listas de precios activas configuradas en Odoo para una empresa."""
        target_company = company_id if company_id is not None else settings.ODOO_COMPANY_ID

        domain: List[Any] = [["active", "=", True]]
        if target_company and target_company > 0:
            domain = ["&", ["active", "=", True], "|", ["company_id", "=", False], ["company_id", "=", target_company]]

        pricelists = self.execute_kw(
            "product.pricelist",
            "search_read",
            [domain],
            {
                "fields": [
                    "id",
                    "name",
                    "currency_id",
                    "company_id",
                    "item_ids",
                    "active",
                ],
                "order": "name asc",
            },
        )
        for pl in pricelists:
            pl["item_count"] = len(pl.get("item_ids") or [])
        return pricelists

    def get_pricelist_products(self, pricelist_id: int) -> List[Dict[str, Any]]:
        """Obtiene todos los productos asignados a una Lista de Precios existente."""
        self._ensure_connected()

        rules = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.pricelist.item",
            "search_read",
            [[["pricelist_id", "=", pricelist_id]]],
            {
                "fields": [
                    "id",
                    "product_tmpl_id",
                    "product_id",
                    "fixed_price",
                    "compute_price",
                    "applied_on",
                ]
            },
        )

        tmpl_ids = []
        for r in rules:
            tmpl = r.get("product_tmpl_id")
            if tmpl and isinstance(tmpl, (list, tuple)):
                tmpl_ids.append(tmpl[0])

        if not tmpl_ids:
            return []

        tmpls = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.template",
            "search_read",
            [[["id", "in", list(set(tmpl_ids))]]],
            {
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "barcode",
                    "list_price",
                    "product_variant_id",
                ]
            },
        )
        tmpl_map = {t["id"]: t for t in tmpls}

        formatted = []
        for r in rules:
            tmpl_info = r.get("product_tmpl_id")
            if not tmpl_info:
                continue
            tmpl_id = tmpl_info[0]
            tmpl_data = tmpl_map.get(tmpl_id, {})
            code = tmpl_data.get("default_code") or ""
            if code in ["-", "--"]:
                code = ""
            barcode = tmpl_data.get("barcode") or ""
            if barcode in ["-", "--"]:
                barcode = ""

            prod_var = tmpl_data.get("product_variant_id")
            prod_id = (
                prod_var[0] if isinstance(prod_var, (list, tuple)) else tmpl_id
            )

            rule_price = float(r.get("fixed_price") or 0.0)
            base_price = float(tmpl_data.get("list_price") or 0.0)

            formatted.append(
                {
                    "product_id": prod_id,
                    "tmpl_id": tmpl_id,
                    "code": code,
                    "barcode": barcode,
                    "name": tmpl_data.get("name") or tmpl_info[1],
                    "list_price": base_price,
                    "pricelist_price": rule_price,
                    "stock": 0.0,
                }
            )
        return formatted

    def get_currencies(self) -> List[Dict[str, Any]]:
        """Obtiene las monedas activas en Odoo con símbolos limpios."""
        try:
            currencies = self.execute_kw(
                "res.currency",
                "search_read",
                [[["active", "=", True]]],
                {"fields": ["id", "name", "symbol"]},
            )
            res = []
            for c in currencies:
                sym = c.get("symbol") or ""
                if sym == "\u20b2" or "₲" in sym:
                    sym = "Gs"
                res.append(
                    {
                        "id": c["id"],
                        "name": c.get("name") or "",
                        "symbol": sym,
                    }
                )
            # Priorizar PYG y USD al principio
            res.sort(
                key=lambda x: (
                    0 if x["name"] == "PYG" else (1 if x["name"] == "USD" else 2),
                    x["name"],
                )
            )
            return res
        except Exception as e:
            logger.error(f"Error al obtener monedas: {e}")
            return [
                {"id": 157, "name": "PYG", "symbol": "Gs"},
                {"id": 1, "name": "USD", "symbol": "$"},
            ]

    def create_pricelist(
        self, name: str, currency_id: int, company_id: int = 1
    ) -> int:
        """Crea una nueva Lista de Precios en Odoo (product.pricelist)."""
        self._ensure_connected()

        vals = {
            "name": name.strip(),
            "currency_id": currency_id,
            "company_id": company_id,
        }
        new_id = self._models.execute_kw(
            self.db, self.uid, self.password, "product.pricelist", "create", [vals]
        )
        logger.info(f"Nueva Lista de Precios creada: '{name}' (ID: {new_id})")
        return new_id

    def update_product_price(self, tmpl_id: int, new_price: float) -> bool:
        """Actualiza el list_price en product.template para un registro específico."""
        self._ensure_connected()

        result = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.template",
            "write",
            [[tmpl_id], {"list_price": float(new_price)}],
        )
        return bool(result)

    def sync_pricelist_items(
        self,
        pricelist_id: int,
        items: List[Dict[str, Any]],
        update_base_price: bool = False,
        company_id: int = 1,
        on_progress=None,
    ) -> Dict[str, Any]:
        """
        Sincroniza productos en la Lista de Precios especificada.
        Actualiza reglas existentes o crea nuevas (product.pricelist.item).
        Opcionalmente actualiza list_price base del producto.
        """
        if not pricelist_id:
            raise ValueError(
                "pricelist_id es obligatorio para sincronizar items."
            )

        self._ensure_connected()

        # Obtener reglas existentes en esta lista para actualizar sin duplicar
        existing_items = self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            "product.pricelist.item",
            "search_read",
            [[["pricelist_id", "=", pricelist_id]]],
            {
                "fields": [
                    "id",
                    "product_tmpl_id",
                    "product_id",
                    "fixed_price",
                    "applied_on",
                ]
            },
        )

        tmpl_rule_map: Dict[int, int] = {}
        for rule in existing_items:
            tmpl = rule.get("product_tmpl_id")
            if tmpl and isinstance(tmpl, (list, tuple)):
                tmpl_rule_map[tmpl[0]] = rule["id"]

        created_count = 0
        updated_count = 0
        errors = []
        total = len(items)

        for idx, item in enumerate(items, start=1):
            tmpl_id = item["tmpl_id"]
            new_price = float(item["new_price"])
            try:
                if tmpl_id in tmpl_rule_map:
                    rule_id = tmpl_rule_map[tmpl_id]
                    self._models.execute_kw(
                        self.db,
                        self.uid,
                        self.password,
                        "product.pricelist.item",
                        "write",
                        [
                            [rule_id],
                            {"fixed_price": new_price, "compute_price": "fixed"},
                        ],
                    )
                    updated_count += 1
                else:
                    new_rule_vals = {
                        "pricelist_id": pricelist_id,
                        "applied_on": "1_product",
                        "product_tmpl_id": tmpl_id,
                        "compute_price": "fixed",
                        "fixed_price": new_price,
                        "base": "list_price",
                        "min_quantity": 0.0,
                        "company_id": company_id,
                    }
                    new_rule_id = self._models.execute_kw(
                        self.db,
                        self.uid,
                        self.password,
                        "product.pricelist.item",
                        "create",
                        [new_rule_vals],
                    )
                    tmpl_rule_map[tmpl_id] = new_rule_id
                    created_count += 1

                if update_base_price:
                    self.update_product_price(tmpl_id, new_price)

            except Exception as e:
                errors.append(
                    {
                        "tmpl_id": tmpl_id,
                        "name": item.get("name"),
                        "error": str(e),
                    }
                )

            if on_progress:
                on_progress(idx, total)

        return {
            "pricelist_id": pricelist_id,
            "created_count": created_count,
            "updated_count": updated_count,
            "error_count": len(errors),
            "errors": errors,
        }

    def write_batch(
        self, updates: List[Dict[str, Any]], on_progress=None
    ) -> List[Dict[str, Any]]:
        """Actualización masiva de precios base en product.template para el CLI."""
        self._ensure_connected()

        results = []
        total = len(updates)

        for idx, item in enumerate(updates, start=1):
            tmpl_id = item["tmpl_id"]
            new_price = item["new_price"]
            try:
                success = self.update_product_price(tmpl_id, new_price)
                if success:
                    item["status"] = "ACTUALIZADO"
                    item["error"] = None
                else:
                    item["status"] = "FALLO_WRITE"
                    item["error"] = "Odoo retornó False en write"
            except Exception as e:
                item["status"] = "ERROR_EXCEPTION"
                item["error"] = str(e)

            results.append(item)
            if on_progress:
                on_progress(idx, total, item)

        return results
