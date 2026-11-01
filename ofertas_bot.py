# -*- coding: utf-8 -*-
"""
Bot de ofertas de juegos para PC (España) -> avisos por ntfy.

Fuentes gratuitas y públicas (sin IA ni servicios de pago):
  - Epic Games Store: juegos gratis de la semana y rebajas (precios en €, país ES).
  - Steam: juegos al 100 % (gratis para quedártelos), ofertas por menos del límite
    y "fines de semana gratis" anunciados en la portada de Steam.
  - GamerPower (opcional): sorteos/regalos gratis para siempre en Steam y Epic.

Uso:
  python ofertas_bot.py            -> una revisión y envía las novedades
  python ofertas_bot.py --bucle    -> revisa cada X horas (config.ini) sin parar
  python ofertas_bot.py --test     -> envía una notificación de prueba
  python ofertas_bot.py --simular  -> revisa y muestra en pantalla, SIN enviar nada
"""
import argparse
import configparser
import difflib
import datetime as dt
import html
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    print("Falta la librería 'requests'. Ejecuta INSTALAR.bat (o: pip install requests).")
    sys.exit(1)

CARPETA = os.path.dirname(os.path.abspath(__file__))
RUTA_CONFIG = os.path.join(CARPETA, "config.ini")
RUTA_VISTOS = os.path.join(CARPETA, "seen.json")
RUTA_LOG = os.path.join(CARPETA, "registro.log")

CAT_GRATIS = "Gratis para siempre"
CAT_PRUEBA = "Prueba temporal / fin de semana gratis"
CAT_OFERTA = "Oferta < 10 €"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ofertas-bot/1.0",
      "Accept-Language": "es-ES,es;q=0.9"}
TZ_MADRID = None
try:
    from zoneinfo import ZoneInfo
    TZ_MADRID = ZoneInfo("Europe/Madrid")
except Exception:
    TZ_MADRID = None


