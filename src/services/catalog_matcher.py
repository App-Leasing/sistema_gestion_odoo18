import re
from typing import Dict, List, Tuple, Any, Optional
from src.core.logger import logger


class CatalogMatcher:
    """
    Servicio de emparejamiento inteligente de productos para cualquier lista de precios.
    Resuelve productos por Código, Barcode o por extracción de Modelo, Especificaciones (RAM/ROM) y Color.
    """

    COLOR_MAP = {
        'BLACK': ['BLK', 'BLACK', 'NEGRO'],
        'BLUE': ['BLU', 'BLUE', 'AZUL'],
        'GOLD': ['GLD', 'GOLD', 'DORADO'],
        'PURPLE': ['PUR', 'PURPLE', 'MORADO', 'DARK PURPLE'],
        'PINK': ['PNK', 'PINK', 'ROSA'],
        'ORANGE': ['ORG', 'ORANGE', 'NARANJA'],
        'SILVER': ['SLV', 'SILVER', 'PLATA'],
        'GREEN': ['GRN', 'GREEN', 'VERDE'],
        'GREY': ['GRY', 'GREY', 'GRIS', 'GUNMETAL', 'TITANIUM GREY'],
        'WHITE': ['WHT', 'WHITE', 'BLANCO'],
        'TITANIUM': ['TIT', 'TITANIUM', 'GREY'],
        'TITANIUM GREY': ['TIT', 'TITANIUM', 'GREY'],
        'RED': ['RED', 'ROJO'],
        'YELLOW': ['YEL', 'YELLOW', 'AMARILLO'],
    }

    @classmethod
    def clean_price(cls, val: Any) -> float:
        """Limpia y extrae valor numérico de precio."""
        if val is None or val == "":
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace('₲', '').replace('$', '').replace('Gs', '').replace('Gs.', '').strip()
        s = s.replace('.', '').replace(',', '')
        m = re.search(r'\d+', s)
        return float(m.group(0)) if m else 0.0

    @classmethod
    def match_item_to_catalog(
        cls,
        search_term: str,
        catalog_products: List[Dict[str, Any]],
        code_index: Dict[str, Dict[str, Any]],
        barcode_index: Dict[str, Dict[str, Any]],
        color_hint: str = "",
        memory_hint: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        Intenta emparejar un término de búsqueda contra el catálogo usando estrategia escalonada:
        1. Código exacto en Tabla Hash
        2. Barcode exacto en Tabla Hash
        3. Código normalizado (sin guiones/espacios)
        4. Emparejamiento por Modelo + Memoria + Color
        """
        term_clean = str(search_term).strip()
        term_upper = term_clean.upper()
        term_norm = term_upper.replace('-', '').replace('_', '').replace(' ', '')

        # Nivel 1: Búsqueda exacta en Hash Table
        if term_upper in code_index:
            return code_index[term_upper]
        if term_norm in code_index:
            return code_index[term_norm]
        if term_clean in barcode_index:
            return barcode_index[term_clean]
        if term_norm in barcode_index:
            return barcode_index[term_norm]

        # Nivel 2: Extracción de componentes (Modelo, Memoria, Color)
        # Extraer modelo: ej. "C3", "WP58 PRO", "RT8", "P1 PRO", etc.
        model_match = re.search(r'\b(C\d+|P\d+|G\d+|WP\d+|RT\d+|OT\d+|BT\d+|V\d+)(?:\s+(?:PLUS|PRO|ULTRA|AIR|TITAN|NFC|KIDS))?', term_upper)
        target_model = model_match.group(0).replace(' ', '') if model_match else ""

        # Extraer memoria: ej. "4+64", "4+128", "8+256", "12+512", "1TB", etc.
        mem_match = re.search(r'(\d+\s*\+\s*\d+|\b1TB\b|\b512GB\b|\b256GB\b|\b128GB\b)', term_upper)
        target_mem = memory_hint or (mem_match.group(0).replace(' ', '') if mem_match else "")

        # Color
        target_color = color_hint.upper().strip()
        if not target_color:
            for color_name in cls.COLOR_MAP.keys():
                if color_name in term_upper:
                    target_color = color_name
                    break

        # Nivel 3: Búsqueda de coincidencia entre productos del catálogo
        for p in catalog_products:
            p_code = (p.get('default_code') or p.get('code') or '').upper()
            p_name = (p.get('name') or '').upper()
            p_code_norm = p_code.replace('-', '').replace('_', '').replace(' ', '')
            p_name_norm = p_name.replace('-', ' ').replace('_', ' ')

            # Coincidencia de modelo
            if target_model:
                if target_model not in p_code_norm and target_model not in p_name_norm.replace(' ', ''):
                    continue

            # Coincidencia de memoria
            if target_mem:
                if '+' in target_mem:
                    r1, r2 = target_mem.split('+')
                    if not ((r1 in p_code_norm or r1 in p_name_norm) and (r2 in p_code_norm or r2 in p_name_norm)):
                        continue
                else:
                    if target_mem not in p_code_norm and target_mem not in p_name_norm:
                        continue

            # Coincidencia de color
            if target_color:
                aliases = cls.COLOR_MAP.get(target_color, [target_color])
                color_found = any(a in p_code_norm or a in p_name_norm for a in aliases)
                if not color_found:
                    continue

            # Si pasó los filtros activos, es un match positivo
            return p

        return None
