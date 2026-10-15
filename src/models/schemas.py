from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class SyncItem(BaseModel):
    """Representa un producto individual a sincronizar."""
    tmpl_id: int
    product_id: Optional[int] = None
    name: str
    code: Optional[str] = ""
    barcode: Optional[str] = ""
    old_price: Optional[float] = 0.0
    new_price: float
    selected: bool = True
    diff: Optional[float] = 0.0
    notes: Optional[str] = ""


class SyncRequest(BaseModel):
    """Solicitud de sincronización de lista de precios hacia Odoo."""
    mode: str = Field(..., description="Modo: 'new', 'create_new' o 'existing'")
    pricelist_id: Optional[int] = Field(None, description="ID de lista si mode es existing")
    new_pricelist_name: Optional[str] = Field(None, description="Nombre si mode es new")
    currency_id: int = Field(157, description="ID de moneda (157=PYG, 1=USD)")
    company_id: int = Field(1, description="ID de empresa (1=Mergal SA)")
    update_base_price: bool = Field(False, description="Si true, actualiza también list_price del producto")
    items: List[SyncItem]


class SyncResponse(BaseModel):
    """Respuesta del proceso de sincronización."""
    success: bool
    pricelist_id: int
    pricelist_name: str
    created_rules: int
    updated_rules: int
    error_rules: int
    errors: List[Dict[str, Any]] = []
    odoo_link: str
    report_file: Optional[str] = None


class CodesRequest(BaseModel):
    """Solicitud de búsqueda por lista de códigos o barcodes."""
    codes: List[str]


class ProductItem(BaseModel):
    """Esquema de producto recuperado de Odoo."""
    product_id: Optional[int] = None
    tmpl_id: Optional[int] = None
    code: Optional[str] = ""
    barcode: Optional[str] = ""
    name: str
    list_price: float = 0.0
    stock: float = 0.0


class StatusResponse(BaseModel):
    """Estado de conexión con Odoo."""
    status: str
    odoo_url: str
    database: str
    username: Optional[str] = None
    uid: Optional[int] = None
    message: Optional[str] = None


class BulkSearchRequest(BaseModel):
    """Solicitud de búsqueda masiva con lista de items."""
    queries: List[str]
    match_mode: str = Field("smart", description="'smart' (código/barcode/nombre) o 'exact_code'")
    refresh_cache: bool = Field(False, description="Si true, recarga el catálogo desde Odoo antes de buscar")


class BulkSearchStats(BaseModel):
    """Métricas y estadísticas de la búsqueda masiva con tabla hash."""
    total_queries: int
    found_count: int
    not_found_count: int
    execution_time_ms: float
    cache_items: int
    unique_found_count: Optional[int] = None


class BulkSearchResponse(BaseModel):
    """Respuesta completa de la búsqueda masiva."""
    items: List[ProductItem]
    not_found: List[str] = []
    stats: BulkSearchStats


class CacheStatusResponse(BaseModel):
    """Estado actual de la tabla hash en memoria."""
    is_loaded: bool
    item_count: int
    code_keys: Optional[int] = 0
    barcode_keys: Optional[int] = 0
    tokens_indexed: Optional[int] = 0
    loaded_at: Optional[str] = None
    age_seconds: Optional[float] = None
    ttl_seconds: Optional[int] = 1800


class CostLineIn(BaseModel):
    product_id: int
    name: str
    price_unit: float
    split_method: str = "by_weight"
    account_id: Optional[int] = None


class LandedCostIn(BaseModel):
    picking_ids: List[int]
    peso_total_kg: float
    tipo_cambio_gastos: float
    tipo_cambio_usd: float
    cost_lines: List[CostLineIn]
    cost_date: Optional[str] = None
    product_weights: Optional[Dict[str, float]] = None
    auto_validate: bool = False


class PreflightIn(BaseModel):
    picking_id: int
    product_weights: Optional[Dict[str, float]] = None


class SanitizeIn(BaseModel):
    faulty_lc_id: int
    clean_payload: LandedCostIn
    dry_run: bool = False


class ExportApprovalExcelIn(BaseModel):
    pricelist_name: Optional[str] = "Lista de Precios"
    items: List[Dict[str, Any]]


class MatchTableIn(BaseModel):
    raw_text: str
    target_pricelist_id: Optional[int] = None


