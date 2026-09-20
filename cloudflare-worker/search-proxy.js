/**
 * Proxy mínimo para el "buscador libre" del panel.
 *
 * Por qué existe: los navegadores no dejan que una página web (tu panel en
 * GitHub Pages) le pida datos directamente a Google News, por una
 * protección del navegador llamada CORS. Este Worker corre en la nube de
 * Cloudflare (gratis), le pide los datos a Google News él mismo, y se los
 * pasa a tu panel. Es la pieza que falta para que "buscar cualquier
 * palabra en vivo" funcione desde el navegador.
 *
 * Cómo desplegarlo (gratis, sin tarjeta):
 * 1. Creá una cuenta en https://workers.cloudflare.com
 * 2. Dashboard → Workers & Pages → Create → "Create Worker"
 * 3. Reemplazá el código de ejemplo por el de este archivo completo
 * 4. Cambiá ALLOWED_ORIGIN de abajo por la URL real de tu GitHub Pages
 *    (por ejemplo "https://tu-usuario.github.io")
 * 5. Deploy. Te va a dar una URL tipo https://algo.tu-cuenta.workers.dev
 * 6. Pegá esa URL en index.html, en la constante SEARCH_PROXY_URL
 */

const ALLOWED_ORIGIN = "https://TU-USUARIO.github.io"; // <-- cambiá esto

export default {
  async fetch(request) {
    const corsHeaders = {
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);
    const q = (url.searchParams.get("q") || "").trim();

    if (!q || q.length < 2 || q.length > 100) {
      return new Response(
        JSON.stringify({ error: "Parámetro 'q' inválido (2 a 100 caracteres)." }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // Restringido a Argentina / español para que los resultados sean útiles
    // para este proyecto. Podés sacar "gl=AR" si querés cobertura mundial.
    const target =
      "https://news.google.com/rss/search?q=" +
      encodeURIComponent(q) +
      "&hl=es-419&gl=AR&ceid=AR:es-419";

    const upstream = await fetch(target, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; RadarPoliticoSalta/1.0)" },
    });
    const body = await upstream.text();

    return new Response(body, {
      headers: { ...corsHeaders, "Content-Type": "application/xml; charset=utf-8" },
    });
  },
};
