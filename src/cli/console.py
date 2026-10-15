import argparse
import sys
from pathlib import Path

# Fix Windows console UTF-8 encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)
from rich.panel import Panel

from src.core.config import settings
from src.core.logger import logger
from src.services.odoo_service import OdooService
from src.services.file_service import FileService

console = Console()


def format_currency(val: float) -> str:
    """Formatea valores monetarios con separador de miles."""
    if val is None:
        return "-"
    return f"{val:,.0f}".replace(",", ".")


def print_banner():
    console.print(
        Panel.fit(
            "[bold cyan]Odoo 18 - Actualizador Masivo de Precios (XML-RPC)[/bold cyan]\n"
            f"[dim]Instancia: {settings.ODOO_URL} | DB: {settings.ODOO_DB}[/dim]",
            border_style="cyan",
        )
    )


def handle_export_catalog(service: OdooService, out_path: Path):
    """Descarga el catálogo completo de Odoo y lo exporta a Excel."""
    console.print("[yellow]Extrayendo catálogo completo de Odoo...[/yellow]")
    products, _, _ = service.fetch_catalog(active_only=True)
    FileService.export_catalog_to_excel(products, out_path)
    console.print(
        f"[green][OK] Catálogo exportado exitosamente a: [bold]{out_path}[/bold][/green]"
    )