# ----------------------------------------------------------------- utilidades
def log(msg):
    linea = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(linea)
    except Exception:
        pass
    try:
        with open(RUTA_LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
        if os.path.getsize(RUTA_LOG) > 1_000_000:  # evita que crezca sin límite
            os.replace(RUTA_LOG, RUTA_LOG + ".old")
    except Exception:
        pass


def cargar_config():
    cfg = configparser.ConfigParser()
    cfg.read(RUTA_CONFIG, encoding="utf-8")
    g = lambda k, d: cfg.get("bot", k, fallback=d)
    c = {
        "tema": os.environ.get("NTFY_TOPIC", "").strip() or g("tema_ntfy", "").strip(),
        "servidor": g("servidor_ntfy", "https://ntfy.sh").rstrip("/"),
        "horas": float(g("intervalo_horas", "6")),
        "limite": float(g("precio_maximo_euros", "10").replace(",", ".")),
        "descuento_min": int(g("descuento_minimo", "1")),
        "max_steam": int(g("max_ofertas_steam", "60")),
        "resenas_min": int(g("valoracion_minima_steam", "80")),
        "max_avisos": int(g("max_avisos_por_revision", "15")),
        "epic": g("usar_epic", "si").lower().startswith("s"),
        "steam": g("usar_steam", "si").lower().startswith("s"),
        "gamerpower": g("usar_gamerpower", "si").lower().startswith("s"),
        "incluir_dlc": g("incluir_dlc_gratis", "no").lower().startswith("s"),
    }
    return c


def euros(cent):
    if cent is None:
        return "?"
    return f"{cent / 100:.2f} €".replace(".", ",")


def fecha_es(iso_o_ts):
    """Devuelve la fecha en hora de Madrid, p. ej. '05/11/2026 17:00'."""
    if not iso_o_ts:
        return ""
    try:
        if isinstance(iso_o_ts, (int, float)):
            d = dt.datetime.fromtimestamp(iso_o_ts, dt.timezone.utc)
        else:
            s = str(iso_o_ts).replace("Z", "+00:00")
            d = dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
        if TZ_MADRID:
            d = d.astimezone(TZ_MADRID)
        return d.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(iso_o_ts)


def ahora_utc():
    return dt.datetime.now(dt.timezone.utc)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- Epic Games
def buscar_epic(cfg):
    url = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
           "?locale=es-ES&country=ES&allowCountries=ES")
    datos = get(url).json()
    elementos = datos["data"]["Catalog"]["searchStore"]["elements"]
    res = []
    ahora = ahora_utc()
    for e in elementos:
        titulo = e.get("title", "?")
        precio = (e.get("price") or {}).get("totalPrice") or {}
        original = precio.get("originalPrice")
        final = precio.get("discountPrice")
        # promoción activa ahora mismo
        promo_activa = None
        for bloque in ((e.get("promotions") or {}).get("promotionalOffers") or []):
            for o in bloque.get("promotionalOffers") or []:
                try:
                    ini = dt.datetime.fromisoformat(o["startDate"].replace("Z", "+00:00"))
                    fin = dt.datetime.fromisoformat(o["endDate"].replace("Z", "+00:00"))
                except Exception:
                    continue
                if ini <= ahora <= fin:
                    promo_activa = o
        if not promo_activa or original is None or final is None:
            continue
        # enlace oficial
        slug = None
        for m in ((e.get("catalogNs") or {}).get("mappings") or []):
            if m.get("pageSlug"):
                slug = m["pageSlug"]
                break
        if not slug:
            for m in (e.get("offerMappings") or []):
                if m.get("pageSlug"):
                    slug = m["pageSlug"]
                    break
        if not slug and e.get("productSlug"):
            slug = e["productSlug"].split("/")[0]
        enlace = (f"https://store.epicgames.com/es-ES/p/{slug}" if slug
                  else "https://store.epicgames.com/es-ES/free-games")
        if final == 0 and original > 0:
            cat = CAT_GRATIS
        elif 0 < final <= cfg["limite"] * 100 and final < original:
            cat = CAT_OFERTA
        else:
            continue
        dto = round(100 - final * 100 / original) if original else 0
        if cat == CAT_OFERTA and dto < cfg["descuento_min"]:
            continue
        res.append({
            "tienda": "Epic", "id": e.get("id") or slug or titulo, "nombre": titulo,
            "antes": original, "ahora": final, "descuento": dto,
            "fin": promo_activa.get("endDate"), "enlace": enlace, "categoria": cat,
        })
    return res


# ----------------------------------------------------------------- Steam
PATRON_FILA = re.compile(
    r'<a href="(?P<href>https://store\.steampowered\.com/(?:app|sub|bundle)/[^"]+)"'
    r'(?P<attrs>[^>]*)>(?P<cuerpo>.*?)</a>', re.S)


def _precio_a_cent(txt):
    txt = html.unescape(txt or "").replace("€", "").replace("\xa0", "").strip()
    txt = txt.replace(".", "").replace(",", ".")
    try:
        return int(round(float(txt) * 100))
    except ValueError:
        return None


def _filas_busqueda_steam(params):
    base = "https://store.steampowered.com/search/results/"
    p = {"query": "", "start": 0, "count": 50, "cc": "es", "l": "spanish",
         "infinite": 1, "category1": "998,21", "specials": 1}
    p.update(params)
    datos = get(base, params=p).json()
    filas = []
    for m in PATRON_FILA.finditer(datos.get("results_html", "")):
        attrs, cuerpo = m.group("attrs"), m.group("cuerpo")
        idm = re.search(r'data-ds-(?:appid|packageid|bundleid)="([\d,]+)"', attrs)
        tit = re.search(r'<span class="title">(.*?)</span>', cuerpo, re.S)
        dto = re.search(r'data-discount="(\d+)"', cuerpo)
        orig = re.search(r'discount_original_price">(.*?)<', cuerpo)
        fin = re.search(r'discount_final_price[^>]*>(.*?)<', cuerpo)
        rev = re.search(r'El (\d+)\s*%\s*de las ([\d.,]+) rese', html.unescape(cuerpo))
        if not (idm and tit and fin):
            continue
        enlace = m.group("href").split("?")[0]
        filas.append({
            "id": idm.group(1), "nombre": html.unescape(tit.group(1)).strip(),
            "descuento": int(dto.group(1)) if dto else 0,
            "antes": _precio_a_cent(orig.group(1)) if orig else None,
            "ahora": _precio_a_cent(fin.group(1)),
            "resenas_pct": int(rev.group(1)) if rev else None,
            "enlace": enlace,
        })
    return filas


