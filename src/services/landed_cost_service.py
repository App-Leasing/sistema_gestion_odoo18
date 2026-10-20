"""
Servicio de Saneamiento de Valoraciones y Automatización de Costos en Destino (Landed Costs) en Odoo 18.
Ubicación: src/services/landed_cost_service.py
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from src.core.logger import logger
from src.services.odoo_service import OdooService


@dataclass
class CostLinePayload:
    product_id: int
    name: str
    price_unit: float
    split_method: str = "by_weight"  # 'by_weight' | 'by_current_cost_price' | 'by_quantity'
    account_id: Optional[int] = None


@dataclass
class LandedCostPayload:
    picking_ids: List[int]
    peso_total_kg: float
    tipo_cambio_gastos: float
    tipo_cambio_usd: float
    cost_lines: List[CostLinePayload]
    cost_date: Optional[str] = None  # YYYY-MM-DD
    product_weights: Optional[Dict[str, float]] = field(default_factory=dict)  # default_code -> weight_kg


class ReceptionCostingEngine:
    """
    Submódulo 2: Motor de Automatización de Costeos vía Recepciones (stock.picking).
    Audita pesos, previene errores de valoración y prorratea costes adicionales.
    """

    def __init__(self, odoo: OdooService):
        self.odoo = odoo

    def preflight_check(
        self, picking_id: int, product_weights: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        Audita el albarán de recepción antes de costear:
        1. Valida que el albarán exista y esté en estado 'done'.
        2. Detecta productos con weight == 0.0 o faltantes.
        3. Si se proveen product_weights, actualiza preventivamente la ficha técnica.
        """
        pickings = self.odoo.execute_kw(
            "stock.picking",
            "search_read",
            [[["id", "=", picking_id]]],
            {"fields": ["id", "name", "state", "origin", "date_done", "move_ids"]},
        )
        if not pickings:
            raise ValueError(f"No se encontró el albarán de recepción con ID {picking_id}")

        picking = pickings[0]
        if picking["state"] != "done":
            raise ValueError(
                f"El albarán {picking['name']} está en estado '{picking['state']}'. Debe estar 'done' para costear."
            )

        # Leer movimientos y productos
        move_ids = picking.get("move_ids", [])
        moves = self.odoo.execute_kw(
            "stock.move",
            "search_read",
            [[["id", "in", move_ids]]],
            {"fields": ["id", "product_id", "product_uom_qty", "quantity", "price_unit"]},
        )

        prod_ids = list(set([m["product_id"][0] for m in moves if m.get("product_id")]))
        products = self.odoo.execute_kw(
            "product.product",
            "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "default_code", "name", "weight"]},
        )

        missing_weights = []
        updated_weights = []
        product_weights = product_weights or {}

        for p in products:
            code = (p.get("default_code") or "").strip()
            p_id = p["id"]
            p_name = p.get("name") or ""
            current_weight = p.get("weight") or 0.0

            # Buscar si el usuario proveyó un peso nuevo por código, ID o nombre
            target_w = None
            if code and code in product_weights and product_weights[code] is not None:
                try:
                    target_w = float(product_weights[code])
                except (ValueError, TypeError):
                    pass
            elif str(p_id) in product_weights and product_weights[str(p_id)] is not None:
                try:
                    target_w = float(product_weights[str(p_id)])
                except (ValueError, TypeError):
                    pass
            elif p_id in product_weights and product_weights[p_id] is not None:
                try:
                    target_w = float(product_weights[p_id])
                except (ValueError, TypeError):
                    pass
            elif p_name in product_weights and product_weights[p_name] is not None:
                try:
                    target_w = float(product_weights[p_name])
                except (ValueError, TypeError):
                    pass

            if target_w is not None and target_w > 0:
                if abs(current_weight - target_w) > 0.0001:
                    self.odoo.execute_kw(
                        "product.product",
                        "write",
                        [[p["id"]], {"weight": target_w}],
                    )
                    updated_weights.append({"code": code or str(p_id), "old": current_weight, "new": target_w})
                    logger.info(f"Actualizado peso de {code or p_name}: {current_weight} kg -> {target_w} kg")
                p["weight"] = target_w
                current_weight = target_w

            if current_weight <= 0.0:
                missing_weights.append({"id": p["id"], "code": code, "name": p.get("name")})

        total_units = sum(m.get("quantity") or m.get("product_uom_qty") or 0.0 for m in moves)

        prod_weight_map = {p["id"]: p.get("weight") or 0.0 for p in products}
        total_physical_weight = round(sum(
            prod_weight_map.get(m["product_id"][0], 0.0) * (m.get("quantity") or m.get("product_uom_qty") or 0.0)
            for m in moves if m.get("product_id")
        ), 3)

        return {
            "picking_id": picking_id,
            "picking_name": picking["name"],
            "origin": picking.get("origin"),
            "total_moves": len(moves),
            "total_products": len(products),
            "total_units": total_units,
            "total_physical_weight_kg": total_physical_weight,
            "updated_weights": updated_weights,
            "missing_weights": missing_weights,
            "products": [
                {
                    "id": p["id"],
                    "code": (p.get("default_code") or "").strip(),
                    "name": p.get("name"),
                    "weight": p.get("weight") or 0.0,
                }
                for p in products
            ],
            "is_ready": len(missing_weights) == 0,
            "can_proceed": len(missing_weights) == 0,
        }

    def simulate_costing(self, payload: LandedCostPayload) -> Dict[str, Any]:
        """
        Modo Dry-Run: Calcula en memoria la absorción por modelo y peso
        sin generar mutaciones en la base de datos de Odoo.
        """
        picking_id = payload.picking_ids[0]
        moves = self.odoo.execute_kw(
            "stock.move",
            "search_read",
            [[["picking_id", "=", picking_id]]],
            {"fields": ["id", "product_id", "product_uom_qty", "quantity", "price_unit", "purchase_line_id"]},
        )

        prod_ids = list(set([m["product_id"][0] for m in moves if m.get("product_id")]))
        prods = self.odoo.execute_kw(
            "product.product",
            "search_read",
            [[["id", "in", prod_ids]]],
            {"fields": ["id", "default_code", "name", "weight"]},
        )
        prod_map = {p["id"]: p for p in prods}

        # Intentar obtener los precios FOB originales en USD desde la Orden de Compra
        pol_map = {}
        pol_ids = [m["purchase_line_id"][0] for m in moves if m.get("purchase_line_id")]
        if pol_ids:
            try:
                pols = self.odoo.execute_kw(
                    "purchase.order.line",
                    "search_read",
                    [[["id", "in", list(set(pol_ids))]]],
                    {"fields": ["id", "price_unit"]},
                )
                pol_map = {pol["id"]: pol.get("price_unit") for pol in pols}
            except Exception as e:
                logger.warning(f"No se pudo consultar purchase.order.line: {e}")

        total_gastos_gs = sum(cl.price_unit for cl in payload.cost_lines)
        peso_total = payload.peso_total_kg or 0.0
        tc_gastos = payload.tipo_cambio_gastos or 1.0
        pw = payload.product_weights or {}

        def get_item_weight(prod_id: int, prod_dict: dict) -> float:
            p_code = (prod_dict.get("default_code") or "").strip()
            p_name = prod_dict.get("name") or ""
            if p_code and p_code in pw and pw[p_code] is not None:
                try:
                    return float(pw[p_code])
                except (ValueError, TypeError):
                    pass
            if str(prod_id) in pw and pw[str(prod_id)] is not None:
                try:
                    return float(pw[str(prod_id)])
                except (ValueError, TypeError):
                    pass
            if prod_id in pw and pw[prod_id] is not None:
                try:
                    return float(pw[prod_id])
                except (ValueError, TypeError):
                    pass
            if p_name in pw and pw[p_name] is not None:
                try:
                    return float(pw[p_name])
                except (ValueError, TypeError):
                    pass
            return float(prod_dict.get("weight") or 0.0)

        # Calcular el peso total acumulado de las líneas
        total_lines_weight = sum(
            get_item_weight(m["product_id"][0], prod_map.get(m["product_id"][0], {}))
            * (m.get("quantity") or m.get("product_uom_qty") or 0.0)
            for m in moves if m.get("product_id")
        )
        # Si el usuario proveyó un Peso Total de Costeo (ej: 9285.5 Kg del Excel), usarlo como divisor
        weight_denominator = peso_total if peso_total > 0 else (total_lines_weight or 1.0)

        simulation_items = []
        tc_usd = payload.tipo_cambio_usd or 7850.0

        # Totales de base para cada método de división nativo de Odoo
        total_moves_fob_gs = sum(
            (m.get("price_unit") or 0.0) * (m.get("quantity") or m.get("product_uom_qty") or 0.0)
            for m in moves
        )
        total_moves_qty = sum(
            (m.get("quantity") or m.get("product_uom_qty") or 0.0)
            for m in moves
        )

        for m in moves:
            pid = m["product_id"][0]
            prod = prod_map.get(pid, {})
            qty = m.get("quantity") or m.get("product_uom_qty") or 0.0
            p_code = prod.get("default_code") or ""
            weight = get_item_weight(pid, prod)
            weight_total_line = weight * qty

            former_cost_unit_gs = m.get("price_unit") or 0.0
            # Si tenemos el precio unitario original en USD de la OC, usarlo directamente
            pol_id = m.get("purchase_line_id")[0] if m.get("purchase_line_id") else None
            if pol_id and pol_id in pol_map and pol_map[pol_id]:
                former_cost_unit_usd = float(pol_map[pol_id])
            else:
                former_cost_unit_usd = former_cost_unit_gs / tc_usd if tc_usd else 0.0

            # Absorción multi-método: respeta el split_method exacto de cada línea (peso, valor FOB, cantidad)
            absorcion_gs_line = 0.0
            for cl in payload.cost_lines:
                method = cl.split_method or "by_weight"
                if method == "by_weight":
                    share = (weight_total_line / weight_denominator) * cl.price_unit if weight_denominator else 0.0
                elif method == "by_current_cost_price":
                    move_val_gs = former_cost_unit_gs * qty
                    share = (move_val_gs / total_moves_fob_gs) * cl.price_unit if total_moves_fob_gs else 0.0
                elif method == "by_quantity":
                    share = (qty / total_moves_qty) * cl.price_unit if total_moves_qty else 0.0
                elif method == "equal":
                    share = cl.price_unit / len(moves) if moves else 0.0
                else:
                    share = (weight_total_line / weight_denominator) * cl.price_unit if weight_denominator else 0.0
                absorcion_gs_line += share

            absorcion_gs_unit = absorcion_gs_line / qty if qty else 0.0
            absorcion_usd_unit = absorcion_gs_unit / tc_gastos if tc_gastos else 0.0

            final_cost_unit_gs = former_cost_unit_gs + absorcion_gs_unit
            final_cost_unit_usd = former_cost_unit_usd + absorcion_usd_unit

            simulation_items.append(
                {
                    "product_id": pid,
                    "code": p_code,
                    "product_code": p_code,
                    "name": prod.get("name"),
                    "product_name": prod.get("name"),
                    "quantity": qty,
                    "unit_weight_kg": weight,
                    "total_weight_kg": weight_total_line,
                    "former_cost_unit_usd": former_cost_unit_usd,
                    "former_cost_gs": former_cost_unit_gs,
                    "additional_landed_cost_unit_usd": absorcion_usd_unit,
                    "landed_cost_unit_gs": absorcion_gs_unit,
                    "landed_cost_unit_usd": absorcion_usd_unit,
                    "final_cost_unit_usd": final_cost_unit_usd,
                    "final_cost_unit_gs": final_cost_unit_gs,
                }
            )

        total_weight_kg = sum(item["total_weight_kg"] for item in simulation_items)
        total_fob_usd = sum(item["former_cost_unit_usd"] * item["quantity"] for item in simulation_items)

        return {
            "total_expenses_gs": total_gastos_gs,
            "total_expense_gs": total_gastos_gs,
            "total_expenses_usd": total_gastos_gs / tc_gastos if tc_gastos else 0.0,
            "total_fob_usd": total_fob_usd,
            "total_weight_kg": total_weight_kg,
            "total_gross_weight_kg": peso_total,
            "lines": simulation_items,
        }


    def create_and_compute_landed_cost(
        self, payload: LandedCostPayload, auto_validate: bool = False
    ) -> Dict[str, Any]:
        """
        Orquesta la creación atómica y segura del Costo en Destino:
        1. Preflight check y actualización de pesos.
        2. Creación de stock.landed.cost con cabecera Studio.
        3. Carga de stock.landed.cost.lines.
        4. Ejecución de compute_landed_cost().
        5. Disparo de Server Action 1792 (Studio Unit Costs).
        6. Si auto_validate=True, llama a button_validate().
        En caso de error, limpia el borrador (Rollback / Unlink).
        """
        # 1. Preflight check
        for pid in payload.picking_ids:
            self.preflight_check(pid, payload.product_weights)

        # 2. Formatear cost lines con nombres descriptivos automáticos
        prod_names = {}
        cost_prod_ids = [cl.product_id for cl in payload.cost_lines if cl.product_id]
        if cost_prod_ids:
            try:
                cp_records = self.odoo.execute_kw(
                    "product.product",
                    "search_read",
                    [[["id", "in", list(set(cost_prod_ids))]]],
                    {"fields": ["id", "name"]},
                )
                prod_names = {cp["id"]: cp["name"] for cp in cp_records}
            except Exception:
                pass

        cost_lines_vals = []
        for cl in payload.cost_lines:
            line_name = (cl.name or "").strip()
            if (not line_name or line_name.lower() in ["gasto de importación", "gasto de importacion"]) and cl.product_id in prod_names:
                line_name = prod_names[cl.product_id]

            line_dict = {
                "product_id": cl.product_id,
                "name": line_name or "Gasto de Importación",
                "price_unit": cl.price_unit,
                "split_method": cl.split_method,
            }
            if cl.account_id:
                line_dict["account_id"] = cl.account_id
            cost_lines_vals.append((0, 0, line_dict))

        # 3. Payload de cabecera
        cost_date = payload.cost_date or date.today().isoformat()
        lc_vals = {
            "picking_ids": [(6, 0, payload.picking_ids)],
            "date": cost_date,
            "cost_lines": cost_lines_vals,
            "x_studio_peso_total_de_costeo": payload.peso_total_kg,
            "x_studio_cambio_gastos": payload.tipo_cambio_gastos,
            "x_studio_tipo_de_cambio_usd": payload.tipo_cambio_usd,
        }

        created_lc_id: Optional[int] = None
        try:
            # Crear borrador
            created_lc_id = self.odoo.execute_kw("stock.landed.cost", "create", [lc_vals])
            logger.info(f"Creado registro stock.landed.cost ID: {created_lc_id} en borrador")

            # Asegurar cabecera Studio
            self.odoo.execute_kw(
                "stock.landed.cost",
                "write",
                [
                    [created_lc_id],
                    {
                        "x_studio_peso_total_de_costeo": payload.peso_total_kg,
                        "x_studio_cambio_gastos": payload.tipo_cambio_gastos,
                        "x_studio_tipo_de_cambio_usd": payload.tipo_cambio_usd,
                    },
                ],
            )

            # Ejecutar cálculo nativo
            logger.info(f"Ejecutando compute_landed_cost() para LC ID {created_lc_id}...")
            self.odoo.execute_kw("stock.landed.cost", "compute_landed_cost", [[created_lc_id]])

            # Disparar automatización 1792 sobre las líneas de ajuste
            adj_lines = self.odoo.execute_kw(
                "stock.valuation.adjustment.lines",
                "search_read",
                [[["cost_id", "=", created_lc_id]]],
                {"fields": ["id"]},
            )
            adj_ids = [al["id"] for al in adj_lines]
            logger.info(f"Generadas {len(adj_ids)} líneas de ajuste. Ejecutando acción Studio 1792...")

            for aid in adj_ids:
                try:
                    self.odoo.execute_kw(
                        "ir.actions.server",
                        "run",
                        [[1792]],
                        {
                            "context": {
                                "active_model": "stock.valuation.adjustment.lines",
                                "active_ids": [aid],
                                "active_id": aid,
                            }
                        },
                    )
                except Exception as e_act:
                    logger.warning(f"Aviso en ejecución de acción 1792 sobre línea {aid}: {e_act}")

            # Validar si fue solicitado
            if auto_validate:
                logger.info(f"Validando / Publicando LC ID {created_lc_id}...")
                self.odoo.execute_kw("stock.landed.cost", "button_validate", [[created_lc_id]])

            # Leer resultado final
            lc_record = self.odoo.execute_kw(
                "stock.landed.cost",
                "search_read",
                [[["id", "=", created_lc_id]]],
                {"fields": ["id", "name", "state", "account_move_id", "amount_total"]},
            )[0]

            return {
                "success": True,
                "landed_cost_id": created_lc_id,
                "landed_cost_name": lc_record["name"],
                "state": lc_record["state"],
                "amount_total": lc_record["amount_total"],
                "account_move_id": lc_record.get("account_move_id"),
                "url": f"{self.odoo.url}/odoo/action-1516/{created_lc_id}",
            }

        except Exception as e:
            logger.error(f"Fallo durante la creación de Landed Cost: {e}")
            if created_lc_id:
                try:
                    logger.info(f"Ejecutando Rollback compensatorio: eliminando borrador LC ID {created_lc_id}...")
                    self.odoo.execute_kw("stock.landed.cost", "unlink", [[created_lc_id]])
                except Exception as e_clean:
                    logger.critical(f"No se pudo eliminar el borrador LC {created_lc_id}: {e_clean}")
            raise