def run_cli(args_list=None):
    """Ejecuta el modo de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Actualizador Masivo de Precios en Odoo 18 vía XML-RPC",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "file",
        nargs="?",
        type=str,
        help="Ruta al archivo Excel (.xlsx, .xls) o CSV con los nuevos precios",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Modo simulación: evalúa el archivo y genera reporte SIN modificar Odoo",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirma automáticamente sin solicitar confirmación interactiva [S/N]",
    )
    parser.add_argument(
        "--col-code",
        type=str,
        default=None,
        help="Nombre de la columna para Referencia Interna / Código",
    )
    parser.add_argument(
        "--col-barcode",
        type=str,
        default=None,
        help="Nombre de la columna para Código de Barras",
    )
    parser.add_argument(
        "--col-price",
        type=str,
        default=None,
        help="Nombre de la columna para Precio Nuevo",
    )
    parser.add_argument(
        "--export-catalog",
        type=str,
        metavar="RUTA_EXCEL",
        help="Exporta el catálogo actual de Odoo a un archivo Excel para usar de plantilla",
    )

    args = parser.parse_args(args_list)
    print_banner()

    settings.validate()
    service = OdooService()

    # Conectar y autenticar
    try:
        with console.status("[bold green]Autenticando contra Odoo XML-RPC..."):
            uid = service.connect()
        console.print(f"[green][OK] Autenticado correctamente (UID: {uid})[/green]\n")
    except Exception as e:
        console.print(f"[bold red]Error crítico de conexión:[/bold red] {e}")
        sys.exit(1)

    # Exportar catálogo si se solicitó
    if args.export_catalog:
        out_file = Path(args.export_catalog)
        handle_export_catalog(service, out_file)
        return

    if not args.file:
        console.print(
            "[red]Error:[/red] Debe especificar la ruta del archivo a procesar o usar --export-catalog."
        )
        parser.print_help()
        sys.exit(1)

    file_path = Path(args.file)
    if not file_path.exists():
        console.print(f"[bold red]Error:[/bold red] El archivo '{file_path}' no existe.")
        sys.exit(1)

    # Cargar archivo de precios
    custom_map = {}
    if args.col_code:
        custom_map["code_col"] = args.col_code
    if args.col_barcode:
        custom_map["barcode_col"] = args.col_barcode
    if args.col_price:
        custom_map["price_col"] = args.col_price

    try:
        df, detected_cols = FileService.load_price_file(file_path, custom_map)
    except Exception as e:
        console.print(f"[bold red]Error al leer archivo:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[cyan]Archivo cargado:[/cyan] {file_path.name} ({len(df)} filas)")
    console.print(
        f"[dim]Columnas mapeadas -> Código: '{detected_cols['code_col']}' | "
        f"Barcode: '{detected_cols['barcode_col']}' | Precio: '{detected_cols['price_col']}'[/dim]\n"
    )

    # Descargar catálogo en Batch Read
    with console.status(
        "[bold green]Descargando catálogo de Odoo en memoria (Batch Read)..."
    ):
        _, code_idx, barcode_idx = service.fetch_catalog(active_only=True)

    # Comparar precios en memoria
    with console.status("[bold green]Comparando precios y detectando variaciones..."):
        to_update, unchanged, not_found, all_audit = FileService.compare_with_odoo(
            df, detected_cols, code_idx, barcode_idx
        )

    # Resumen de métricas
    summary_table = Table(
        title="Resumen del Análisis de Precios",
        show_header=True,
        header_style="bold magenta",
    )
    summary_table.add_column("Métrica", style="dim")
    summary_table.add_column("Cantidad", justify="right", style="bold")
    summary_table.add_row("Total filas analizadas", str(len(df)))
    summary_table.add_row(
        "[yellow]Productos con cambio de precio[/yellow]",
        f"[bold yellow]{len(to_update)}[/bold yellow]",
    )
    summary_table.add_row(
        "[green]Productos con precio idéntico (omitidos)[/green]",
        f"[green]{len(unchanged)}[/green]",
    )
    summary_table.add_row(
        "[red]Productos no encontrados en Odoo[/red]",
        f"[bold red]{len(not_found)}[/bold red]",
    )
    console.print(summary_table)
    console.print()

    # Previsualización
    if to_update:
        preview_table = Table(
            title=f"Vista previa de cambios a aplicar (Mostrando {min(10, len(to_update))} de {len(to_update)})",
            show_header=True,
        )
        preview_table.add_column("Ref / Código", style="cyan")
        preview_table.add_column(
            "Nombre", style="white", max_width=40, overflow="ellipsis"
        )
        preview_table.add_column("Precio Actual", justify="right", style="dim")
        preview_table.add_column(
            "Precio Nuevo", justify="right", style="bold yellow"
        )
        preview_table.add_column("Diferencia", justify="right")

        for item in to_update[:10]:
            diff_str = (
                f"+{format_currency(item['diff'])}"
                if item["diff"] > 0
                else format_currency(item["diff"])
            )
            diff_style = "green" if item["diff"] > 0 else "red"
            preview_table.add_row(
                item["code"] or item["barcode"],
                item["name"],
                format_currency(item["old_price"]),
                format_currency(item["new_price"]),
                f"[{diff_style}]{diff_str}[/{diff_style}]",
            )
        console.print(preview_table)
        console.print()

    if not_found:
        nf_table = Table(
            title=f"Muestra de productos no encontrados en Odoo ({min(5, len(not_found))} de {len(not_found)})",
            show_header=True,
        )
        nf_table.add_column("Fila", justify="right")
        nf_table.add_column("Código", style="red")
        nf_table.add_column("Código de Barras", style="red")
        nf_table.add_column("Precio en Archivo", justify="right")
        for nf in not_found[:5]:
            nf_table.add_row(
                str(nf["file_row"]),
                nf["code"] or "-",
                nf["barcode"] or "-",
                format_currency(nf["new_price"]),
            )
        console.print(nf_table)
        console.print()

    # Modo simulación
    if args.dry_run:
        console.print(
            "[bold yellow]MODO SIMULACIÓN (--dry-run) ACTIVO[/bold yellow]: No se realizaron cambios en Odoo."
        )
        for item in to_update:
            item["status"] = "SIMULADO"
        report_path = FileService.export_audit_excel(
            all_audit, settings.REPORTS_DIR
        )
        console.print(
            f"[green][OK] Reporte de simulación guardado en: [bold]{report_path}[/bold][/green]"
        )
        return

    # Si no hay variaciones
    if not to_update:
        console.print(
            "[bold green][OK] Todos los productos del archivo ya tienen el precio correcto en Odoo. Nada que actualizar.[/bold green]"
        )
        report_path = FileService.export_audit_excel(
            all_audit, settings.REPORTS_DIR
        )
        console.print(f"[dim]Reporte guardado en: {report_path}[/dim]")
        return

    # Confirmación interactiva
    if not args.yes:
        confirm = (
            console.input(
                f"[bold yellow]¿Desea proceder a actualizar los {len(to_update)} productos en Odoo? (S/N): [/bold yellow]"
            )
            .strip()
            .upper()
        )
        if confirm not in ["S", "SI", "Y", "YES"]:
            console.print("[red]Operación cancelada por el usuario.[/red]")
            sys.exit(0)

    # Actualización masiva
    console.print(
        f"\n[bold green]Iniciando actualización masiva de {len(to_update)} productos...[/bold green]"
    )
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "[cyan]Actualizando precios en Odoo...", total=len(to_update)
        )

        def update_progress(curr, total, item):
            progress.update(task, completed=curr)

        service.write_batch(to_update, on_progress=update_progress)

    # Exportar reporte final
    report_path = FileService.export_audit_excel(
        all_audit, settings.REPORTS_DIR
    )

    success_count = sum(
        1 for item in to_update if item.get("status") == "ACTUALIZADO"
    )
    error_count = len(to_update) - success_count

    console.print()
    if error_count == 0:
        console.print(
            f"[bold green][OK] Éxito total: Se actualizaron los {success_count} productos correctamente.[/bold green]"
        )
    else:
        console.print(
            f"[bold yellow]Finalizado con advertencias: {success_count} actualizados, {error_count} errores.[/bold yellow]"
        )

    console.print(
        f"[green][OK] Reporte completo de auditoría generado en: [bold]{report_path}[/bold][/green]"
    )