def buscar_steam(cfg):
    res = []
    fines = {}
    destacados = {}
    try:
        feat = get("https://store.steampowered.com/api/featuredcategories",
                   params={"cc": "es", "l": "spanish"}).json()
        for clave, bloque in feat.items():
            if not isinstance(bloque, dict):
                continue
            for it in bloque.get("items") or []:
                if it.get("id") and it.get("discount_expiration"):
                    fines[str(it["id"])] = it["discount_expiration"]
                destacados[clave] = bloque
    except Exception as ex:
        log(f"Steam portada: error {ex}")
        feat = {}

    # 1) Gratis para siempre: juegos al 100 % de descuento
    for f in _filas_busqueda_steam({"maxprice": "free", "category1": ""}):
        if f["descuento"] >= 100 or (f["ahora"] == 0 and f["antes"]):
            res.append({"tienda": "Steam", "id": f["id"], "nombre": f["nombre"],
                        "antes": f["antes"], "ahora": 0, "descuento": 100,
                        "fin": fines.get(f["id"]), "enlace": f["enlace"],
                        "categoria": CAT_GRATIS})

    # 2) Ofertas por debajo del límite, ordenadas por mejores reseñas
    vistos_ids = set()
    inicio = 0
    while len([r for r in res if r["categoria"] == CAT_OFERTA]) < cfg["max_steam"] and inicio < 300:
        filas = _filas_busqueda_steam({"maxprice": str(int(cfg["limite"])) if cfg["limite"] >= 1 else "free",
                                       "sort_by": "Reviews_DESC", "start": inicio})
        if not filas:
            break
        inicio += 50
        for f in filas:
            if f["id"] in vistos_ids or f["ahora"] is None or f["antes"] is None:
                continue
            vistos_ids.add(f["id"])
            if not (0 < f["ahora"] <= cfg["limite"] * 100) or f["descuento"] < cfg["descuento_min"]:
                continue
            if f["resenas_pct"] is not None and f["resenas_pct"] < cfg["resenas_min"]:
                continue
            res.append({"tienda": "Steam", "id": f["id"], "nombre": f["nombre"],
                        "antes": f["antes"], "ahora": f["ahora"], "descuento": f["descuento"],
                        "fin": fines.get(f["id"]), "enlace": f["enlace"],
                        "categoria": CAT_OFERTA})
            if len([r for r in res if r["categoria"] == CAT_OFERTA]) >= cfg["max_steam"]:
                break

    # 3) Fines de semana gratis / pruebas: Steam los anuncia en la portada
    palabras = ("gratis", "free weekend", "play for free", "juega gratis", "free to play this")
    for clave, bloque in (feat or {}).items():
        if not isinstance(bloque, dict):
            continue
        for it in bloque.get("items") or []:
            texto = f"{it.get('name', '')} {it.get('body', '')}".lower()
            if any(p in texto for p in palabras) and it.get("url"):
                m = re.search(r"/app/(\d+)", it["url"])
                ident = m.group(1) if m else it["url"]
                res.append({"tienda": "Steam", "id": ident,
                            "nombre": html.unescape(it.get("name", "Fin de semana gratis")),
                            "antes": None, "ahora": None, "descuento": None,
                            "fin": it.get("discount_expiration"), "enlace": it["url"].split("?")[0],
                            "categoria": CAT_PRUEBA})
    return res


MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}


def completar_fin_steam(i):
    """Lee la página oficial del juego en Steam para saber cuándo acaba la oferta."""
    if i["tienda"] != "Steam" or i.get("fin") or i.get("fuente") or not str(i["id"]).isdigit():
        return
    try:
        r = requests.get(f"https://store.steampowered.com/app/{i['id']}/",
                         params={"cc": "es", "l": "spanish"}, headers=UA, timeout=30,
                         cookies={"birthtime": "0", "lastagecheckage": "1-0-1990",
                                  "wants_mature_content": "1"})
        t = r.text
    except Exception:
        return
    m = re.search(r"InitDailyDealTimer\(\s*\$?\w+\s*,\s*(\d{9,11})\s*\)", t)
    if m:
        i["fin"] = int(m.group(1))
        return
    m = re.search(r"La oferta (?:finaliza|termina) el (\d{1,2}) de (\w+)", t)
    if m and m.group(2).lower() in MESES:
        i["fin_texto"] = f"{m.group(1)} de {m.group(2).lower()}"
        return
    m = re.search(r"gratis si lo añades a tu cuenta antes del ([^.<]{3,40})", t)
    if m:
        i["fin_texto"] = f"antes del {m.group(1).strip()} (hora que indica Steam)"


# ----------------------------------------------------------------- GamerPower
def buscar_gamerpower(cfg):
    res = []
    for plat, tienda in (("steam", "Steam"), ("epic-games-store", "Epic")):
        try:
            r = requests.get("https://www.gamerpower.com/api/giveaways",
                             params={"platform": plat}, headers=UA, timeout=30)
            datos = r.json()
        except Exception as ex:
            log(f"GamerPower {plat}: error {ex}")
            continue
        if not isinstance(datos, list):
            continue
        for g in datos:
            tipo = (g.get("type") or "").lower()
            if tipo == "game":
                pass
            elif tipo == "dlc" and cfg["incluir_dlc"]:
                pass
            else:
                continue  # se ignoran playtests, betas, etc.
            if (g.get("status") or "Active").lower() != "active":
                continue
            titulo = g.get("title", "")
            titulo = re.sub(r"\s*(\((Steam|Epic Games)\)\s*)?(Steam Key|Key)?\s*Giveaway$", "", titulo)
            titulo = re.sub(r"\s*\((Steam|Epic Games)\)\s*(Key)?$|\s*Steam Key$", "", titulo).strip()
            fin = g.get("end_date")
            fin = None if not fin or fin == "N/A" else fin.replace(" ", "T") + "+00:00"
            if fin:
                try:
                    if dt.datetime.fromisoformat(fin) < ahora_utc():
                        continue  # ya caducado
                except ValueError:
                    pass
            res.append({"tienda": tienda, "id": "gp" + str(g.get("id")), "nombre": titulo,
                        "antes": None, "ahora": 0, "descuento": 100, "valor": g.get("worth"),
                        "fin": fin,
                        "enlace": ("https://store.epicgames.com/es-ES/free-games" if tienda == "Epic"
                                   else g.get("open_giveaway_url") or g.get("gamerpower_url")),
                        "categoria": CAT_GRATIS, "es_dlc": tipo == "dlc",
                        "fuente": "GamerPower"})
    return res


# ----------------------------------------------------------------- unir y avisar
def normaliza(n):
    return re.sub(r"[^a-z0-9]", "", (n or "").lower())