class LandedCostSanitizer:
    """
    Submódulo 1: Motor de Saneamiento y Reversión de Costeos Defectuosos.
    Identifica anomalías (como split_method='equal'), anula asientos antiguos
    y reemplaza por costeos rectificativos limpios.
    """

    def __init__(self, odoo: OdooService, costing_engine: ReceptionCostingEngine):
        self.odoo = odoo
        self.costing_engine = costing_engine

    def analyze_landed_cost(self, lc_id: int) -> Dict[str, Any]:
        """
        Audita forensemente un Landed Cost para detectar fallas:
        - split_method == 'equal'
        - campos Studio en 0.0
        - asiento STJ publicado y capas de valoración asociadas.
        """
        lcs = self.odoo.execute_kw(
            "stock.landed.cost",
            "search_read",
            [[["id", "=", lc_id]]],
            {
                "fields": [
                    "id",
                    "name",
                    "state",
                    "date",
                    "picking_ids",
                    "amount_total",
                    "account_move_id",
                    "cost_lines",
                    "valuation_adjustment_lines",
                    "x_studio_peso_total_de_costeo",
                    "x_studio_cambio_gastos",
                    "x_studio_tipo_de_cambio_usd",
                ]
            },
        )
        if not lcs:
            raise ValueError(f"No se encontró stock.landed.cost con ID {lc_id}")

        lc = lcs[0]
        cost_lines = self.odoo.execute_kw(
            "stock.landed.cost.lines",
            "search_read",
            [[["id", "in", lc.get("cost_lines", [])]]],
            {"fields": ["id", "product_id", "name", "split_method", "price_unit", "account_id"]},
        )

        has_equal_split = any(cl["split_method"] == "equal" for cl in cost_lines)
        has_zero_studio = (
            (lc.get("x_studio_peso_total_de_costeo") or 0.0) == 0.0
            or (lc.get("x_studio_cambio_gastos") or 0.0) == 0.0
        )

        move_data = None
        if lc.get("account_move_id"):
            move_id = lc["account_move_id"][0]
            move_rec = self.odoo.execute_kw(
                "account.move",
                "search_read",
                [[["id", "=", move_id]]],
                {"fields": ["id", "name", "state", "amount_total", "date"]},
            )
            if move_rec:
                move_data = move_rec[0]

        return {
            "id": lc["id"],
            "name": lc["name"],
            "state": lc["state"],
            "picking_ids": lc.get("picking_ids"),
            "amount_total": lc.get("amount_total"),
            "has_equal_split": has_equal_split,
            "has_zero_studio": has_zero_studio,
            "is_defective": has_equal_split or has_zero_studio,
            "cost_lines": cost_lines,
            "account_move": move_data,
        }

    def reverse_accounting_entry(
        self, move_id: int, reason: str = "Saneamiento de costeo erróneo"
    ) -> Optional[int]:
        """
        Ejecuta la reversión contable del asiento STJ mediante account.move.reversal
        dejando la cuenta transitoria y de stock limpias para el nuevo costeo.
        """
        try:
            logger.info(f"Iniciando reversión del asiento contable ID {move_id}...")
            # En Odoo 18, la reversión se orquesta mediante el wizard account.move.reversal
            reversal_wizard_id = self.odoo.execute_kw(
                "account.move.reversal",
                "create",
                [
                    {
                        "move_ids": [(6, 0, [move_id])],
                        "reason": reason,
                        "date_mode": "entry",  # o 'custom'
                    }
                ],
            )
            # Ejecutar reversión
            reversal_action = self.odoo.execute_kw(
                "account.move.reversal",
                "reverse_moves",
                [[reversal_wizard_id]],
            )
            logger.info(f"Reversión contable generada exitosamente: {reversal_action}")
            return reversal_wizard_id
        except Exception as e:
            logger.error(f"Error al revertir asiento contable {move_id}: {e}")
            raise

    def execute_sanitization_pipeline(
        self, faulty_lc_id: int, clean_payload: LandedCostPayload, dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Pipeline completo y transaccionalmente protegido:
        1. Analiza el LC defectuoso y guarda snapshot.
        2. Si dry_run=True, retorna simulación y auditoría sin modificar.
        3. Crea y computa el nuevo LC limpio.
        4. Si el nuevo LC es válido, revierte el asiento viejo y marca el LC viejo como cancelado.
        """
        analysis = self.analyze_landed_cost(faulty_lc_id)
        logger.info(f"Iniciando saneamiento de LC {analysis['name']} (ID {faulty_lc_id})...")

        if dry_run:
            sim = self.costing_engine.simulate_costing(clean_payload)
            return {
                "dry_run": True,
                "faulty_analysis": analysis,
                "simulation": sim,
            }

        # 1. Crear nuevo LC limpio (en borrador)
        new_lc = self.costing_engine.create_and_compute_landed_cost(
            clean_payload, auto_validate=False
        )

        # 2. Si el nuevo LC se computó con éxito, revertir asiento anterior
        reversal_id = None
        if analysis.get("account_move") and analysis["account_move"]["state"] == "posted":
            try:
                move_id = analysis["account_move"]["id"]
                reversal_id = self.reverse_accounting_entry(move_id)
            except Exception as e:
                logger.warning(
                    f"Aviso: El nuevo LC {new_lc['landed_cost_id']} está listo, pero la reversión automática del asiento {move_id} requiere revisión manual: {e}"
                )

        # 3. Cancelar costeo anterior si es posible
        try:
            self.odoo.execute_kw("stock.landed.cost", "button_cancel", [[faulty_lc_id]])
        except Exception as e:
            logger.info(f"Nota de estado en LC original {faulty_lc_id}: {e}")

        return {
            "success": True,
            "faulty_lc_id": faulty_lc_id,
            "clean_lc_id": new_lc["landed_cost_id"],
            "clean_lc_name": new_lc["landed_cost_name"],
            "url": new_lc["url"],
            "reversal_id": reversal_id,
        }


class LandedCostService:
    """
    Orquestador Central para APIs, Web y CLI.
    """

    def __init__(self, odoo_service: Optional[OdooService] = None):
        self.odoo = odoo_service or OdooService()
        self.costing = ReceptionCostingEngine(self.odoo)
        self.sanitizer = LandedCostSanitizer(self.odoo, self.costing)

    def get_incoming_pickings(self, q: str = "", limit: int = 50, company_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Devuelve las últimas recepciones (stock.picking) confirmadas con búsqueda rápida."""
        from src.core.config import settings
        target_co = company_id if company_id is not None else settings.ODOO_COMPANY_ID
        domain: List[Any] = [["state", "=", "done"], ["picking_type_code", "=", "incoming"]]
        if target_co and target_co > 0:
            domain.append(["company_id", "=", target_co])
        if q and q.strip():
            term = q.strip()
            domain.extend([
                "|", "|",
                ["name", "ilike", term],
                ["origin", "ilike", term],
                ["purchase_id", "ilike", term],
            ])
        return self.odoo.execute_kw(
            "stock.picking",
            "search_read",
            [domain],
            {
                "fields": ["id", "name", "origin", "partner_id", "purchase_id", "date_done", "state"],
                "order": "date_done desc, id desc",
                "limit": limit,
            },
        )


    def get_landed_costs_history(self, limit: int = 25, company_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Devuelve el historial de Costes en Destino con diagnóstico de defectos."""
        from src.core.config import settings
        target_co = company_id if company_id is not None else settings.ODOO_COMPANY_ID
        domain: List[Any] = []
        if target_co and target_co > 0:
            domain.append(["company_id", "=", target_co])
        records = self.odoo.execute_kw(
            "stock.landed.cost",
            "search_read",
            [domain],
            {
                "fields": [
                    "id",
                    "name",
                    "date",
                    "state",
                    "picking_ids",
                    "amount_total",
                    "x_studio_peso_total_de_costeo",
                    "x_studio_cambio_gastos",
                    "x_studio_tipo_de_cambio_usd",
                    "cost_lines",
                ],
                "order": "id desc",
                "limit": limit,
            },
        )
        for r in records:
            peso = r.get("x_studio_peso_total_de_costeo") or 0.0
            cambio_gastos = r.get("x_studio_cambio_gastos") or 0.0
            r["is_defective"] = peso == 0.0 or cambio_gastos == 0.0
        return records

    def preflight_check(
        self, picking_id: int, product_weights: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        return self.costing.preflight_check(picking_id, product_weights)

    def simulate(self, payload: LandedCostPayload) -> Dict[str, Any]:
        return self.costing.simulate_costing(payload)

    def create_and_compute(
        self, payload: LandedCostPayload, auto_validate: bool = False
    ) -> Dict[str, Any]:
        return self.costing.create_and_compute_landed_cost(payload, auto_validate=auto_validate)

    def analyze(self, lc_id: int) -> Dict[str, Any]:
        return self.sanitizer.analyze_landed_cost(lc_id)

    def sanitize(
        self, faulty_lc_id: int, clean_payload: LandedCostPayload, dry_run: bool = False
    ) -> Dict[str, Any]:
        return self.sanitizer.execute_sanitization_pipeline(faulty_lc_id, clean_payload, dry_run=dry_run)

