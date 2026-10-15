import re
import time
import unicodedata
from datetime import datetime
from threading import RLock
from typing import Dict, List, Set, Optional, Tuple, Any

from src.core.logger import logger


def normalize_text(text: Optional[str]) -> str:
    """Normaliza un texto: minúsculas, sin acentos ni caracteres extraños."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("utf-8")
    return text.lower().strip()


def normalize_code(code: Optional[str]) -> str:
    """Normaliza un código o código de barras para lookup hash O(1)."""
    if not code:
        return ""
    # Quita espacios, guiones, barras, puntos y caracteres especiales y pasa a mayúsculas
    return re.sub(r"[\s\-_/.:,;()]+", "", str(code).upper())


def tokenize(text: Optional[str]) -> List[str]:
    """Genera tokens alfanuméricos limpios (len >= 1) para el índice invertido."""
    norm = normalize_text(text)
    if not norm:
        return []
    raw_tokens = re.findall(r"[a-z0-9]+", norm)
    return [t for t in raw_tokens if len(t) >= 1]


class CatalogHashIndex:
    """
    Estructura de datos en memoria basada en Tablas Hash e Índices Invertidos.
    Permite búsquedas O(1) de productos por código/barcode y búsquedas masivas ultrarrápidas
    sin saturar la API de Odoo.
    """

    def __init__(self, ttl_seconds: int = 1800):
        self._lock = RLock()
        self._ttl_seconds: int = ttl_seconds
        self._loaded: bool = False
        self._loaded_at: Optional[datetime] = None

        # Lista de productos estándar en memoria
        self._products: List[Dict[str, Any]] = []

        # Tablas Hash O(1)
        self._code_index: Dict[str, Dict[str, Any]] = {}       # Referencia interna exacta y normalizada
        self._barcode_index: Dict[str, Dict[str, Any]] = {}    # Código de barras exacto y normalizado
        self._id_index: Dict[int, Dict[str, Any]] = {}         # product_id
        self._tmpl_id_index: Dict[int, Dict[str, Any]] = {}    # tmpl_id

        # Índice Invertido Hash: token -> set(product_id)
        self._token_index: Dict[str, Set[int]] = {}

        # Caché de búsqueda de nombres normalizados para matching secundario
        self._product_names_norm: Dict[int, str] = {}
        self._product_codes_norm: Dict[int, str] = {}
        self._product_barcodes_norm: Dict[int, str] = {}

    def is_valid(self) -> bool:
        """Determina si la caché en memoria está cargada y no ha expirado su TTL."""
        with self._lock:
            if not self._loaded or not self._products:
                return False
            if not self._loaded_at:
                return False
            elapsed = (datetime.now() - self._loaded_at).total_seconds()
            return elapsed < self._ttl_seconds

    def get_stats(self) -> Dict[str, Any]:
        """Devuelve estadísticas y metadatos de la tabla hash en memoria."""
        with self._lock:
            age = None
            if self._loaded_at:
                age = round((datetime.now() - self._loaded_at).total_seconds(), 1)
            return {
                "is_loaded": self._loaded and len(self._products) > 0,
                "item_count": len(self._products),
                "code_keys": len(self._code_index),
                "barcode_keys": len(self._barcode_index),
                "tokens_indexed": len(self._token_index),
                "loaded_at": self._loaded_at.strftime("%Y-%m-%d %H:%M:%S") if self._loaded_at else None,
                "age_seconds": age,
                "ttl_seconds": self._ttl_seconds,
            }

    def clear(self) -> None:
        """Limpia los índices en memoria."""
        with self._lock:
            self._products.clear()
            self._code_index.clear()
            self._barcode_index.clear()
            self._id_index.clear()
            self._tmpl_id_index.clear()
            self._token_index.clear()
            self._product_names_norm.clear()
            self._product_codes_norm.clear()
            self._product_barcodes_norm.clear()
            self._loaded = False
            self._loaded_at = None

    def load(self, products: List[Dict[str, Any]]) -> int:
        """
        Construye las tablas hash y el índice invertido a partir de la lista de productos de Odoo.
        Ejecución en tiempo < 20 ms para miles de productos.
        """
        start_time = time.perf_counter()
        with self._lock:
            self.clear()

            for p in products:
                # Estandarización de claves mínimas
                tmpl_id = (
                    p["product_tmpl_id"][0]
                    if isinstance(p.get("product_tmpl_id"), (list, tuple))
                    else p.get("tmpl_id", p.get("id"))
                )
                prod_id = p.get("product_id", p.get("id"))
                code = str(p.get("code") or p.get("default_code") or "").strip()
                if code in ["-", "--", "None", "False"]:
                    code = ""
                barcode = str(p.get("barcode") or "").strip()
                if barcode in ["-", "--", "None", "False"]:
                    barcode = ""
                name = str(p.get("name") or "").strip()
                if name in ["None", "False"]:
                    name = ""
                list_price = float(p.get("list_price") or 0.0)
                stock = float(p.get("stock") or p.get("qty_available") or 0.0)

                name_norm = normalize_text(name)
                code_norm = normalize_code(code)
                barcode_norm = normalize_code(barcode)

                item = {
                    "id": prod_id,
                    "product_id": prod_id,
                    "tmpl_id": tmpl_id,
                    "product_tmpl_id": p.get("product_tmpl_id") or [tmpl_id, name],
                    "code": code,
                    "default_code": code,
                    "barcode": barcode,
                    "name": name,
                    "list_price": list_price,
                    "stock": stock,
                    "qty_available": stock,
                    "name_norm": name_norm,
                    "code_norm": code_norm,
                    "barcode_norm": barcode_norm,
                    "raw": p,
                }
                self._products.append(item)

                # 1. Indexar por ID
                if prod_id is not None:
                    self._id_index[prod_id] = item
                if tmpl_id is not None and tmpl_id not in self._tmpl_id_index:
                    self._tmpl_id_index[tmpl_id] = item

                # 2. Indexar por Referencia Interna (Hash O(1))
                if code:
                    self._code_index[code] = item
                    self._code_index[code.upper()] = item
                    self._code_index[code.lower()] = item
                    if code_norm:
                        self._code_index[code_norm] = item
                        self._code_index[code_norm.lower()] = item

                # 3. Indexar por Código de Barras (Hash O(1))
                if barcode:
                    self._barcode_index[barcode] = item
                    self._barcode_index[barcode.upper()] = item
                    self._barcode_index[barcode.lower()] = item
                    if barcode_norm:
                        self._barcode_index[barcode_norm] = item
                        self._barcode_index[barcode_norm.lower()] = item

                # 4. Índice Invertido de Tokens para Búsqueda Instantánea
                if prod_id is not None:
                    self._product_names_norm[prod_id] = name_norm
                    self._product_codes_norm[prod_id] = code_norm
                    self._product_barcodes_norm[prod_id] = barcode_norm

                    tokens = set(tokenize(name))
                    if code:
                        tokens.update(tokenize(code))
                        if code_norm:
                            tokens.add(code_norm.lower())
                    if barcode:
                        tokens.update(tokenize(barcode))
                        if barcode_norm:
                            tokens.add(barcode_norm.lower())

                    for token in tokens:
                        if token not in self._token_index:
                            self._token_index[token] = set()
                        self._token_index[token].add(prod_id)

            self._loaded = True
            self._loaded_at = datetime.now()

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"[CatalogHashIndex] Indexación completa: {len(self._products)} productos, "
            f"{len(self._code_index)} refs hash, {len(self._barcode_index)} barcodes hash, "
            f"{len(self._token_index)} tokens en {elapsed_ms:.1f}ms."
        )
        return len(self._products)

    def lookup_exact(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Búsqueda O(1) exacta en tabla hash por código de barras, referencia interna o ID.
        """
        if not query:
            return None
        q = str(query).strip().strip("\"'").strip()
        if not q or q.lower() in ["-", "--", "none", "null", "nan", "false"]:
            return None

        with self._lock:
            # 1. Barcode directo y normalizado
            if q in self._barcode_index:
                return self._barcode_index[q]
            if q.upper() in self._barcode_index:
                return self._barcode_index[q.upper()]
            if q.lower() in self._barcode_index:
                return self._barcode_index[q.lower()]

            q_norm = normalize_code(q)
            if q_norm and q_norm in self._barcode_index:
                return self._barcode_index[q_norm]
            if q_norm and q_norm.lower() in self._barcode_index:
                return self._barcode_index[q_norm.lower()]

            # 2. Código / Referencia directa y normalizada
            if q in self._code_index:
                return self._code_index[q]
            if q.upper() in self._code_index:
                return self._code_index[q.upper()]
            if q.lower() in self._code_index:
                return self._code_index[q.lower()]

            if q_norm and q_norm in self._code_index:
                return self._code_index[q_norm]
            if q_norm and q_norm.lower() in self._code_index:
                return self._code_index[q_norm.lower()]

            # 3. Numérico por ID o Template ID
            if q.isdigit():
                num_id = int(q)
                if num_id in self._id_index:
                    return self._id_index[num_id]
                if num_id in self._tmpl_id_index:
                    return self._tmpl_id_index[num_id]

        return None

    def search(self, query: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        """
        Búsqueda ultrarrápida en memoria usando lookup exacto O(1) + scoring de tokens + subcadena.
        Responde en < 2 ms y ordena los resultados por relevancia determinista.
        Aplica umbrales estrictos de coincidencia para evitar falsos positivos con tokens triviales.
        """
        with self._lock:
            if not self._products:
                return []

            q = str(query).strip() if query is not None else ""
            if not q:
                return self._products[:limit]

            results: List[Dict[str, Any]] = []
            seen_prod_ids: Set[int] = set()

            # 1. Coincidencia exacta prioritaria O(1)
            exact = self.lookup_exact(q)
            if exact:
                results.append(exact)
                seen_prod_ids.add(exact["product_id"])
                if limit == 1:
                    return results

            # 2. Búsqueda con puntuación por índice invertido de tokens
            raw_query_tokens = tokenize(q)
            query_tokens = list(dict.fromkeys(raw_query_tokens))
            q_norm = normalize_text(q)
            q_code_norm = normalize_code(q)

            candidate_scores: Dict[int, int] = {}
            candidate_matched_tokens: Dict[int, Set[str]] = {}
            candidate_exact_tokens: Dict[int, Set[str]] = {}

            if query_tokens:
                total_q_tokens = len(query_tokens)

                for t in query_tokens:
                    t_len = len(t)
                    # 2.1 Coincidencia de token exacto
                    matched_ids = self._token_index.get(t)
                    if matched_ids:
                        for pid in matched_ids:
                            if pid not in candidate_matched_tokens:
                                candidate_matched_tokens[pid] = set()
                                candidate_exact_tokens[pid] = set()
                                candidate_scores[pid] = 0

                            candidate_matched_tokens[pid].add(t)
                            candidate_exact_tokens[pid].add(t)
                            if t_len >= 3:
                                candidate_scores[pid] += 10
                            elif t_len == 2:
                                candidate_scores[pid] += 6
                            else:
                                candidate_scores[pid] += 4

                    # 2.2 Prefijo de token (SOLO para tokens significativos len >= 3)
                    elif t_len >= 3:
                        for indexed_tok, ids in self._token_index.items():
                            if indexed_tok.startswith(t):
                                for pid in ids:
                                    if pid not in candidate_matched_tokens:
                                        candidate_matched_tokens[pid] = set()
                                        candidate_exact_tokens[pid] = set()
                                        candidate_scores[pid] = 0

                                    candidate_matched_tokens[pid].add(t)
                                    candidate_scores[pid] += 5
                            elif t_len >= 4 and t in indexed_tok:
                                for pid in ids:
                                    if pid not in candidate_matched_tokens:
                                        candidate_matched_tokens[pid] = set()
                                        candidate_exact_tokens[pid] = set()
                                        candidate_scores[pid] = 0

                                    candidate_matched_tokens[pid].add(t)
                                    candidate_scores[pid] += 2

                # Validar umbrales mínimos para cada candidato
                valid_candidates: List[Tuple[int, int]] = []

                for pid, score in candidate_scores.items():
                    if pid in seen_prod_ids:
                        continue

                    matched_tokens = candidate_matched_tokens.get(pid, set())
                    exact_tokens = candidate_exact_tokens.get(pid, set())
                    num_matched = len(matched_tokens)
                    substantive_matched = sum(1 for tok in matched_tokens if len(tok) >= 3)

                    name_norm = self._product_names_norm.get(pid, "")
                    code_norm = self._product_codes_norm.get(pid, "")
                    barcode_norm = self._product_barcodes_norm.get(pid, "")

                    is_phrase_match = bool(q_norm and len(q_norm) >= 3 and q_norm in name_norm)
                    is_code_substr = bool(q_code_norm and len(q_code_norm) >= 3 and q_code_norm in code_norm)
                    is_barcode_substr = bool(q_code_norm and len(q_code_norm) >= 4 and q_code_norm in barcode_norm)

                    # Bonificaciones de relevancia
                    if is_phrase_match:
                        score += 15
                    if is_code_substr:
                        score += 20
                    if is_barcode_substr:
                        score += 20

                    # Validación de umbral estricto:
                    is_valid = False
                    if is_phrase_match or is_code_substr or is_barcode_substr:
                        is_valid = True
                    elif total_q_tokens == 1:
                        # Para 1 token: debe coincidir exactamente y tener len >= 2
                        t0 = query_tokens[0]
                        if len(t0) >= 2 and t0 in exact_tokens:
                            is_valid = True
                    elif total_q_tokens == 2:
                        # Para 2 tokens: deben coincidir AMBOS tokens, con al menos 1 sustantivo
                        if num_matched == 2 and substantive_matched >= 1:
                            is_valid = True
                    else:
                        # Para 3 o más tokens:
                        # Al menos 2 tokens coincidentes, al menos 1 sustantivo,
                        # y una proporción de tokens >= 50%
                        if num_matched >= 2 and substantive_matched >= 1:
                            ratio = num_matched / total_q_tokens
                            if ratio >= 0.50:
                                is_valid = True

                    if is_valid:
                        valid_candidates.append((pid, score))

                # Ordenar candidatos válidos por puntuación descendente
                if valid_candidates:
                    valid_candidates.sort(key=lambda x: x[1], reverse=True)
                    for pid, _ in valid_candidates:
                        item = self._id_index.get(pid)
                        if item:
                            results.append(item)
                            seen_prod_ids.add(pid)
                            if len(results) >= limit:
                                return results

            # 3. Fallback: búsqueda por subcadena normalizada (solo cadenas significativas len >= 3)
            if len(results) < limit and ((q_norm and len(q_norm) >= 3) or (q_code_norm and len(q_code_norm) >= 3)):
                for item in self._products:
                    pid = item["product_id"]
                    if pid in seen_prod_ids:
                        continue

                    name_norm = item.get("name_norm", "")
                    code_norm = item.get("code_norm", "")
                    barcode_norm = item.get("barcode_norm", "")

                    matched = False
                    if q_norm and len(q_norm) >= 3 and q_norm in name_norm:
                        matched = True
                    elif q_code_norm and len(q_code_norm) >= 3 and q_code_norm in code_norm:
                        matched = True
                    elif q_code_norm and len(q_code_norm) >= 4 and q_code_norm in barcode_norm:
                        matched = True

                    if matched:
                        results.append(item)
                        seen_prod_ids.add(pid)
                        if len(results) >= limit:
                            break

            return results[:limit]

    def bulk_search(
        self, queries: List[str], match_mode: str = "smart"
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
        """
        Búsqueda masiva en lote para una lista de productos pegados o cargados.
        match_mode:
            - 'exact_code': Solo O(1) en código o código de barras.
            - 'smart': O(1) primero; si no coincide, búsqueda inteligente por nombre/modelo.
        Devuelve: (items_encontrados, queries_no_encontrados, estadisticas).
        """
        start_time = time.perf_counter()

        # Limpiar y normalizar lista de queries conservando orden
        clean_queries: List[str] = []
        seen_queries: Set[str] = set()
        for raw in queries:
            if raw is None:
                continue
            s = str(raw).strip().strip("\"'").strip()
            if not s or s.lower() in ["-", "--", "none", "null", "nan", "false"]:
                continue
            if s not in seen_queries:
                seen_queries.add(s)
                clean_queries.append(s)

        found_items: List[Dict[str, Any]] = []
        seen_prod_ids: Set[int] = set()
        not_found: List[str] = []

        with self._lock:
            for q in clean_queries:
                # 1. Intento O(1) exacto
                item = self.lookup_exact(q)

                # 2. Si no encontró y es modo inteligente, buscar el mejor candidato
                if not item and match_mode == "smart":
                    candidates = self.search(q, limit=1)
                    if candidates:
                        item = candidates[0]

                if item:
                    pid = item["product_id"]
                    if pid not in seen_prod_ids:
                        seen_prod_ids.add(pid)
                        found_items.append(item)
                else:
                    not_found.append(q)

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        stats = {
            "total_queries": len(clean_queries),
            "found_count": len(clean_queries) - len(not_found),
            "unique_found_count": len(found_items),
            "not_found_count": len(not_found),
            "execution_time_ms": round(elapsed_ms, 2),
            "cache_items": len(self._products),
        }

        return found_items, not_found, stats

    def get_indices(
        self,
    ) -> Tuple[
        List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]
    ]:
        """
        Devuelve (products, code_index, barcode_index) para compatibilidad directa
        con FileService.compare_with_odoo y evitar llamadas XML-RPC redundantes.
        """
        with self._lock:
            return list(self._products), self._code_index, self._barcode_index