def recoger(cfg):
    items = []
    for activo, nombre, fn in ((cfg["epic"], "Epic", buscar_epic),
                               (cfg["steam"], "Steam", buscar_steam),
                               (cfg["gamerpower"], "GamerPower", buscar_gamerpower)):
        if not activo:
            continue
        try:
            encontrados = fn(cfg)
            log(f"{nombre}: {len(encontrados)} elementos")
            items.extend(encontrados)
        except Exception as ex:
            log(f"{nombre}: error al consultar ({ex})")
    # quitar duplicados de GamerPower si ya lo tenemos desde la tienda oficial
    oficiales = {(i["tienda"], normaliza(i["nombre"])): i for i in items if i.get("fuente") != "GamerPower"}
    final = []
    for i in items:
        if i.get("fuente") == "GamerPower":
            clave = (i["tienda"], normaliza(i["nombre"]))
            parecido = [o for (t, n), o in oficiales.items()
                        if t == i["tienda"] and len(n) > 4 and
                        (n == clave[1] or n in clave[1] or clave[1] in n or
                         difflib.SequenceMatcher(None, n, clave[1]).ratio() >= 0.85)]
            if parecido:
                for o in parecido:  # aprovechar la fecha de fin si la tienda no la da
                    if not o.get("fin") and i.get("fin"):
                        o["fin"] = i["fin"]
                continue
        final.append(i)
    return final


def clave_visto(i):
    if i.get("clave"):
        return i["clave"]
    fin = i.get("fin") or f"precio{i.get('ahora')}"
    return f"{i['tienda']}|{i['id']}|{fin}"


