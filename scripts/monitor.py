# -*- coding: utf-8 -*-
"""
Radar Político Salta - Recolector de menciones en medios digitales.

Qué hace:
1. Para cada político/persona en data/politicians.json, busca noticias
   recientes en Google News, limitado a un grupo de medios de Salta.
2. También lee directamente los RSS de El Tribuno (política y Salta) y
   cruza esas noticias contra la lista de nombres a seguir.
3. Calcula un puntaje de tono (positivo/negativo/neutral) con un método
   simple basado en palabras clave en español (no es Inteligencia
   Artificial "de verdad": es un punto de partida honesto y gratuito).
4. Guarda todo en data/mentions.json y arma data/aggregates.json con
   resúmenes por político y por día, que es lo que consume el panel web.

Este script está pensado para correr repetidamente (por ejemplo cada
15 minutos vía GitHub Actions) sin duplicar noticias ya guardadas.
"""

import os
import re
import json
import hashlib
from collections import defaultdict
from itertools import combinations
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
POLITICIANS_FILE = os.path.join(DATA_DIR, "politicians.json")
TOPICS_FILE = os.path.join(DATA_DIR, "topics.json")
MENTIONS_FILE = os.path.join(DATA_DIR, "mentions.json")
TOPIC_MENTIONS_FILE = os.path.join(DATA_DIR, "topic_mentions.json")
TOPIC_DATA_FILE = os.path.join(DATA_DIR, "topics_data.json")
AGGREGATES_FILE = os.path.join(DATA_DIR, "aggregates.json")
DOSSIERS_FILE = os.path.join(DATA_DIR, "dossiers.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
NEWS_GENERAL_FILE = os.path.join(DATA_DIR, "news_general.json")

# Medios nacionales de alcance amplio, para la sección de noticias
# generales (no atada a políticos puntuales). Usamos Google News en vez
# de intentar adivinar la URL de RSS propia de cada uno.
NATIONAL_SITES = [
    "infobae.com", "clarin.com", "lanacion.com.ar", "pagina12.com.ar",
    "ambito.com", "tn.com.ar", "perfil.com",
]

MAX_GENERAL_ITEMS_STORED = 1200
MAX_GENERAL_ITEMS_PER_FEED = 60

# Medios de Salta verificados a los que restringimos una de las búsquedas
# (alta precisión). Agregá o sacá dominios según lo que quieras cubrir;
# solo agregá dominios que hayas confirmado vos mismo que existen.
SOURCE_SITES = [
    "eltribuno.com",
    "salta12.com.ar",
    "informatesalta.com.ar",
    "elintransigente.com",
    "nuevodiariosalta.com.ar",
    "radiosalta.com.ar",
]

# Departamentos y localidades de la provincia, para una segunda búsqueda
# más amplia (sin restringir a dominios específicos) que capture medios
# locales de cada zona que Google News indexe, aunque no sepamos su URL.
DEPARTMENTS = [
    "Salta capital", "Orán", "Tartagal", "Metán", "Rosario de la Frontera",
    "Cafayate", "General Güemes", "Cachi", "Cerrillos", "Chicoana",
    "Rosario de Lerma", "San Ramón de la Nueva Orán", "Embarcación",
    "Joaquín V. González", "El Carril", "Guachipas", "Iruya", "La Caldera",
    "La Poma", "La Viña", "Los Andes", "San Carlos", "Molinos",
]

# Feeds propios que además leemos completos (no solo por nombre buscado),
# para detectar menciones que Google News podría no traer todavía.
DIRECT_FEEDS = [
    ("El Tribuno - Política", "https://www.eltribuno.com/rss-new/politica.rss"),
    ("El Tribuno - Salta", "https://www.eltribuno.com/rss-new/salta.rss"),
    ("El Tribuno - Municipios", "https://www.eltribuno.com/rss-new/municipios.rss"),
]

# Palabras a ignorar al calcular "temas en tendencia" (muy comunes en
# español y en el lenguaje periodístico, no aportan información).
STOPWORDS = {
    "de", "la", "el", "en", "y", "a", "los", "las", "un", "una", "por",
    "con", "para", "que", "su", "sus", "del", "al", "es", "se", "no",
    "más", "salta", "tras", "sobre", "como", "fue", "fueron", "entre",
    "esta", "este", "estos", "estas", "video", "así", "también", "hoy",
    "qué", "cómo", "gobierno", "provincia", "provincial", "les", "le",
}

MAX_MENTIONS_STORED = 4000
MAX_ITEMS_PER_FEED = 40

# Diccionario de tono muy simple. Es deliberadamente básico: la idea es
# que puedas ampliarlo vos mismo, o más adelante reemplazarlo por una
# llamada a un modelo de lenguaje si querés más precisión.
POSITIVE_WORDS = [
    "elogia", "elogió", "destaca", "destacó", "logro", "logró", "avance",
    "avanzó", "acuerdo", "celebra", "celebró", "aprobó", "impulsa",
    "impulsó", "beneficio", "mejora", "mejoró", "respaldo", "apoyo",
    "reconocimiento", "éxito", "crecimiento", "inversión", "solución",
]

NEGATIVE_WORDS = [
    "crítica", "criticó", "denuncia", "denunció", "escándalo", "polémica",
    "polemizó", "rechazo", "rechazó", "acusación", "acusó", "corrupción",
    "fraude", "renuncia", "renunció", "conflicto", "crisis", "protesta",
    "cuestionó", "cuestionamiento", "investigación", "imputado", "condena",
    "fracaso", "reclamo", "reclamó",
]


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sentiment_score(text):
    text_low = text.lower()
    pos = sum(text_low.count(w) for w in POSITIVE_WORDS)
    neg = sum(text_low.count(w) for w in NEGATIVE_WORDS)
    score = pos - neg
    if score > 0:
        label = "positivo"
    elif score < 0:
        label = "negativo"
    else:
        label = "neutral"
    return score, label


def build_national_query_url():
    sites = " OR ".join(f"site:{s}" for s in NATIONAL_SITES)
    return google_news_url(f"política Argentina ({sites})")


def build_provincial_query_url():
    sites = " OR ".join(f"site:{s}" for s in SOURCE_SITES)
    places = " OR ".join(f'"{d}"' for d in DEPARTMENTS)
    return google_news_url(f"Salta ({sites} OR {places})")


def collect_general_news():
    """Noticias generales de Nacional y Provincia, sin atarlas a un político
    puntual — para poder ver rápido 'qué pasó' en una ventana de horas,
    con la fuente de cada una a la vista."""
    existing = load_json(NEWS_GENERAL_FILE, [])
    existing_ids = {n["id"] for n in existing}
    new_count = 0

    queries = [
        ("Nacional", build_national_query_url()),
        ("Provincial", build_provincial_query_url()),
    ]
    for category, url in queries:
        feed = fetch_feed(url)
        if not feed:
            continue
        for entry in feed.entries[:MAX_GENERAL_ITEMS_PER_FEED]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            mid = make_id(f"{category}|{link}")
            if mid in existing_ids or not link:
                continue
            score, label = sentiment_score(f"{title} {summary}")
            source = entry_source_name(entry, "Google News")
            published = entry.get("published", datetime.now(timezone.utc).isoformat())
            existing.append({
                "id": mid,
                "category": category,
                "title": title,
                "quote": extract_quote(title),
                "link": link,
                "source": source,
                "published": published,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "sentiment_label": label,
            })
            existing_ids.add(mid)
            new_count += 1

    existing.sort(key=lambda n: n.get("collected_at", ""), reverse=True)
    existing = existing[:MAX_GENERAL_ITEMS_STORED]
    save_json(NEWS_GENERAL_FILE, existing)
    return new_count


def google_news_url(query):
    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=es-419&gl=AR&ceid=AR:es-419"
    )


