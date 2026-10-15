import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from src.core.config import settings
from src.core.logger import logger
from src.models.schemas import (
    SyncRequest,
    SyncResponse,
    CodesRequest,
    StatusResponse,
    BulkSearchRequest,
    BulkSearchResponse,
    BulkSearchStats,
    CacheStatusResponse,
    CostLineIn,
    LandedCostIn,
    PreflightIn,
    SanitizeIn,
    ExportApprovalExcelIn,
    MatchTableIn,
)
from src.services.odoo_service import OdooService
from src.services.file_service import FileService
from src.services.catalog_matcher import CatalogMatcher
from src.services.landed_cost_service import (
    LandedCostService,
    LandedCostPayload,
    CostLinePayload,
)


router = APIRouter(prefix="/api", tags=["Odoo Integration"])
odoo_service = OdooService()
landed_cost_service = LandedCostService(odoo_service)


@router.get("/status", response_model=StatusResponse)
def get_status():
    """Valida la conexión con Odoo y devuelve la información del entorno."""
    try:
        uid = odoo_service.connect()
        return StatusResponse(
            status="online",
            odoo_url=settings.ODOO_URL,
            database=settings.ODOO_DB,
            username=settings.ODOO_USERNAME,
            uid=uid,
        )
    except Exception as e:
        logger.error(f"Fallo en comprobación de status: {e}")
        return StatusResponse(
            status="error",
            odoo_url=settings.ODOO_URL,
            database=settings.ODOO_DB,
            message=str(e),
        )


@router.get("/pricelists")
def list_pricelists():
    """Retorna las Listas de Precios existentes en Odoo."""
    try:
        return odoo_service.get_pricelists()
    except Exception as e:
        logger.error(f"Error listando listas de precios: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/currencies")
def list_currencies():
    """Retorna las monedas activas en Odoo con símbolos limpios."""
    try:
        return odoo_service.get_currencies()
    except Exception as e:
        logger.error(f"Error obteniendo monedas: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/inventory/search")
def search_inventory(
    q: str = "",
    limit: int = 100,
    use_cache: bool = True,
    refresh: bool = False,
):
    """
    Busca productos en el inventario utilizando la Tabla Hash en memoria o Odoo directo.
    Respuesta sub-milisegundo gracias al motor hash indexado.
    """
    try:
        return odoo_service.search_inventory(
            q, limit=limit, use_cache=use_cache, force_refresh=refresh
        )
    except Exception as e:
        logger.error(f"Error buscando en inventario: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inventory/bulk-search", response_model=BulkSearchResponse)
def bulk_search_inventory(req: BulkSearchRequest):
    """
    Búsqueda masiva ultrarrápida utilizando la Tabla Hash en memoria.
    Resuelve listas completas de productos (códigos, barcodes o nombres) en pocos milisegundos.
    """
    try:
        items, not_found, stats = odoo_service.bulk_search_inventory(
            queries=req.queries,
            match_mode=req.match_mode,
            force_refresh=req.refresh_cache,
        )
        return BulkSearchResponse(
            items=items,
            not_found=not_found,
            stats=BulkSearchStats(**stats),
        )
    except Exception as e:
        logger.error(f"Error en búsqueda masiva por tabla hash: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/inventory/cache/status", response_model=CacheStatusResponse)
def get_cache_status():
    """Devuelve las métricas y estado en memoria de la Tabla Hash del catálogo."""
    try:
        stats = odoo_service.get_cache_stats()
        return CacheStatusResponse(**stats)
    except Exception as e:
        logger.error(f"Error obteniendo estado de la tabla hash: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inventory/cache/refresh", response_model=CacheStatusResponse)
def refresh_cache():
    """Fuerza la recarga del catálogo de Odoo y reconstrucción de la Tabla Hash."""
    try:
        stats = odoo_service.refresh_catalog_cache()
        return CacheStatusResponse(**stats)
    except Exception as e:
        logger.error(f"Error refrescando tabla hash del catálogo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inventory/by-codes")
def search_by_codes(req: CodesRequest):
    """Busca masivamente productos por lista de códigos o barcodes pegados usando la tabla hash."""
    try:
        return odoo_service.get_products_by_codes(req.codes)
    except Exception as e:
        logger.error(f"Error buscando productos por códigos: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pricelists/{pricelist_id}/products")
def get_pricelist_products(pricelist_id: int):
    """Obtiene todos los productos asignados a una Lista de Precios existente."""
    try:
        return odoo_service.get_pricelist_products(pricelist_id)
    except Exception as e:
        logger.error(f"Error obteniendo productos de lista {pricelist_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-file")