def cargar_vistos():
    try:
        with open(RUTA_VISTOS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_vistos(v):
    # olvidar avisos de hace más de 60 días
    limite = time.time() - 60 * 86400
    v = {k: t for k, t in v.items() if t > limite}
    with open(RUTA_VISTOS, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=1, sort_keys=True)


def montar_aviso(i):
    cat = i["categoria"]
    if cat == CAT_GRATIS:
        icono, tags, prio = "🎁", "gift,video_game", "high"
        titulo = f"GRATIS PARA SIEMPRE: {i['nombre']} ({i['tienda']})"
    elif cat == CAT_PRUEBA:
        icono, tags, prio = "⏳", "hourglass,video_game", "default"
        titulo = f"PRUEBA TEMPORAL: {i['nombre']} ({i['tienda']})"
    else:
        icono, tags, prio = "💶", "moneybag,video_game", "default"
        titulo = f"Oferta {euros(i['ahora'])}: {i['nombre']} ({i['tienda']})"
    lineas = [f"{icono} {cat}", f"Juego: {i['nombre']}" + (" (contenido extra/DLC, necesitas el juego base)" if i.get("es_dlc") else "")]
    if i.get("antes") is not None or i.get("ahora") is not None:
        if i.get("antes") is not None:
            antes = euros(i["antes"])
        elif i.get("valor") and i["valor"] != "N/A":
            antes = f"{i['valor']} (valor aprox. en dólares)"
        else:
            antes = "?"
        ahora = euros(i["ahora"]) if i.get("ahora") else "0,00 € (gratis)"
        txt = f"Precio: antes {antes} → ahora {ahora}"
        if i.get("descuento"):
            txt += f" (-{i['descuento']} %)"
        lineas.append(txt)
    if cat == CAT_PRUEBA:
        lineas.append("Se puede jugar gratis unos días, pero NO te lo quedas.")
    elif cat == CAT_GRATIS:
        lineas.append("Si lo reclamas antes del final, es tuyo para siempre.")
    if i.get("fin"):
        lineas.append(f"Termina: {fecha_es(i['fin'])} (hora de Madrid)")
    elif i.get("fin_texto"):
        lineas.append(f"Termina: {i['fin_texto']}")
    else:
        lineas.append("Termina: fecha no publicada")
    lineas.append(f"Enlace: {i['enlace']}")
    if i.get("fuente") == "GamerPower":
        if i["tienda"] == "Epic":
            lineas.append("Fuente: GamerPower. Búscalo en la página de juegos gratis de Epic.")
        else:
            lineas.append("Fuente: GamerPower (puede ser una clave en una web externa; comprueba antes de reclamar)")
    return titulo, "\n".join(lineas), tags, prio


def enviar_ntfy(cfg, titulo, cuerpo, tags="video_game", prioridad="default", enlace=None):
    if not cfg["tema"]:
        raise RuntimeError("No hay tema de ntfy en config.ini")
    cabeceras = {
        "Title": titulo.encode("utf-8"),
        "Tags": tags,
        "Priority": prioridad,
    }
    if enlace:
        cabeceras["Click"] = enlace
    r = requests.post(f"{cfg['servidor']}/{cfg['tema']}", data=cuerpo.encode("utf-8"),
                      headers=cabeceras, timeout=30)
    return r.status_code


def revisar(cfg, simular=False):
    items = recoger(cfg)
    vistos = cargar_vistos()
    orden = {CAT_GRATIS: 0, CAT_PRUEBA: 1, CAT_OFERTA: 2}
    items.sort(key=lambda i: (orden[i["categoria"]], i.get("fuente") == "GamerPower", -(i.get("descuento") or 0)))
    for i in items:
        i["clave"] = clave_visto(i)  # se fija antes de completar datos
    nuevos = [i for i in items if i["clave"] not in vistos]
    if simular:
        nuevos = items
    log(f"Total: {len(items)} ofertas, {len(nuevos)} nuevas")
    enviados = 0
    for i in nuevos:
        if enviados >= cfg["max_avisos"]:
            log(f"Límite de {cfg['max_avisos']} avisos por revisión alcanzado; el resto irá en la próxima.")
            break
        completar_fin_steam(i)
        titulo, cuerpo, tags, prio = montar_aviso(i)
        if simular:
            print("-" * 60)
            print(titulo)
            print(cuerpo)
            enviados += 1
            continue
        try:
            codigo = enviar_ntfy(cfg, titulo, cuerpo, tags, prio, i["enlace"])
        except Exception as ex:
            log(f"Error enviando aviso: {ex}")
            continue
        if codigo == 200:
            vistos[clave_visto(i)] = time.time()
            enviados += 1
            time.sleep(2)  # ser amable con ntfy.sh
        elif codigo == 429:
            log("ntfy: demasiados mensajes hoy (límite gratuito). Se reintentará más tarde.")
            break
        else:
            log(f"ntfy respondió {codigo}")
    if not simular:
        vistos["_ultima_revision"] = time.time()
        guardar_vistos(vistos)
    log(f"Avisos {'mostrados' if simular else 'enviados'}: {enviados}")
    return items


def main():
    ap = argparse.ArgumentParser(description="Bot de ofertas de juegos (España) con avisos por ntfy")
    ap.add_argument("--test", action="store_true", help="envía una notificación de prueba")
    ap.add_argument("--bucle", action="store_true", help="revisa sin parar cada X horas")
    ap.add_argument("--simular", "--dry-run", action="store_true", help="no envía nada, solo muestra")
    a = ap.parse_args()
    cfg = cargar_config()
    if not cfg["tema"] and not a.simular:
        log("ERROR: falta el tema de ntfy (config.ini -> tema_ntfy).")
        sys.exit(2)
    if a.test:
        codigo = enviar_ntfy(cfg, "✅ Prueba del bot de ofertas",
                             "¡Funciona! A partir de ahora recibirás aquí los juegos gratis y las ofertas de Steam y Epic.",
                             "white_check_mark,video_game", "default",
                             "https://store.epicgames.com/es-ES/free-games")
        log(f"Notificación de prueba enviada. Respuesta de ntfy: HTTP {codigo}")
        sys.exit(0 if codigo == 200 else 1)
    if a.bucle:
        # evita que se abran dos bots a la vez
        import socket
        candado = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candado.bind(("127.0.0.1", 47653))
        except OSError:
            log("El bot ya estaba funcionando; no abro otro.")
            sys.exit(0)
        log(f"Bot iniciado. Revisará cada {cfg['horas']} horas.")
        while True:
            try:
                cfg = cargar_config()
                revisar(cfg)
            except Exception as ex:
                log(f"Error inesperado: {ex}")
            time.sleep(max(0.25, cfg["horas"]) * 3600)
    revisar(cfg, simular=a.simular)


if __name__ == "__main__":
    main()
