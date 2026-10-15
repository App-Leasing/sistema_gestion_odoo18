import re
import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional
import pandas as pd

from src.core.config import settings
from src.core.logger import logger


class FileService:
    """Servicio de procesamiento de archivos (Excel/CSV), detección de columnas y auditoría."""

    @staticmethod
    def clean_price_value(val: Any) -> Optional[float]:
        """Limpia y convierte un valor de precio heterogéneo a float."""
        if pd.isna(val) or val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)

        s = str(val).strip()
        s = re.sub(r"[^\d,\.]", "", s)
        if not s:
            return None

        # Detectar formato paraguayo / latino (punto como miles, coma como decimal) o anglosajón
        if "." in s and "," in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "." in s:
            parts = s.split(".")
            if len(parts) == 2 and len(parts[1]) == 3:
                s = s.replace(".", "")
            elif len(parts) > 2:
                s = s.replace(".", "")
        elif "," in s:
            s = s.replace(",", ".")

        try:
            return float(s)
        except ValueError:
            return None

    @classmethod
    def detect_columns(
        cls, df: pd.DataFrame, custom_mapping: Optional[Dict[str, str]] = None
    ) -> Dict[str, Optional[str]]:
        """Identifica las columnas de Código, Código de Barras y Precio en el DataFrame."""
        mapping: Dict[str, Optional[str]] = {
            "code_col": None,
            "barcode_col": None,
            "price_col": None,
        }

        if custom_mapping:
            for k, v in custom_mapping.items():
                if v and v in df.columns:
                    mapping[k] = v

        cols = list(df.columns)
        cols_lower = [str(c).lower().strip() for c in cols]

        # Detectar columna de código / referencia interna
        if not mapping["code_col"]:
            code_candidates = [
                "default_code",
                "codigo",
                "código",
                "código / ref. interna",
                "codigo / ref. interna",
                "código / ref interna",
                "codigo / ref interna",
                "referencia_interna",
                "referencia interna",
                "referencia",
                "ref. interna",
                "ref interna",
                "ref",
                "sku",
                "articulo",
                "artículo",
                "cod",
                "item",
                "código_producto",
                "codigo_producto",
            ]
            for candidate in code_candidates:
                if candidate in cols_lower:
                    idx = cols_lower.index(candidate)
                    mapping["code_col"] = cols[idx]
                    break

        # Detectar columna de código de barras
        if not mapping["barcode_col"]:
            barcode_candidates = [
                "barcode",
                "codigo_barra",
                "codigo_barras",
                "código_de_barras",
                "código_barras",
                "código de barras",
                "codigo de barras",
                "código de barra",
                "codigo de barra",
                "ean",
                "ean13",
                "ean_13",
                "upc",
                "cod_barra",
                "cod_barras",
            ]
            for candidate in barcode_candidates:
                if candidate in cols_lower:
                    idx = cols_lower.index(candidate)
                    mapping["barcode_col"] = cols[idx]
                    break

        # Detectar columna de precio
        if not mapping["price_col"]:
            price_candidates = [
                "nuevo precio (gs.)",
                "nuevo precio (gs)",
                "nuevo precio",
                "precio nuevo",
                "precio fincionario (gs)",
                "precio fincionario (gs.)",
                "precio fincionario",
                "precio funcionario (gs)",
                "precio funcionario (gs.)",
                "precio funcionarios",
                "precio funcionario",
                "precio_nuevo",
                "nuevo_precio",
                "list_price",
                "precio",
                "precio (gs.)",
                "precio (gs)",
                "precio_venta",
                "pvp",
                "precio_lista",
                "price",
                "new_price",
                "precio_publico",
                "precio_final",
                "precio venta",
                "precio lista",
                "costo",
            ]
            for candidate in price_candidates:
                if candidate in cols_lower:
                    idx = cols_lower.index(candidate)
                    mapping["price_col"] = cols[idx]
                    break

        return mapping

    @classmethod
    def load_price_file(
        cls,
        file_path: Path,
        custom_mapping: Optional[Dict[str, str]] = None,
    ) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
        """Carga archivo Excel (.xlsx / .xls) o CSV y detecta las columnas clave."""
        suffix = file_path.suffix.lower()
        if suffix in [".xlsx", ".xls"]:
            df = pd.read_excel(file_path, dtype=str)
        elif suffix in [".csv", ".txt"]:
            try:
                df = pd.read_csv(file_path, dtype=str, sep=None, engine="python")
            except Exception:
                df = pd.read_csv(file_path, dtype=str, sep=";")
        else:
            raise ValueError(
                f"Formato no soportado: '{suffix}'. Utilice .xlsx, .xls o .csv"
            )

        mapping = cls.detect_columns(df, custom_mapping)
        if not mapping["price_col"]:
            raise ValueError(
                f"No se detectó columna de PRECIO en: {list(df.columns)}. "
                "Especifique --col-price o use un encabezado como 'precio' o 'list_price'."
            )
        if not mapping["code_col"] and not mapping["barcode_col"]:
            raise ValueError(
                "Se requiere al menos una columna de IDENTIFICADOR (Código o Código de Barras). "
                f"Columnas disponibles: {list(df.columns)}"
            )

        return df, mapping

    @classmethod
    def compare_with_odoo(
        cls,
        df: pd.DataFrame,
        col_mapping: Dict[str, Optional[str]],
        code_index: Dict[str, Dict[str, Any]],
        barcode_index: Dict[str, Dict[str, Any]],
        tolerance: float = 0.001,
    ) -> Tuple[
        List[Dict[str, Any]],
        List[Dict[str, Any]],
        List[Dict[str, Any]],
        List[Dict[str, Any]],
    ]:
        """Compara las filas del archivo contra los índices en memoria de Odoo."""
        to_update = []
        unchanged = []
        not_found = []
        all_audit = []

        code_col = col_mapping.get("code_col")
        barcode_col = col_mapping.get("barcode_col")
        price_col = col_mapping.get("price_col")

        seen_keys = set()

        for idx, row in df.iterrows():
            raw_code = (
                str(row[code_col]).strip()
                if code_col and pd.notna(row[code_col])
                else ""
            )
            raw_barcode = (
                str(row[barcode_col]).strip()
                if barcode_col and pd.notna(row[barcode_col])
                else ""
            )
            raw_price = row[price_col]

            # Ignorar valores basura como '-', 'nan', etc.
            if raw_code.lower() in ["-", "--", "nan", "none", "null"]:
                raw_code = ""
            if raw_barcode.lower() in ["-", "--", "nan", "none", "null"]:
                raw_barcode = ""

            new_price = cls.clean_price_value(raw_price)

            if not raw_code and not raw_barcode:
                continue

            matched_prod = None
            if raw_code:
                matched_prod = code_index.get(raw_code) or code_index.get(
                    raw_code.upper().replace(" ", "")
                )

            if not matched_prod and raw_barcode:
                matched_prod = barcode_index.get(raw_barcode) or barcode_index.get(
                    raw_barcode.upper().replace(" ", "")
                )

            dedup_key = raw_code or raw_barcode
            is_duplicate = dedup_key in seen_keys
            seen_keys.add(dedup_key)

            if not matched_prod:
                item = {
                    "file_row": idx + 2,
                    "code": raw_code,
                    "barcode": raw_barcode,
                    "product_id": None,
                    "tmpl_id": None,
                    "name": "NO ENCONTRADO EN ODOO",
                    "old_price": None,
                    "new_price": new_price,
                    "diff": None,
                    "status": "NO_ENCONTRADO",
                    "notes": "Duplicado en archivo" if is_duplicate else "",
                }
                not_found.append(item)
                all_audit.append(item)
                continue

            matched_prod_id = matched_prod.get("product_id") or matched_prod.get("id")
            raw_tmpl = matched_prod.get("product_tmpl_id")
            if isinstance(raw_tmpl, (list, tuple)) and len(raw_tmpl) > 0:
                tmpl_id = raw_tmpl[0]
            else:
                tmpl_id = matched_prod.get("tmpl_id") or matched_prod_id

            if new_price is None:
                item = {
                    "file_row": idx + 2,
                    "code": raw_code or matched_prod.get("default_code") or matched_prod.get("code") or "",
                    "barcode": raw_barcode or matched_prod.get("barcode") or "",
                    "product_id": matched_prod_id,
                    "tmpl_id": tmpl_id,
                    "name": matched_prod.get("name", ""),
                    "old_price": matched_prod.get("list_price"),
                    "new_price": None,
                    "diff": None,
                    "status": "ERROR_PRECIO_INVALIDO",
                    "notes": f"Valor en archivo no es numérico: {raw_price}",
                }
                all_audit.append(item)
                continue

            old_price = float(matched_prod.get("list_price") or 0.0)
            diff = new_price - old_price

            code_display = raw_code or matched_prod.get("default_code") or matched_prod.get("code") or ""
            if code_display in ["-", "--"]:
                code_display = ""
            barcode_display = raw_barcode or matched_prod.get("barcode") or ""
            if barcode_display in ["-", "--"]:
                barcode_display = ""

            audit_item = {
                "file_row": idx + 2,
                "code": code_display,
                "barcode": barcode_display,
                "product_id": matched_prod_id,
                "tmpl_id": tmpl_id,
                "name": matched_prod.get("name", ""),
                "old_price": old_price,
                "new_price": new_price,
                "diff": diff,
                "notes": "Duplicado en archivo" if is_duplicate else "",
            }

            if abs(diff) <= tolerance:
                audit_item["status"] = "SIN_CAMBIOS"
                unchanged.append(audit_item)
            else:
                audit_item["status"] = "PENDIENTE"
                to_update.append(audit_item)

            all_audit.append(audit_item)

        return to_update, unchanged, not_found, all_audit

    @classmethod
    def export_audit_excel(
        cls,
        audit_records: List[Dict[str, Any]],
        out_dir: Optional[Path] = None,
    ) -> Path:
        """Genera un archivo Excel enriquecido con la auditoría de cambios."""
        target_dir = out_dir or settings.REPORTS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = target_dir / f"auditoria_precios_{timestamp}.xlsx"

        df_out = pd.DataFrame(audit_records)
        cols_order = [
            ("file_row", "Fila Archivo"),
            ("code", "Referencia Interna"),
            ("barcode", "Código de Barras"),
            ("name", "Nombre Producto"),
            ("old_price", "Precio Anterior (Odoo)"),
            ("new_price", "Precio Nuevo"),
            ("diff", "Diferencia"),
            ("status", "Estado"),
            ("notes", "Observaciones"),
        ]

        rename_dict = {}
        final_cols = []
        for k, label in cols_order:
            if k in df_out.columns:
                rename_dict[k] = label
                final_cols.append(label)

        df_out = df_out.rename(columns=rename_dict)[final_cols]
        df_out.to_excel(out_file, index=False)
        logger.info(f"Reporte de auditoría generado: {out_file}")
        return out_file

    @classmethod
    def export_catalog_to_excel(
        cls, products: List[Dict[str, Any]], out_path: Path
    ) -> Path:
        """Descarga el catálogo completo de Odoo a un archivo Excel plantilla."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
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

            rows.append(
                {
                    "ID Producto": p["id"],
                    "ID Plantilla": tmpl_id,
                    "Referencia Interna": code,
                    "Código de Barras": barcode,
                    "Nombre": p["name"],
                    "Precio Actual": p.get("list_price") or 0.0,
                }
            )

        df = pd.DataFrame(rows)
        df.to_excel(out_path, index=False)
        logger.info(f"Catálogo exportado exitosamente a: {out_path}")
        return out_path

    @classmethod
    def generate_approval_excel(
        cls,
        items: List[Dict[str, Any]],
        pricelist_name: str = "Lista de Precios",
        out_dir: Optional[Path] = None,
    ) -> Path:
        """
        Genera una planilla ejecutiva de aprobación en Excel con formato corporativo,
        diseño ejecutivo, separadores de miles y fórmulas de variación para gerencia.
        """
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        target_dir = out_dir or settings.REPORTS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)

        clean_slug = re.sub(r"[^\w\-]", "_", pricelist_name.strip().lower())
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = target_dir / f"aprobacion_{clean_slug}_{timestamp}.xlsx"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Lista de Precios"

        headers = [
            ("Código / Ref. Interna", 24),
            ("Código de Barras", 18),
            ("Descripción del Producto", 40),
            ("Precio Anterior (Gs.)", 20),
            ("Nuevo Precio (Gs.)", 20),
            ("Diferencia (Gs.)", 18),
            ("Variación %", 14),
            ("Observación / Estado", 28),
        ]

        header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        thin_border = Border(
            left=Side(style="thin", color="D9D9D9"),
            right=Side(style="thin", color="D9D9D9"),
            top=Side(style="thin", color="D9D9D9"),
            bottom=Side(style="thin", color="D9D9D9"),
        )

        font_reg = Font(name="Calibri", size=10)
        font_bold = Font(name="Calibri", size=10, bold=True)
        font_green = Font(name="Calibri", size=10, color="006100")
        font_red = Font(name="Calibri", size=10, color="9C0006")

        fill_zebra = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
        fill_highlight = PatternFill(start_color="EBF1F5", end_color="EBF1F5", fill_type="solid")

        ws.row_dimensions[1].height = 28
        for col_idx, (title, width) in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=title)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        for row_idx, it in enumerate(items, 2):
            ws.row_dimensions[row_idx].height = 20
            row_fill = fill_zebra if (row_idx % 2 == 0) else None

            code = str(it.get("code") or "").strip()
            barcode = str(it.get("barcode") or "").strip()
            name = str(it.get("name") or "").strip()
            old_p = float(it.get("old_price") or 0.0)
            new_p = float(it.get("new_price") if it.get("new_price") is not None else old_p)
            diff = new_p - old_p
            pct = (diff / old_p) if old_p > 0 else 0.0
            note = str(it.get("notes") or "").strip()
            if not note:
                note = "Modificado" if diff != 0 else "Sin cambio"

            # Col 1: Codigo
            c1 = ws.cell(row=row_idx, column=1, value=code)
            c1.font = font_reg
            c1.alignment = Alignment(horizontal="left", vertical="center")
            c1.border = thin_border
            if row_fill: c1.fill = row_fill

            # Col 2: Barcode
            c2 = ws.cell(row=row_idx, column=2, value=barcode)
            c2.font = font_reg
            c2.alignment = Alignment(horizontal="center", vertical="center")
            c2.number_format = "@"
            c2.border = thin_border
            if row_fill: c2.fill = row_fill

            # Col 3: Name
            c3 = ws.cell(row=row_idx, column=3, value=name)
            c3.font = font_reg
            c3.alignment = Alignment(horizontal="left", vertical="center")
            c3.border = thin_border
            if row_fill: c3.fill = row_fill

            # Col 4: Precio Anterior
            c4 = ws.cell(row=row_idx, column=4, value=round(old_p))
            c4.font = font_reg
            c4.alignment = Alignment(horizontal="right", vertical="center")
            c4.number_format = "#,##0"
            c4.border = thin_border
            if row_fill: c4.fill = row_fill

            # Col 5: Nuevo Precio (destacado)
            c5 = ws.cell(row=row_idx, column=5, value=round(new_p))
            c5.font = font_bold
            c5.alignment = Alignment(horizontal="right", vertical="center")
            c5.number_format = "#,##0"
            c5.fill = fill_highlight
            c5.border = thin_border

            # Col 6: Diferencia
            c6 = ws.cell(row=row_idx, column=6, value=round(diff))
            c6.font = font_red if diff < 0 else (font_green if diff > 0 else font_reg)
            c6.alignment = Alignment(horizontal="right", vertical="center")
            c6.number_format = "+#,##0;-#,##0;0"
            c6.border = thin_border
            if row_fill: c6.fill = row_fill

            # Col 7: Variacion %
            c7 = ws.cell(row=row_idx, column=7, value=pct)
            c7.font = font_red if pct < 0 else (font_green if pct > 0 else font_reg)
            c7.alignment = Alignment(horizontal="right", vertical="center")
            c7.number_format = "0.00%"
            c7.border = thin_border
            if row_fill: c7.fill = row_fill

            # Col 8: Observacion
            c8 = ws.cell(row=row_idx, column=8, value=note)
            c8.font = font_reg
            c8.alignment = Alignment(horizontal="left", vertical="center")
            c8.border = thin_border
            if row_fill: c8.fill = row_fill

        ws.auto_filter.ref = f"A1:H{len(items) + 1}"
        ws.freeze_panes = "A2"

        wb.save(out_file)
        logger.info(f"Planilla de aprobación generada exitosamente: {out_file}")
        return out_file