async def upload_file(
    file: UploadFile = File(...),
    col_code: Optional[str] = Form(None),
    col_barcode: Optional[str] = Form(None),
    col_price: Optional[str] = Form(None),
):
    """Recibe un archivo Excel (.xlsx, .xls) o CSV y cruza cada fila contra Odoo en memoria."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in [".xlsx", ".xls", ".csv", ".txt"]:
        raise HTTPException(
            status_code=400,
            detail="Formato de archivo no soportado. Suba un archivo .xlsx, .xls o .csv",
        )

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        custom_mapping = {}
        if col_code:
            custom_mapping["code_col"] = col_code
        if col_barcode:
            custom_mapping["barcode_col"] = col_barcode
        if col_price:
            custom_mapping["price_col"] = col_price

        df, detected_cols = FileService.load_price_file(tmp_path, custom_mapping)
        # Usamos los índices hash en memoria evitando re-descargas lentas
        _, code_idx, barcode_idx = odoo_service.get_catalog_indices()
        to_update, unchanged, not_found, all_audit = FileService.compare_with_odoo(
            df, detected_cols, code_idx, barcode_idx
        )


        return {
            "filename": file.filename,
            "total_rows": len(df),
            "detected_columns": detected_cols,
            "counts": {
                "to_update": len(to_update),
                "unchanged": len(unchanged),
                "not_found": len(not_found),
                "total": len(all_audit),
            },
            "items": all_audit,
        }
    except Exception as e:
        logger.error(f"Error procesando archivo subido: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.post("/sync-pricelist", response_model=SyncResponse)
def sync_pricelist(req: SyncRequest):
    """
    Crea o selecciona la Lista de Precios en Odoo y sincroniza los items seleccionados
    creando o actualizando las reglas (product.pricelist.item con fixed_price).
    """
    selected_items = [
        item.dict() for item in req.items if item.selected and item.tmpl_id
    ]
    if not selected_items:
        raise HTTPException(
            status_code=400,
            detail="No se seleccionó ningún producto válido para sincronizar.",
        )

    try:
        pricelist_id = req.pricelist_id
        pricelist_name = req.new_pricelist_name or ""

        # Manejo de Modo de Creación o Actualización
        if req.mode in ["new", "create_new"]:
            if not req.new_pricelist_name or not req.new_pricelist_name.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Debe especificar un nombre para la nueva Lista de Precios.",
                )
            pricelist_id = odoo_service.create_pricelist(
                name=req.new_pricelist_name.strip(),
                currency_id=req.currency_id,
                company_id=req.company_id,
            )
            pricelist_name = req.new_pricelist_name.strip()
        elif req.mode in ["existing", "update"]:
            if not pricelist_id:
                raise HTTPException(
                    status_code=400,
                    detail="Debe seleccionar una Lista de Precios existente.",
                )
            pls = odoo_service.get_pricelists()
            for pl in pls:
                if pl["id"] == pricelist_id:
                    pricelist_name = pl["name"]
                    break
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Modo no reconocido: '{req.mode}'. Debe ser 'new' o 'existing'.",
            )

        if not pricelist_id:
            raise HTTPException(
                status_code=500,
                detail="Error crítico: No se pudo obtener el ID de la Lista de Precios en Odoo.",
            )

        # Sincronizar items en la lista de precios
        sync_result = odoo_service.sync_pricelist_items(
            pricelist_id=pricelist_id,
            items=selected_items,
            update_base_price=req.update_base_price,
            company_id=req.company_id,
        )

        # Generar reporte Excel de auditoría
        audit_records = []
        for it in selected_items:
            audit_records.append(
                {
                    "code": it.get("code"),
                    "barcode": it.get("barcode"),
                    "name": it.get("name"),
                    "old_price": it.get("old_price"),
                    "new_price": it.get("new_price"),
                    "diff": (it.get("new_price") or 0) - (it.get("old_price") or 0),
                    "status": "SINCRONIZADO_EN_LISTA",
                    "notes": f"Lista ID: {pricelist_id} ({pricelist_name})",
                }
            )
        report_path = FileService.export_audit_excel(
            audit_records, settings.REPORTS_DIR
        )

        # Generar enlace directo al formulario de la lista en Odoo 18
        odoo_link = f"{settings.ODOO_URL}/web#id={pricelist_id}&cids={req.company_id}&model=product.pricelist&view_type=form"

        return SyncResponse(
            success=True,
            pricelist_id=pricelist_id,
            pricelist_name=pricelist_name,
            created_rules=sync_result["created_count"],
            updated_rules=sync_result["updated_count"],
            error_rules=sync_result["error_count"],
            errors=sync_result["errors"],
            odoo_link=odoo_link,
            report_file=report_path.name,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en sincronización de lista de precios: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download-report/{filename}")
def download_report(filename: str):
    """Descarga el reporte de auditoría en Excel."""
    file_path = settings.REPORTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Reporte no encontrado.")
    return FileResponse(
        file_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/export-approval-excel")
def export_approval_excel(req: ExportApprovalExcelIn):
    """Genera y descarga directamente una planilla ejecutiva de aprobación en Excel."""
    try:
        if not req.items:
            raise HTTPException(
                status_code=400, detail="No hay productos para exportar a la planilla."
            )
        out_file = FileService.generate_approval_excel(
            items=req.items,
            pricelist_name=req.pricelist_name or "Lista de Precios",
        )
        return FileResponse(
            out_file,
            filename=out_file.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generando planilla de aprobación: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/match-table")
def match_table(req: MatchTableIn):
    """
    Recibe texto copiado (tabuladores, saltos de línea, WhatsApp, Excel)
    y lo empareja inteligentemente contra el catálogo en memoria de Odoo.
    """
    try:
        raw_text = req.raw_text.strip()
        if not raw_text:
            return {"items": [], "not_found": [], "stats": {"total": 0, "matched": 0, "not_found": 0}}

        catalog, code_idx, barcode_idx = odoo_service.get_catalog_indices()

        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        matched_items = []
        not_found_list = []
        seen_tmpl_ids = set()

        for line in lines:
            parts = [p.strip() for p in re.split(r"[\t;,|]+", line) if p.strip()]
            if not parts:
                continue

            # El primer campo suele ser el modelo o código, el último suele ser el precio
            term = parts[0]
            price_val = 0.0
            color_hint = ""
            memory_hint = ""

            if len(parts) >= 2:
                # Tratar de encontrar el precio en el último o penúltimo elemento
                for p_candidate in reversed(parts[1:]):
                    pv = CatalogMatcher.clean_price(p_candidate)
                    if pv > 0:
                        price_val = pv
                        break

            # Buscar hints de color o memoria en los campos intermedios
            for p in parts[1:]:
                if re.match(r"^\d+\s*\+\s*\d+$", p) or p in ["1TB", "512GB", "256GB"]:
                    memory_hint = p
                elif p.upper() in CatalogMatcher.COLOR_MAP:
                    color_hint = p

            match = CatalogMatcher.match_item_to_catalog(
                search_term=term,
                catalog_products=catalog,
                code_index=code_idx,
                barcode_index=barcode_idx,
                color_hint=color_hint,
                memory_hint=memory_hint,
            )

            if match:
                raw_tmpl = match.get("product_tmpl_id")
                tmpl_id = (
                    raw_tmpl[0]
                    if isinstance(raw_tmpl, (list, tuple)) and len(raw_tmpl) > 0
                    else (match.get("tmpl_id") or match.get("id"))
                )
                if tmpl_id in seen_tmpl_ids:
                    continue
                seen_tmpl_ids.add(tmpl_id)

                old_p = float(match.get("list_price") or 0.0)
                new_p = price_val if price_val > 0 else old_p

                matched_items.append(
                    {
                        "product_id": match.get("id") or match.get("product_id"),
                        "tmpl_id": tmpl_id,
                        "code": match.get("default_code") or match.get("code") or "",
                        "barcode": match.get("barcode") or "",
                        "name": match.get("name") or "",
                        "old_price": old_p,
                        "new_price": new_p,
                        "diff": new_p - old_p,
                        "notes": "Matcheo Inteligente",
                        "selected": True,
                    }
                )
            else:
                not_found_list.append(term)

        return {
            "items": matched_items,
            "not_found": not_found_list,
            "stats": {
                "total": len(lines),
                "matched": len(matched_items),
                "not_found": len(not_found_list),
            },
        }
    except Exception as e:
        logger.error(f"Error en match_table: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------
# RUTAS DE COSTOS EN DESTINO Y SANEAMIENTO (LANDED COSTS)
# -------------------------------------------------------------

@router.get("/landed-cost/pickings")
def list_incoming_pickings(q: str = "", limit: int = 50):
    """Retorna los últimos albaranes de recepción finalizados (done) con soporte de búsqueda."""
    try:
        return landed_cost_service.get_incoming_pickings(q=q, limit=limit)
    except Exception as e:
        logger.error(f"Error listando recepciones: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/landed-cost/history")
def list_landed_costs_history(limit: int = 25):
    """Retorna el historial de Costes en Destino y su estado de consistencia."""
    try:
        return landed_cost_service.get_landed_costs_history(limit=limit)
    except Exception as e:
        logger.error(f"Error obteniendo historial de costes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/landed-cost/expense-products")
def list_expense_products():
    """Retorna los productos de servicio habilitados para Landed Costs en Odoo."""
    try:
        return odoo_service.execute_kw(
            "product.product",
            "search_read",
            [[["landed_cost_ok", "=", True]]],
            {"fields": ["id", "name", "default_code", "split_method_landed_cost"]},
        )
    except Exception as e:
        logger.error(f"Error listando productos de gasto: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@router.post("/landed-cost/preflight")
def preflight_picking(req: PreflightIn):
    """Valida los productos de una recepción y detecta pesos faltantes o en cero."""
    try:
        return landed_cost_service.preflight_check(
            picking_id=req.picking_id,
            product_weights=req.product_weights,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error en preflight check de recepción {req.picking_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/landed-cost/simulate")
def simulate_landed_cost(req: LandedCostIn):
    """Simula en memoria el prorrateo de costes adicionales sin escribir en Odoo."""
    try:
        cost_lines = [
            CostLinePayload(
                product_id=cl.product_id,
                name=cl.name,
                price_unit=cl.price_unit,
                split_method=cl.split_method,
                account_id=cl.account_id,
            )
            for cl in req.cost_lines
        ]
        payload = LandedCostPayload(
            picking_ids=req.picking_ids,
            peso_total_kg=req.peso_total_kg,
            tipo_cambio_gastos=req.tipo_cambio_gastos,
            tipo_cambio_usd=req.tipo_cambio_usd,
            cost_lines=cost_lines,
            cost_date=req.cost_date,
            product_weights=req.product_weights or {},
        )
        return landed_cost_service.simulate(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error en simulación de landed cost: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/landed-cost/create")
def create_landed_cost(req: LandedCostIn):
    """Crea y computa un nuevo registro stock.landed.cost en Odoo Staging."""
    try:
        cost_lines = [
            CostLinePayload(
                product_id=cl.product_id,
                name=cl.name,
                price_unit=cl.price_unit,
                split_method=cl.split_method,
                account_id=cl.account_id,
            )
            for cl in req.cost_lines
        ]
        payload = LandedCostPayload(
            picking_ids=req.picking_ids,
            peso_total_kg=req.peso_total_kg,
            tipo_cambio_gastos=req.tipo_cambio_gastos,
            tipo_cambio_usd=req.tipo_cambio_usd,
            cost_lines=cost_lines,
            cost_date=req.cost_date,
            product_weights=req.product_weights or {},
        )
        return landed_cost_service.create_and_compute(payload, auto_validate=req.auto_validate)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creando landed cost en Odoo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/landed-cost/analyze/{lc_id}")
def analyze_landed_cost_record(lc_id: int):
    """Audita un Landed Cost existente para detectar métodos erróneos ('equal') o variables 0."""
    try:
        return landed_cost_service.analyze(lc_id)
    except Exception as e:
        logger.error(f"Error analizando landed cost ID {lc_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/landed-cost/sanitize")
def sanitize_landed_cost(req: SanitizeIn):
    """Ejecuta el pipeline de saneamiento: analiza, crea nuevo LC limpio y revierte asiento viciado."""
    try:
        clean_cl = [
            CostLinePayload(
                product_id=cl.product_id,
                name=cl.name,
                price_unit=cl.price_unit,
                split_method=cl.split_method,
                account_id=cl.account_id,
            )
            for cl in req.clean_payload.cost_lines
        ]
        clean_payload = LandedCostPayload(
            picking_ids=req.clean_payload.picking_ids,
            peso_total_kg=req.clean_payload.peso_total_kg,
            tipo_cambio_gastos=req.clean_payload.tipo_cambio_gastos,
            tipo_cambio_usd=req.clean_payload.tipo_cambio_usd,
            cost_lines=clean_cl,
            cost_date=req.clean_payload.cost_date,
            product_weights=req.clean_payload.product_weights or {},
        )
        return landed_cost_service.sanitize(
            faulty_lc_id=req.faulty_lc_id,
            clean_payload=clean_payload,
            dry_run=req.dry_run,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error saneando landed cost ID {req.faulty_lc_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