def build_query_url(politician):
    """Búsqueda de alta precisión: solo en los medios verificados."""
    names = " OR ".join(f'"{a}"' for a in politician["aliases"])
    sites = " OR ".join(f"site:{s}" for s in SOURCE_SITES)
    return google_news_url(f"({names}) ({sites})")


def build_wide_query_url(politician):
    """Búsqueda amplia: sin restringir dominio, agregando el nombre de la
    provincia y de los departamentos para capturar medios locales que no
    tenemos identificados por URL (radios, portales de cada zona, etc.)."""
    names = " OR ".join(f'"{a}"' for a in politician["aliases"])
    places = " OR ".join(f'"{d}"' for d in DEPARTMENTS)
    return google_news_url(f"({names}) ({places})")


def fetch_feed(url):
    try:
        return feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001
        print(f"[aviso] no se pudo leer {url}: {exc}")
        return None


def make_id(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def entry_source_name(entry, fallback):
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return src.title
    return fallback


def match_politicians(text, politicians):
    text_low = text.lower()
    matched = []
    for p in politicians:
        for alias in p["aliases"]:
            if alias.lower() in text_low:
                matched.append(p["id"])
                break
    return matched


LEGAL_KEYWORDS = [
    "denuncia", "denunciado", "denunciada", "imputado", "imputada",
    "procesado", "procesada", "indagatoria", "fiscalía", "causa judicial",
    "juicio", "condena", "condenado", "condenada", "allanamiento",
    "investigación penal", "sobreseído", "sobreseída", "fraude",
    "corrupción", "malversación", "coima",
]


def has_legal_signal(text):
    """Marca si el título/copete usa vocabulario de contexto judicial.
    ¡OJO! Esto NO determina ni afirma que la persona tenga una causa
    real: solo indica que la nota usa esas palabras. Puede ser sobre la
    causa de un tercero, una nota que la menciona sin acusarla, una
    desmentida, etc. Siempre hay que leer la nota original — por eso el
    link está siempre a la vista donde se muestra esto."""
    text_low = text.lower()
    return any(k in text_low for k in LEGAL_KEYWORDS)


def extract_quote(text):
    """Extrae una frase textual citada en el título/copete, si la hay.
    Los medios argentinos suelen citar así: Fulano: "la frase" o
    Fulano dijo que “la frase”. Es una extracción simple por comillas,
    no una atribución verificada: siempre hay que poder ver la nota
    original (por eso guardamos también el link)."""
    match = re.search(r'[“"]([^”"]{10,220})[”"]', text)
    return match.group(1).strip() if match else None


def add_mention(mentions, existing_ids, *, mid, politician, title, link,
                 source, published, sentiment_score_value, sentiment_label):
    if mid in existing_ids or not link:
        return False
    mentions.append({
        "id": mid,
        "politician_id": politician["id"],
        "politician_name": politician["name"],
        "title": title,
        "quote": extract_quote(title),
        "legal_signal": has_legal_signal(title),
        "link": link,
        "source": source,
        "published": published,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "sentiment_score": sentiment_score_value,
        "sentiment_label": sentiment_label,
    })
    existing_ids.add(mid)
    return True


def _collect_from_url(url, p, mentions, existing_ids):
    new_count = 0
    feed = fetch_feed(url)
    if not feed:
        return 0
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        link = entry.get("link", "")
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        # El id incluye al político (no solo el link): así, si una misma
        # noticia menciona a más de una persona, queda un registro por
        # cada una -y extract_comentions() puede detectar el cruce-, en
        # vez de que la segunda persona quede descartada como "duplicado".
        mid = make_id(f"{link}|{p['id']}")
        score, label = sentiment_score(f"{title} {summary}")
        source = entry_source_name(entry, "Google News")
        published = entry.get("published", datetime.now(timezone.utc).isoformat())
        if add_mention(
            mentions, existing_ids, mid=mid, politician=p, title=title,
            link=link, source=source, published=published,
            sentiment_score_value=score, sentiment_label=label,
        ):
            new_count += 1
    return new_count


def collect_by_politician(politicians, mentions, existing_ids):
    """Dos pasadas por político: una de alta precisión (medios verificados)
    y otra amplia (toda la provincia y sus departamentos), para no perder
    cobertura de medios locales que no tenemos mapeados por dominio."""
    new_count = 0
    for p in politicians:
        new_count += _collect_from_url(build_query_url(p), p, mentions, existing_ids)
        new_count += _collect_from_url(build_wide_query_url(p), p, mentions, existing_ids)
    return new_count


def collect_from_direct_feeds(politicians, mentions, existing_ids):
    new_count = 0
    by_id = {p["id"]: p for p in politicians}
    for feed_name, url in DIRECT_FEEDS:
        feed = fetch_feed(url)
        if not feed:
            continue
        for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            full_text = f"{title} {summary}"
            matched_ids = match_politicians(full_text, politicians)
            if not matched_ids:
                continue
            score, label = sentiment_score(full_text)
            published = entry.get("published", datetime.now(timezone.utc).isoformat())
            for pid in matched_ids:
                mid = make_id(f"{link}|{pid}")
                if add_mention(
                    mentions, existing_ids, mid=mid, politician=by_id[pid],
                    title=title, link=link, source=feed_name,
                    published=published, sentiment_score_value=score,
                    sentiment_label=label,
                ):
                    new_count += 1
    return new_count


def extract_trending_terms(mentions, hours=48, top_n=15):
    """Cuenta qué palabras significativas se repiten más en los títulos de
    las últimas `hours` horas. Es una heurística simple (frecuencia de
    palabras, sin stopwords), no un modelo de lenguaje: sirve para detectar
    de qué se está hablando más, no para entender matices."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    counts = defaultdict(int)
    for m in mentions:
        try:
            collected = datetime.fromisoformat(m["collected_at"]).timestamp()
        except (KeyError, ValueError):
            continue
        if collected < cutoff:
            continue
        words = re.findall(r"[a-záéíóúñü]{4,}", m["title"].lower())
        seen_in_title = set()
        for w in words:
            if w in STOPWORDS or w in seen_in_title:
                continue
            seen_in_title.add(w)
            counts[w] += 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [{"term": t, "count": c} for t, c in top if c > 1]


def extract_comentions(mentions, politicians):
    """Detecta qué políticos aparecen mencionados en la misma noticia
    (mismo link), como señal simple de qué figuras se asocian entre sí
    en la cobertura mediática."""
    by_id = {p["id"]: p["name"] for p in politicians}
    politicians_by_link = defaultdict(set)
    for m in mentions:
        politicians_by_link[m["link"]].add(m["politician_id"])

    pair_counts = defaultdict(int)
    for pids in politicians_by_link.values():
        if len(pids) < 2:
            continue
        for a, b in combinations(sorted(pids), 2):
            pair_counts[(a, b)] += 1

    pairs = sorted(pair_counts.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return [
        {"a": by_id.get(a, a), "b": by_id.get(b, b), "count": c}
        for (a, b), c in pairs
    ]


def build_topic_query_url(topic):
    """Igual criterio que para políticos: una búsqueda por tema, restringida
    a Salta (o a medios nacionales si el tema tiene scope: "nacional")."""
    names = " OR ".join(f'"{k}"' for k in topic["keywords"])
    if topic.get("scope") == "nacional":
        sites = " OR ".join(f"site:{s}" for s in NATIONAL_SITES)
    else:
        places = " OR ".join(f'"{d}"' for d in DEPARTMENTS)
        site_list = " OR ".join(f"site:{s}" for s in SOURCE_SITES)
        sites = f"{site_list} OR {places}"
    return google_news_url(f"({names}) ({sites})")


def collect_by_topic(topics, topic_mentions, existing_ids, politicians):
    new_count = 0
    for t in topics:
        feed = fetch_feed(build_topic_query_url(t))
        if not feed:
            continue
        for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            mid = make_id(f"{link}|tema|{t['id']}")
            if mid in existing_ids or not link:
                continue
            score, label = sentiment_score(f"{title} {summary}")
            source = entry_source_name(entry, "Google News")
            published = entry.get("published", datetime.now(timezone.utc).isoformat())
            topic_mentions.append({
                "id": mid,
                "topic_id": t["id"],
                "topic_name": t["name"],
                "title": title,
                "quote": extract_quote(title),
                "link": link,
                "source": source,
                "published": published,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "sentiment_score": score,
                "sentiment_label": label,
                # Qué políticos, si los hay, están mencionados en esta nota
                # sobre el tema — así se puede ver quién se asocia a qué.
                "politicians_mentioned": match_politicians(f"{title} {summary}", politicians),
            })
            existing_ids.add(mid)
            new_count += 1
    return new_count


def build_topics_data(topic_mentions, topics, politicians):
    by_id_name = {p["id"]: p["name"] for p in politicians}
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "topics": {}}

    for t in topics:
        own = [m for m in topic_mentions if m["topic_id"] == t["id"]]
        own_sorted = sorted(own, key=lambda m: m.get("collected_at", ""), reverse=True)

        total = len(own)
        pos = sum(1 for m in own if m["sentiment_label"] == "positivo")
        neg = sum(1 for m in own if m["sentiment_label"] == "negativo")
        neu = total - pos - neg

        by_day = defaultdict(lambda: {"total": 0, "score_sum": 0})
        for m in own:
            day = (m.get("published") or m["collected_at"])[:10]
            by_day[day]["total"] += 1
            by_day[day]["score_sum"] += m["sentiment_score"]

        politician_counts = defaultdict(int)
        for m in own:
            for pid in m.get("politicians_mentioned", []):
                politician_counts[pid] += 1
        involved = sorted(politician_counts.items(), key=lambda kv: kv[1], reverse=True)
        involved = [{"name": by_id_name.get(pid, pid), "count": c} for pid, c in involved]

        quotes = [
            {"quote": m["quote"], "source": m["source"], "link": m["link"],
             "date": m.get("published") or m["collected_at"]}
            for m in own_sorted if m.get("quote")
        ][:15]

        result["topics"][t["id"]] = {
            "id": t["id"], "name": t["name"], "role": t.get("role", ""),
            "total_mentions": total,
            "sentiment_breakdown": {"positivo": pos, "negativo": neg, "neutral": neu},
            "sentiment_by_day": by_day,
            "top_topics": extract_trending_terms(own, hours=24 * 30, top_n=8),
            "politicians_involved": involved,
            "quotes": quotes,
            "recent_articles": [
                {"title": m["title"], "link": m["link"], "source": m["source"],
                 "date": m.get("published") or m["collected_at"],
                 "sentiment_label": m["sentiment_label"]}
                for m in own_sorted[:30]
            ],
        }
    return result


def politician_trending_terms(mentions, politician_id, hours=168, top_n=8):
    """Igual que extract_trending_terms pero acotado a un solo político,
    para mostrar de qué temas se habla específicamente sobre esa persona."""
    own = [m for m in mentions if m["politician_id"] == politician_id]
    return extract_trending_terms(own, hours=hours, top_n=top_n)


def build_dossiers(mentions, politicians):
    """Arma una ficha pública por político: historial, tono, temas propios,
    declaraciones citadas (extraídas de títulos) y con quién aparece
    mencionado. Todo a partir de datos ya recolectados de medios públicos."""
    dossiers = {}
    for p in politicians:
        own = [m for m in mentions if m["politician_id"] == p["id"]]
        own_sorted = sorted(own, key=lambda m: m.get("collected_at", ""), reverse=True)

        total = len(own)
        pos = sum(1 for m in own if m["sentiment_label"] == "positivo")
        neg = sum(1 for m in own if m["sentiment_label"] == "negativo")
        neu = total - pos - neg

        by_day = defaultdict(lambda: {"total": 0, "score_sum": 0})
        for m in own:
            day = (m.get("published") or m["collected_at"])[:10]
            by_day[day]["total"] += 1
            by_day[day]["score_sum"] += m["sentiment_score"]

        quotes = [
            {
                "quote": m["quote"],
                "source": m["source"],
                "link": m["link"],
                "date": m.get("published") or m["collected_at"],
            }
            for m in own_sorted if m.get("quote")
        ][:15]

        legal_mentions = [
            {
                "title": m["title"],
                "source": m["source"],
                "link": m["link"],
                "date": m.get("published") or m["collected_at"],
            }
            for m in own_sorted if m.get("legal_signal")
        ][:20]

        dossiers[p["id"]] = {
            "id": p["id"],
            "name": p["name"],
            "role": p.get("role", ""),
            "total_mentions": total,
            "sentiment_breakdown": {"positivo": pos, "negativo": neg, "neutral": neu},
            "sentiment_by_day": by_day,
            "top_topics": politician_trending_terms(mentions, p["id"]),
            "quotes": quotes,
            "legal_mentions": legal_mentions,
            "recent_articles": [
                {
                    "title": m["title"], "link": m["link"], "source": m["source"],
                    "date": m.get("published") or m["collected_at"],
                    "sentiment_label": m["sentiment_label"],
                }
                for m in own_sorted[:30]
            ],
        }
    return dossiers


def _significant_words(title):
    return {
        w for w in re.findall(r"[a-záéíóúñü]{5,}", title.lower())
        if w not in STOPWORDS
    }


def build_events(mentions, topic_mentions, general_news, politicians, topics,
                  hours=72, min_shared_words=2, max_items=400):
    """El 'cerebro': agrupa noticias de distintas fuentes/tipos que
    probablemente hablan del mismo hecho (comparten al menos
    `min_shared_words` palabras significativas en el título y ocurrieron
    dentro de la misma ventana de horas), y arma un 'evento' que muestra
    qué políticos y temas están conectados a él. Es agrupamiento por
    texto, no comprensión real del contenido: dos notas no relacionadas
    que casualmente comparten palabras pueden agruparse por error —
    revisá siempre los artículos originales de cada evento."""
    items = []
    for m in mentions:
        items.append({
            "title": m["title"], "link": m["link"], "source": m["source"],
            "date": m.get("published") or m["collected_at"], "collected_at": m["collected_at"],
            "politician_ids": {m["politician_id"]}, "topic_ids": set(),
        })
    for m in topic_mentions:
        items.append({
            "title": m["title"], "link": m["link"], "source": m["source"],
            "date": m.get("published") or m["collected_at"], "collected_at": m["collected_at"],
            "politician_ids": set(m.get("politicians_mentioned", [])), "topic_ids": {m["topic_id"]},
        })
    for n in general_news:
        items.append({
            "title": n["title"], "link": n["link"], "source": n["source"],
            "date": n.get("published") or n["collected_at"], "collected_at": n["collected_at"],
            "politician_ids": set(), "topic_ids": set(),
        })

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    recent = []
    for it in items:
        try:
            t = datetime.fromisoformat(it["collected_at"]).timestamp()
        except (ValueError, KeyError):
            continue
        if t >= cutoff:
            it["_words"] = _significant_words(it["title"])
            recent.append(it)
    recent = recent[:max_items]

    parent = list(range(len(recent)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(recent)):
        for j in range(i + 1, len(recent)):
            if len(recent[i]["_words"] & recent[j]["_words"]) >= min_shared_words:
                union(i, j)

    clusters = defaultdict(list)
    for i in range(len(recent)):
        clusters[find(i)].append(recent[i])

    pol_names = {p["id"]: p["name"] for p in politicians}
    topic_names = {t["id"]: t["name"] for t in topics}

    events = []
    for cluster in clusters.values():
        seen_links, unique_items = set(), []
        for it in cluster:
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
            unique_items.append(it)
        if len(unique_items) < 2:
            continue

        pol_ids, topic_ids = set(), set()
        for it in unique_items:
            pol_ids |= it["politician_ids"]
            topic_ids |= it["topic_ids"]
        unique_items.sort(key=lambda x: x["collected_at"], reverse=True)

        events.append({
            "id": make_id("evento|" + unique_items[0]["link"]),
            "title": unique_items[0]["title"],
            "items_count": len(unique_items),
            "politicians": sorted(pol_names.get(pid, pid) for pid in pol_ids),
            "topics": sorted(topic_names.get(tid, tid) for tid in topic_ids),
            "sources": sorted({it["source"] for it in unique_items}),
            "first_seen": min(it["collected_at"] for it in unique_items),
            "last_seen": max(it["collected_at"] for it in unique_items),
            "items": [
                {"title": it["title"], "link": it["link"], "source": it["source"], "date": it["date"]}
                for it in unique_items[:15]
            ],
        })

    events.sort(key=lambda e: e["items_count"], reverse=True)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "events": events[:50]}


def build_aggregates(mentions, politicians):
    by_politician = defaultdict(lambda: {
        "total": 0, "positivo": 0, "negativo": 0, "neutral": 0, "score_sum": 0,
    })
    by_day = defaultdict(lambda: defaultdict(lambda: {"total": 0, "score_sum": 0}))

    for m in mentions:
        pid = m["politician_id"]
        by_politician[pid]["total"] += 1
        by_politician[pid][m["sentiment_label"]] += 1
        by_politician[pid]["score_sum"] += m["sentiment_score"]

        day = (m.get("published") or m["collected_at"])[:10]
        by_day[day][pid]["total"] += 1
        by_day[day][pid]["score_sum"] += m["sentiment_score"]

    aggregates = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_politician": by_politician,
        "by_day": by_day,
        "politicians": politicians,
        "trending_terms": extract_trending_terms(mentions),
        "comentions": extract_comentions(mentions, politicians),
    }
    save_json(AGGREGATES_FILE, aggregates)


def main():
    politicians = load_json(POLITICIANS_FILE, [])
    topics = load_json(TOPICS_FILE, [])
    if not politicians and not topics:
        print("No hay políticos ni temas configurados.")
        return

    if politicians:
        mentions = load_json(MENTIONS_FILE, [])
        existing_ids = {m["id"] for m in mentions}

        new_by_search = collect_by_politician(politicians, mentions, existing_ids)
        new_by_feeds = collect_from_direct_feeds(politicians, mentions, existing_ids)

        mentions.sort(key=lambda m: m.get("collected_at", ""), reverse=True)
        mentions = mentions[:MAX_MENTIONS_STORED]

        save_json(MENTIONS_FILE, mentions)
        build_aggregates(mentions, politicians)
        save_json(DOSSIERS_FILE, build_dossiers(mentions, politicians))

        total_new = new_by_search + new_by_feeds
        print(f"Menciones nuevas: {total_new} (búsqueda: {new_by_search}, feeds directos: {new_by_feeds})")
        print(f"Total menciones almacenadas: {len(mentions)}")
    else:
        mentions = []

    if topics:
        topic_mentions = load_json(TOPIC_MENTIONS_FILE, [])
        existing_topic_ids = {m["id"] for m in topic_mentions}
        new_topic_count = collect_by_topic(topics, topic_mentions, existing_topic_ids, politicians)

        topic_mentions.sort(key=lambda m: m.get("collected_at", ""), reverse=True)
        topic_mentions = topic_mentions[:MAX_MENTIONS_STORED]

        save_json(TOPIC_MENTIONS_FILE, topic_mentions)
        save_json(TOPIC_DATA_FILE, build_topics_data(topic_mentions, topics, politicians))
        print(f"Menciones de temas nuevas: {new_topic_count}")

    new_general = collect_general_news()
    general_news = load_json(NEWS_GENERAL_FILE, [])
    print(f"Noticias generales nuevas (nacional + provincial): {new_general}")

    topic_mentions_for_events = load_json(TOPIC_MENTIONS_FILE, []) if topics else []
    events_data = build_events(mentions, topic_mentions_for_events, general_news, politicians, topics)
    save_json(EVENTS_FILE, events_data)
    print(f"Eventos detectados: {len(events_data['events'])}")


if __name__ == "__main__":
    main()
