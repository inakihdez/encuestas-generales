#!/usr/bin/env python3
"""
Encuestas de las elecciones generales -> Datawrapper.

Adaptación a Python del script R de las autonómicas de Castilla y León.
Descarga la tabla de sondeos de Wikipedia, la pasa a formato largo y
actualiza tres gráficos de Datawrapper:
  1. Todas las encuestas (formato largo)
  2. Media móvil de las últimas N fechas con encuesta
  3. Media semanal

Variables de entorno:
  DATAWRAPPER_API_KEY   token de Datawrapper (secret en GitHub)
  DW_CHART_ENCUESTAS    id del gráfico de encuestas
  DW_CHART_MEDIA        id del gráfico de media móvil
  DW_CHART_SEMANAL      id del gráfico de media semanal
  DRY_RUN=1             no sube nada; guarda los CSV en ./salida
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
URLS = [
    "https://en.wikipedia.org/wiki/Opinion_polling_for_the_2026_Spanish_general_election",
    # Respaldo por si la página anterior no existe o se renombra
    "https://en.wikipedia.org/wiki/Opinion_polling_for_the_next_Spanish_general_election",
]

CHART_ENCUESTAS = os.getenv("DW_CHART_ENCUESTAS") or "XXXXX"
CHART_MEDIA = os.getenv("DW_CHART_MEDIA") or "XXXXX"
CHART_SEMANAL = os.getenv("DW_CHART_SEMANAL") or "XXXXX"

VENTANA_MEDIA = 5                 # zoo::rollmean(k = 5)
ANYOS_MEDIA = [2026]              # filtro del gráfico de media móvil
ANYOS_SEMANAL = ["2025", "2026"]  # filtro del gráfico semanal
SOLO_CON_ESCANOS = True           # igual que filter(!is.na(Diputados)) en R

# Nombres de Wikipedia -> nombres en los gráficos
RENOMBRAR_PARTIDOS = {"Vox": "VOX"}
# Filas que no son encuestas (además de cualquier fila de resultados "... election")
EXCLUIR_ENCUESTADORES = {"PP", "PSOE"}
# Columnas entre Turnout y Lead que no son partidos
NO_PARTIDOS = {"Question", "X mark", "Others", "Other", "Blank", "Undecided", "Abstention"}

DRY_RUN = os.getenv("DRY_RUN") == "1"
RUTA_JSON = Path(os.getenv("RUTA_JSON") or "datos/encuestas.json")
DW_API = "https://api.datawrapper.de/v3"
HEADERS_WIKI = {"User-Agent": "EuropaPress-Encuestas/1.0 (GitHub Actions; datos@europapress.es)"}


# --------------------------------------------------------------------------
# Lectura de Wikipedia
# --------------------------------------------------------------------------
def limpiar(texto: str) -> str:
    texto = (texto or "").replace("\xa0", " ")
    texto = re.sub(r"\[.*?\]", "", texto)  # notas tipo [p 1], [a]
    return re.sub(r"\s+", " ", texto).strip()


class TablaNoEncontrada(RuntimeError):
    pass


def obtener_tabla() -> list:
    """Prueba cada URL hasta encontrar una tabla de estimación de voto."""
    for url in URLS:
        r = requests.get(url, headers=HEADERS_WIKI, timeout=60)
        if r.status_code != 200:
            print(f"Aviso: {url} devolvió {r.status_code}", file=sys.stderr)
            continue
        print(f"Página leída: {r.url}")
        try:
            return leer_tabla_sondeos(r.text)
        except TablaNoEncontrada as e:
            print(f"Aviso: {e} en {r.url}", file=sys.stderr)
    raise RuntimeError("Ninguna página de Wikipedia tiene la tabla de estimación de voto")


def _int(valor) -> int:
    m = re.match(r"\d+", str(valor or "1"))
    return int(m.group()) if m else 1


def expandir_tabla(tabla):
    """Convierte la tabla en una rejilla resolviendo rowspan y colspan
    (lo que hace html_table(fill = TRUE) en R)."""
    filas = [tr for tr in tabla.find_all("tr") if tr.find_parent("table") is tabla]
    rejilla, arrastre = [], {}  # arrastre: col -> (filas restantes, celda)
    for tr in filas:
        fila = {}
        for col, (restantes, celda) in list(arrastre.items()):
            fila[col] = celda
            if restantes - 1 <= 0:
                del arrastre[col]
            else:
                arrastre[col] = (restantes - 1, celda)
        col = 0
        for celda in tr.find_all(["th", "td"], recursive=False):
            while col in fila:
                col += 1
            rs, cs = _int(celda.get("rowspan")), _int(celda.get("colspan"))
            for k in range(cs):
                fila[col + k] = celda
                if rs > 1:
                    arrastre[col + k] = (rs - 1, celda)
            col += cs
        if fila:
            rejilla.append([fila.get(i) for i in range(max(fila) + 1)])
    return rejilla


def texto_cabecera(th) -> str:
    """Texto de la cabecera; si es un logo de partido, usa el alt de la imagen."""
    if th is None:
        return ""
    t = limpiar(th.get_text(" ", strip=True))
    if t:
        return t
    img = th.find("img")
    if img and img.get("alt"):
        return limpiar(img["alt"])
    a = th.find("a")
    return re.sub(r"\s*\(.*?\)$", "", limpiar(a.get("title", ""))) if a else ""


def anyo_seccion(tabla):
    """Año de la tabla: caption, o un texto que sea solo un año ('2026') entre la
    tabla anterior y esta (título de sección, negrita, div...). None si no hay."""
    cap = tabla.find("caption")
    if cap:
        m = re.search(r"\b(20\d{2})\b", limpiar(cap.get_text(" ", strip=True)))
        if m:
            return m.group(1)
    for i, el in enumerate(tabla.previous_elements):
        if i > 400:
            break
        if isinstance(el, NavigableString):
            padre = el.find_parent("table")
            if padre is not None and padre is not tabla:
                break  # hemos llegado a la tabla anterior
            t = limpiar(str(el))
            if re.fullmatch(r"20\d{2}", t):
                return t
    return None


def titulo_seccion(tabla) -> str:
    h = tabla.find_previous(["h2", "h3", "h4"])
    return limpiar(h.get_text(" ", strip=True)) if h else ""


ESCENARIOS = re.compile(r"hypothetical|scenario|alternative|if .* ran", re.I)


def anyo_mas_frecuente(fechas: pd.Series):
    anyos = fechas.str.findall(r"\b(20\d{2})\b").explode().dropna()
    return anyos.mode().iloc[0] if not anyos.empty else None


def leer_tabla_sondeos(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    # Fuera notas al pie y textos ocultos (claves de ordenación)
    for el in soup.select("sup.reference, [style*='display:none'], [style*='display: none']"):
        el.decompose()

    candidatas = []
    for tabla in soup.select("table.wikitable"):
        rejilla = expandir_tabla(tabla)
        if not rejilla:
            continue
        cab = [texto_cabecera(c) for c in rejilla[0]]
        if (any(c.startswith("Polling firm") for c in cab)
                and any(c.startswith("Fieldwork") for c in cab)
                and "PP" in cab and "PSOE" in cab):
            # Solo las tablas de estimación de voto tienen columna Turnout;
            # las de voto directo, preferencias, etc. se descartan
            valida = any(c.startswith("Turnout") for c in cab)
            seccion = anyo_seccion(tabla)
            titulo = titulo_seccion(tabla)
            escenario = bool(ESCENARIOS.search(titulo))
            estado = "OK" if valida and not escenario else ("escenario" if escenario else "sin Turnout")
            print(f"  Tabla {estado} | año {seccion or '-'} | título «{titulo}» "
                  f"| {len(rejilla)} filas | {cab}")
            if valida and not escenario:
                candidatas.append((cab, rejilla, seccion))
    if not candidatas:
        raise TablaNoEncontrada("No hay tabla de estimación de voto (con columna Turnout)")

    # Las tablas sin año detectado se conservan (pueden llevar el año en las fechas)
    # salvo que tengan partidos que no aparecen en ninguna tabla anual: eso indica
    # un escenario hipotético (p. ej. FA en lugar de Sumar + Podemos).
    def partidos_de(cab):
        i0 = next((i for i, c in enumerate(cab) if c.startswith("Turnout")), 0)
        i1 = next((i for i, c in enumerate(cab) if c.startswith("Lead")), len(cab))
        return {c for c in cab[i0 + 1:i1] if c not in NO_PARTIDOS}
    conocidos = set().union(*[partidos_de(c) for c, _, sec in candidatas if sec])
    if conocidos:
        filtradas = []
        for c in candidatas:
            extra = partidos_de(c[0]) - conocidos
            if c[2] or not extra:
                filtradas.append(c)
            else:
                print(f"  Descartada tabla sin año con partidos no habituales: {sorted(extra)}")
        candidatas = filtradas
    print(f"Años detectados: {[sec for _, _, sec in candidatas]}")

    # Wikipedia parte la tabla por años (2026, 2025...) y cada una puede tener
    # columnas distintas, así que se devuelven todas y se procesan por separado
    tablas = []
    for cab, rejilla, seccion in candidatas:
        cab = [c or f"col_{i}" for i, c in enumerate(cab)]
        registros = []
        for fila in rejilla[1:]:
            fila = fila + [None] * (len(cab) - len(fila))
            if len(fila) != len(cab) or all(x is None or x.name == "th" for x in fila):
                continue  # filas de cabecera / colores
            registros.append([limpiar(x.get_text(" ", strip=True)) if x is not None else "" for x in fila])
        if registros:
            t = pd.DataFrame(registros, columns=cab)
            t.attrs["anyo"] = seccion
            tablas.append(t)
    print(f"Tablas usadas: {len(tablas)} | Filas totales: {sum(len(t) for t in tablas)}")
    return tablas


# --------------------------------------------------------------------------
# Limpieza
# --------------------------------------------------------------------------
def parse_fecha_campo(x: str, anyo_defecto=None):
    """'24–26 Sep 2026' -> 2026-09-26 ; '28 Dec 2025–3 Jan 2026' -> 2026-01-03
    '29 Sep–1 Oct' en la tabla de 2026 -> 2026-10-01 (año de la sección)"""
    x = limpiar(x)
    segunda = re.split(r"[–—-]", x)[-1].strip()
    if not re.search(r"\d{4}", segunda):
        anyo = re.search(r"(\d{4})$", x)
        if anyo:
            segunda = f"{segunda} {anyo.group(1)}"
        elif anyo_defecto:
            segunda = f"{segunda} {anyo_defecto}"
        else:
            return pd.NaT
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return pd.to_datetime(segunda, format=fmt)
        except (ValueError, TypeError):
            pass
    return pd.to_datetime(segunda, errors="coerce", dayfirst=True)


# porcentaje (o ?) + escaños opcionales (137, 134/146, 134–146)
PATRON_VOTO = re.compile(r"^(\?|\d{1,3}(?:\.\d)?)\s*(\d+(?:\s*[/–-]\s*\d+)?)?$")


def parse_voto(celda: str):
    m = PATRON_VOTO.match(celda or "")
    if not m:
        return None, None
    pct = None if m.group(1) == "?" else float(m.group(1))
    esc = re.sub(r"\s*[/–-]\s*", "/", m.group(2)) if m.group(2) else None
    return pct, esc


def tabla_a_largo(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(df.columns)
    c_enc = cols[0]
    c_fecha = next(c for c in cols if c.startswith("Fieldwork"))
    anyo = df.attrs.get("anyo") or anyo_mas_frecuente(df[c_fecha])
    c_muestra = next(c for c in cols if c.startswith("Sample"))
    c_part = next((c for c in cols if c.startswith("Turnout")), c_muestra)
    c_lead = next((c for c in cols if c.startswith("Lead")), None)
    i_fin = cols.index(c_lead) if c_lead else len(cols)
    partidos = [c for c in cols[cols.index(c_part) + 1:i_fin] if c not in NO_PARTIDOS]
    print(f"  Tabla {anyo or '-'} | partidos: {partidos}")

    df = df.rename(columns={c_enc: "Encuestador", c_muestra: "Muestra"})
    df["Fecha_fin"] = df[c_fecha].map(lambda x: parse_fecha_campo(x, anyo))
    df = df[["Encuestador", "Fecha_fin", "Muestra"] + partidos]

    return df.melt(id_vars=["Encuestador", "Fecha_fin", "Muestra"],
                   var_name="Partidos", value_name="Votos")


def preparar_encuestas(tablas: list) -> pd.DataFrame:
    largo = pd.concat([tabla_a_largo(t) for t in tablas], ignore_index=True)
    largo = largo.drop_duplicates()
    largo["Muestra"] = largo["Muestra"].str.replace(",", "", regex=False)
    largo["Partidos"] = largo["Partidos"].replace(RENOMBRAR_PARTIDOS)

    pv = largo["Votos"].map(parse_voto)
    largo["Porcentaje"] = pv.map(lambda t: t[0]).astype("Float64")
    largo["Diputados"] = pv.map(lambda t: t[1])

    if SOLO_CON_ESCANOS:
        largo = largo[largo["Diputados"].notna()]
    else:
        largo = largo[largo["Porcentaje"].notna() | largo["Diputados"].notna()]

    largo["Encuestador"] = largo["Encuestador"].map(limpiar)
    es_resultado = largo["Encuestador"].str.contains(r"\belection\b", case=False, na=False)
    largo = largo[~es_resultado
                  & ~largo["Encuestador"].isin(EXCLUIR_ENCUESTADORES)
                  & (largo["Encuestador"] != "")]

    sin_fecha = largo["Fecha_fin"].isna()
    if sin_fecha.any():
        print("Aviso: fechas no reconocidas, se descartan:",
              sorted(largo.loc[sin_fecha, "Encuestador"].unique())[:10], file=sys.stderr)
        largo = largo[~sin_fecha]

    largo = largo.sort_values(["Fecha_fin", "Encuestador", "Porcentaje"],
                              ascending=[False, True, False], na_position="last")
    return largo.reset_index(drop=True)


def media_movil(df: pd.DataFrame) -> pd.DataFrame:
    m = (df.dropna(subset=["Porcentaje"])
           .groupby(["Fecha_fin", "Partidos"], as_index=False)["Porcentaje"].mean()
           .sort_values(["Partidos", "Fecha_fin"]))
    m["PorcentajeMe"] = (m.groupby("Partidos")["Porcentaje"]
                          .transform(lambda s: s.rolling(VENTANA_MEDIA, min_periods=VENTANA_MEDIA).mean()))
    ancho = (m.dropna(subset=["PorcentajeMe"])
              .pivot(index="Fecha_fin", columns="Partidos", values="PorcentajeMe")
              .reset_index())
    ancho.columns.name = None
    if "PP" not in ancho:
        return ancho.iloc[0:0]
    ancho = ancho[ancho["PP"].notna() & ancho["Fecha_fin"].dt.year.isin(ANYOS_MEDIA)]
    return ancho.reset_index(drop=True)


def media_semanal(df: pd.DataFrame) -> pd.DataFrame:
    s = df.dropna(subset=["Porcentaje"]).copy()
    iso = s["Fecha_fin"].dt.isocalendar()  # año ISO, para que no falle en el cambio de año
    s["Fecha_fin"] = iso["year"].astype(str) + " S" + iso["week"].astype(str).str.zfill(2)
    ancho = (s.groupby(["Fecha_fin", "Partidos"], as_index=False)["Porcentaje"].mean()
              .pivot(index="Fecha_fin", columns="Partidos", values="Porcentaje")
              .reset_index())
    ancho.columns.name = None
    if "PP" not in ancho:
        return ancho.iloc[0:0]
    ancho = ancho[ancho["Fecha_fin"].str[:4].isin(ANYOS_SEMANAL) & ancho["PP"].notna()]
    return ancho.reset_index(drop=True)


# --------------------------------------------------------------------------
# Datawrapper
# --------------------------------------------------------------------------
def dw_subir_y_publicar(chart_id: str, df: pd.DataFrame, nombre: str):
    if df.empty:
        print(f"Aviso: '{nombre}' sin datos; no se actualiza el gráfico", file=sys.stderr)
        return
    csv = df.to_csv(index=False, date_format="%Y-%m-%d")
    if DRY_RUN:
        Path("salida").mkdir(exist_ok=True)
        Path(f"salida/{nombre}.csv").write_text(csv, encoding="utf-8")
        print(f"[DRY_RUN] salida/{nombre}.csv ({len(df)} filas)")
        return
    token = os.getenv("DATAWRAPPER_API_KEY")
    if chart_id == "XXXXX" or not token:
        print(f"Datawrapper no configurado para '{nombre}'; se omite")
        return
    auth = {"Authorization": f"Bearer {token}"}
    r = requests.put(f"{DW_API}/charts/{chart_id}/data", data=csv.encode("utf-8"),
                     headers={**auth, "Content-Type": "text/csv; charset=utf-8"}, timeout=60)
    r.raise_for_status()
    r = requests.post(f"{DW_API}/charts/{chart_id}/publish", headers=auth, timeout=120)
    r.raise_for_status()
    print(f"Publicado {nombre} ({chart_id}): {len(df)} filas")


# --------------------------------------------------------------------------
# JSON para desarrollo propio
# --------------------------------------------------------------------------
def _num(v, decimales=2):
    return None if pd.isna(v) else round(float(v), decimales)


def _escanos(txt):
    if txt is None or pd.isna(txt):
        return None, None
    n = [int(x) for x in str(txt).split("/")]
    return n[0], n[-1]


def _muestra(txt):
    return int(txt) if isinstance(txt, str) and txt.isdigit() else None


def construir_json(encuestas, media, semana) -> dict:
    lista = []
    for (enc, fecha, muestra), g in encuestas.groupby(
            ["Encuestador", "Fecha_fin", "Muestra"], sort=False, dropna=False):
        resultados = []
        for _, r in g.iterrows():
            e_min, e_max = _escanos(r["Diputados"])
            resultados.append({"partido": r["Partidos"], "porcentaje": _num(r["Porcentaje"], 1),
                               "escanos": r["Diputados"], "escanos_min": e_min, "escanos_max": e_max})
        lista.append({"encuestador": enc, "fecha": fecha.strftime("%Y-%m-%d"),
                      "muestra": _muestra(muestra), "resultados": resultados})

    def ancho_a_lista(df, clave, fmt=None):
        partidos = [c for c in df.columns if c != "Fecha_fin"]
        return [{clave: (f.strftime(fmt) if fmt else f),
                 **{p: _num(r[p]) for p in partidos}}
                for f, (_, r) in zip(df["Fecha_fin"], df.iterrows())]

    return {
        "fuente": URLS[0],
        "partidos": sorted(encuestas["Partidos"].unique().tolist()),
        "media_movil": {"ventana": VENTANA_MEDIA,
                        "datos": ancho_a_lista(media, "fecha", "%Y-%m-%d")},
        "media_semanal": {"datos": ancho_a_lista(semana, "semana")},
        "encuestas": lista,
    }


def guardar_json(datos: dict) -> bool:
    """Escribe el JSON solo si los datos han cambiado (así no hay commits vacíos)."""
    if RUTA_JSON.exists():
        try:
            previo = json.loads(RUTA_JSON.read_text(encoding="utf-8"))
            previo.pop("actualizado", None)
            if previo == json.loads(json.dumps(datos, ensure_ascii=False)):
                print(f"{RUTA_JSON}: sin cambios")
                return False
        except (json.JSONDecodeError, OSError):
            pass
    salida = {"actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"), **datos}
    RUTA_JSON.parent.mkdir(parents=True, exist_ok=True)
    RUTA_JSON.write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{RUTA_JSON}: actualizado ({RUTA_JSON.stat().st_size // 1024} KB, {len(datos['encuestas'])} encuestas)")
    return True


def main():
    encuestas = preparar_encuestas(obtener_tabla())
    if encuestas.empty:
        raise RuntimeError("No hay encuestas tras la limpieza; revisa la estructura de la tabla")

    unicas = encuestas.drop_duplicates(["Encuestador", "Fecha_fin"])
    print("Encuestas por año:", unicas.groupby(unicas["Fecha_fin"].dt.year).size().to_dict())
    print("Más reciente:", encuestas["Fecha_fin"].max().date())
    media, semana = media_movil(encuestas), media_semanal(encuestas)
    guardar_json(construir_json(encuestas, media, semana))

    dw_subir_y_publicar(CHART_ENCUESTAS, encuestas, "encuestas")
    dw_subir_y_publicar(CHART_MEDIA, media, "media_movil")
    dw_subir_y_publicar(CHART_SEMANAL, semana, "media_semanal")


if __name__ == "__main__":
    main()
