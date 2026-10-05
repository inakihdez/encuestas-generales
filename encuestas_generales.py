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

import os
import re
import sys
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

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


def obtener_tabla() -> pd.DataFrame:
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
    return limpiar(a.get("title", "")) if a else ""


def leer_tabla_sondeos(html: str) -> pd.DataFrame:
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
            print(f"  Tabla {'OK ' if valida else 'descartada'} | {len(rejilla)} filas | {cab}")
            if valida:
                candidatas.append((cab, rejilla))
    if not candidatas:
        raise TablaNoEncontrada("No hay tabla de estimación de voto (con columna Turnout)")

    # La tabla principal es la más larga; se suman las que tengan la misma cabecera
    # (por si Wikipedia la parte por años)
    cab = max(candidatas, key=lambda x: len(x[1]))[0]
    cab = [c or f"col_{i}" for i, c in enumerate(cab)]
    registros = []
    for c, rejilla in candidatas:
        if [x or f"col_{i}" for i, x in enumerate(c)] != cab:
            continue
        for fila in rejilla[1:]:
            fila = fila + [None] * (len(cab) - len(fila))
            if len(fila) != len(cab) or all(x is None or x.name == "th" for x in fila):
                continue  # filas de cabecera / colores
            registros.append([limpiar(x.get_text(" ", strip=True)) if x is not None else "" for x in fila])

    df = pd.DataFrame(registros, columns=cab)
    print(f"Filas leídas: {len(df)} | Columnas: {cab}")
    return df


# --------------------------------------------------------------------------
# Limpieza
# --------------------------------------------------------------------------
def parse_fecha_campo(x: str):
    """'24–26 Sep 2026' -> 2026-09-26 ; '28 Dec 2025–3 Jan 2026' -> 2026-01-03"""
    x = limpiar(x)
    segunda = re.split(r"[–—-]", x)[-1].strip()
    if not re.search(r"\d{4}", segunda):
        anyo = re.search(r"(\d{4})$", x)
        if anyo:
            segunda = f"{segunda} {anyo.group(1)}"
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


def preparar_encuestas(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(df.columns)
    c_enc = cols[0]
    c_fecha = next(c for c in cols if c.startswith("Fieldwork"))
    c_muestra = next(c for c in cols if c.startswith("Sample"))
    c_part = next((c for c in cols if c.startswith("Turnout")), c_muestra)
    c_lead = next((c for c in cols if c.startswith("Lead")), None)
    i_fin = cols.index(c_lead) if c_lead else len(cols)
    partidos = [c for c in cols[cols.index(c_part) + 1:i_fin] if c not in NO_PARTIDOS]
    print(f"Partidos: {partidos}")

    df = df.rename(columns={c_enc: "Encuestador", c_muestra: "Muestra"})
    df["Fecha_fin"] = df[c_fecha].map(parse_fecha_campo)
    df = df[["Encuestador", "Fecha_fin", "Muestra"] + partidos]

    largo = df.melt(id_vars=["Encuestador", "Fecha_fin", "Muestra"],
                    var_name="Partidos", value_name="Votos")
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
              sorted(df.loc[df["Fecha_fin"].isna(), "Encuestador"].unique())[:10], file=sys.stderr)
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
    ancho = ancho[ancho["Fecha_fin"].str[:4].isin(ANYOS_SEMANAL) & ancho["PP"].notna()]
    return ancho.reset_index(drop=True)


# --------------------------------------------------------------------------
# Datawrapper
# --------------------------------------------------------------------------
def dw_subir_y_publicar(chart_id: str, df: pd.DataFrame, nombre: str):
    csv = df.to_csv(index=False, date_format="%Y-%m-%d")
    if DRY_RUN:
        Path("salida").mkdir(exist_ok=True)
        Path(f"salida/{nombre}.csv").write_text(csv, encoding="utf-8")
        print(f"[DRY_RUN] salida/{nombre}.csv ({len(df)} filas)")
        return
    if chart_id == "XXXXX":
        raise RuntimeError(f"Falta el id del gráfico '{nombre}'")
    token = os.environ["DATAWRAPPER_API_KEY"]
    auth = {"Authorization": f"Bearer {token}"}
    r = requests.put(f"{DW_API}/charts/{chart_id}/data", data=csv.encode("utf-8"),
                     headers={**auth, "Content-Type": "text/csv; charset=utf-8"}, timeout=60)
    r.raise_for_status()
    r = requests.post(f"{DW_API}/charts/{chart_id}/publish", headers=auth, timeout=120)
    r.raise_for_status()
    print(f"Publicado {nombre} ({chart_id}): {len(df)} filas")


def main():
    encuestas = preparar_encuestas(obtener_tabla())
    if encuestas.empty:
        raise RuntimeError("No hay encuestas tras la limpieza; revisa la estructura de la tabla")

    dw_subir_y_publicar(CHART_ENCUESTAS, encuestas, "encuestas")
    dw_subir_y_publicar(CHART_MEDIA, media_movil(encuestas), "media_movil")
    dw_subir_y_publicar(CHART_SEMANAL, media_semanal(encuestas), "media_semanal")


if __name__ == "__main__":
    main()
