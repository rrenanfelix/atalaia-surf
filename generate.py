#!/usr/bin/env python3
"""
Atalaia Surf Report — gerador automático (v2).

Busca dados frescos do Open-Meteo (Wavewatch III + GFS) + marés do
Tabuademares para a Praia do Atalaia (Itajaí / SC), calcula ranking
discriminativo dos próximos 3 dias para surfista intermediário,
e regrava o index.html.

Roda diariamente via GitHub Actions às 6h BRT.
"""

import json
import re
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LAT = -26.92
LON = -48.64
TZ = "America/Sao_Paulo"
BR = ZoneInfo(TZ)
HOURS_FULL = list(range(0, 24))
HOURS_KEY = [6, 9, 12, 15, 18]
SLOT_WEIGHTS = {6: 2.0, 9: 2.5, 12: 2.0, 15: 1.3, 18: 1.0}  # manhã pesa mais
TOTAL_WEIGHT = sum(SLOT_WEIGHTS.values())  # 8.8


def fetch(url: str, timeout=30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "atalaia-surf-bot/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))


# ──────────────────────── Helpers ────────────────────────

def deg_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) % 360 / 22.5)]


def wind_kind(deg: float) -> str:
    """Atalaia faz fundo pra E — vento de W/NW/SW é terral, E/NE/SE é maral."""
    d = deg % 360
    if 200 <= d <= 340:
        return "terral"
    if 50 <= d <= 170:
        return "maral"
    return "cruzado"


# ──────────────────────── Scoring discriminativo ────────────────────────

def score_wave(h: float) -> float:
    """Onda intermediário: ideal 0.8–1.5m. Retorna 0–2."""
    if h < 0.3:
        return 0.0
    if h < 0.5:
        return 0.5
    if h < 0.7:
        return 1.0
    if h < 0.9:
        return 1.6
    if h < 1.3:
        return 2.0
    if h < 1.8:
        return 1.7
    return 1.2  # grande demais


def score_period(p: float) -> float:
    """Período mais longo = mais power. Retorna 0–1.5."""
    if p < 5:
        return 0.0
    if p < 6:
        return 0.4
    if p < 7:
        return 0.7
    if p < 8:
        return 1.0
    if p < 10:
        return 1.3
    return 1.5


def score_wind(speed_kt: float, dir_deg: float) -> float:
    """Vento: terral leve = +2; maral forte = −2. Retorna −2 a +2."""
    k = wind_kind(dir_deg)
    s = speed_kt
    if s < 3:
        return 1.5  # glassy independente da direção
    if k == "terral":
        if s < 8:
            return 2.0
        if s < 14:
            return 1.4
        if s < 20:
            return 0.5
        return -0.5  # terral forte demais
    if k == "cruzado":
        if s < 8:
            return 0.5
        if s < 14:
            return -0.3
        return -0.8
    # maral
    if s < 5:
        return -0.3
    if s < 10:
        return -1.2
    return -2.0


def score_swell_direction(deg: float) -> float:
    """Atalaia ideal SE (135°). Retorna 0–1."""
    diff = min(abs(deg - 135), 360 - abs(deg - 135))
    if diff < 25:
        return 1.0
    if diff < 50:
        return 0.6
    if diff < 80:
        return 0.3
    return 0.0


def score_hour(h: dict) -> float:
    """Score 0–~6.5 de uma hora-chave."""
    return (
        score_wave(h["wh"])
        + score_period(h["wp"])
        + score_wind(h["wspd"], h["windir"])
        + score_swell_direction(h["wd"])
    )


def score_day(hours: dict, rain_mm: float) -> tuple[float, dict]:
    """Score 0–10 do dia, mais info da melhor janela."""
    total = 0.0
    hour_scores = {}
    for hr in HOURS_KEY:
        s = score_hour(hours[hr])
        hour_scores[hr] = s
        total += s * SLOT_WEIGHTS[hr]
    # max possível: ~6.5 * 8.8 = 57
    raw = total / 57 * 10
    # penaliza chuva forte
    if rain_mm > 5:
        raw -= 1.5
    elif rain_mm > 2:
        raw -= 0.6
    raw = max(0.0, min(10.0, raw))

    # Best window (consecutive 2-3h with highest avg)
    best_hr = max(hour_scores, key=lambda k: hour_scores[k])
    return round(raw, 1), {"hour_scores": hour_scores, "best_hr": best_hr}


# ──────────────────────── Marés ────────────────────────

def fetch_tides() -> dict:
    """Scraping do tabuademares.com (HTML real usa tabelas <td>, não tabs).

    Retorna {date_iso: [{time, height, type}]}.
    """
    try:
        html = fetch("https://tabuademares.com/br/santa-catarina/itajai/previsao/mares", timeout=20)
    except Exception as e:
        print(f"⚠️  fetch_tides falhou: {e}")
        return {}

    months = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
              "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}
    year = datetime.now(BR).year

    # Cada dia: marker "DD MES" precede uma tabela de marés
    # Marker no HTML aparece como text "27 MAI" perto do bloco da tabela
    date_marker_re = re.compile(
        r"(\d{1,2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\b"
    )
    # Pattern de uma linha de maré dentro de <tr>: ícone (bajamar/pleamar), hora, altura, coef
    tide_row_re = re.compile(
        r'icon-ficha_(bajamar|pleamar)[^<]*</span></td>\s*'
        r'<td>(\d{1,2}:\d{2})</td>\s*'
        r'<td><span class="tabla_mareas_marea_altura_numero">(\d+,\d+)</span>',
        re.DOTALL,
    )

    # Encontrar todos os date markers e suas posições
    markers = list(date_marker_re.finditer(html))
    out: dict = {}
    for i, m in enumerate(markers):
        try:
            day = int(m.group(1))
            month = months[m.group(2)]
        except (KeyError, ValueError):
            continue
        date_iso = f"{year}-{month:02d}-{day:02d}"
        # Pegar o trecho HTML entre esse marker e o próximo
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else min(start + 6000, len(html))
        chunk = html[start:end]
        tides = []
        for tr in tide_row_re.finditer(chunk):
            kind, time_s, height_s = tr.group(1), tr.group(2), tr.group(3)
            height = float(height_s.replace(",", "."))
            tides.append({
                "time": time_s,
                "height": height,
                "type": "alta" if kind == "pleamar" else "baixa",
            })
        if tides:
            out[date_iso] = tides
    return out


# ──────────────────────── Data fetch ────────────────────────

def fetch_data():
    today = datetime.now(BR).date()
    start = today.isoformat()
    end = (today + timedelta(days=2)).isoformat()

    marine = fetch_json(
        f"https://marine-api.open-meteo.com/v1/marine?latitude={LAT}&longitude={LON}"
        f"&hourly=wave_height,wave_direction,wave_period,swell_wave_height,"
        f"swell_wave_direction,swell_wave_period,sea_surface_temperature"
        f"&start_date={start}&end_date={end}&timezone={TZ}"
    )
    weather = fetch_json(
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m,precipitation,cloud_cover,wind_speed_10m,"
        f"wind_direction_10m,wind_gusts_10m"
        f"&daily=sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&start_date={start}&end_date={end}&timezone={TZ}&wind_speed_unit=kn"
    )
    tides = fetch_tides()

    by_day: dict[str, dict] = {}
    mh, wh = marine["hourly"], weather["hourly"]
    for i, t in enumerate(mh["time"]):
        date_s, hour = t[:10], int(t[11:13])
        if hour not in HOURS_KEY:
            continue
        by_day.setdefault(date_s, {})[hour] = {
            "wh": mh["wave_height"][i],
            "wd": mh["wave_direction"][i],
            "wp": mh["wave_period"][i],
            "sst": mh["sea_surface_temperature"][i],
        }
    for i, t in enumerate(wh["time"]):
        date_s, hour = t[:10], int(t[11:13])
        if hour not in HOURS_KEY or date_s not in by_day or hour not in by_day[date_s]:
            continue
        by_day[date_s][hour].update({
            "temp": wh["temperature_2m"][i],
            "wspd": wh["wind_speed_10m"][i],
            "windir": wh["wind_direction_10m"][i],
            "wgust": wh["wind_gusts_10m"][i],
        })

    weekday = {0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta",
               4: "Sexta", 5: "Sábado", 6: "Domingo"}
    months_pt = ["jan", "fev", "mar", "abr", "mai", "jun",
                 "jul", "ago", "set", "out", "nov", "dez"]

    days_meta = []
    for i, date_s in enumerate(sorted(by_day.keys())):
        d = datetime.strptime(date_s, "%Y-%m-%d").date()
        hours = by_day[date_s]
        rain = weather["daily"]["precipitation_sum"][i]
        score, info = score_day(hours, rain)
        whs = [hours[h]["wh"] for h in HOURS_KEY if h in hours]
        wps = [hours[h]["wp"] for h in HOURS_KEY if h in hours]
        ssts = [hours[h]["sst"] for h in HOURS_KEY if h in hours]
        days_meta.append({
            "date_s": date_s,
            "weekday": weekday[d.weekday()],
            "short": f"{d.day:02d} {months_pt[d.month-1]}",
            "tmax": weather["daily"]["temperature_2m_max"][i],
            "tmin": weather["daily"]["temperature_2m_min"][i],
            "rain": rain,
            "sunrise": weather["daily"]["sunrise"][i][11:16],
            "sunset": weather["daily"]["sunset"][i][11:16],
            "wh_min": round(min(whs), 2),
            "wh_max": round(max(whs), 2),
            "wp_avg": round(sum(wps) / len(wps), 1),
            "sst_avg": round(sum(ssts) / len(ssts), 1),
            "score": score,
            "best_hr": info["best_hr"],
            "hour_scores": info["hour_scores"],
            "tides": tides.get(date_s, []),
        })

    return by_day, days_meta


# ──────────────────────── Window finder + tips ────────────────────────

def find_window(meta: dict, hours: dict) -> dict:
    """Janela ideal com descrição rica como um analista faria."""
    scores = meta["hour_scores"]
    best = meta["best_hr"]
    # expandir vizinhos
    sorted_hrs = sorted(HOURS_KEY)
    idx = sorted_hrs.index(best)
    candidates = [best]
    if idx > 0 and scores[sorted_hrs[idx - 1]] > scores[sorted_hrs[min(idx + 1, len(sorted_hrs) - 1)]]:
        candidates.insert(0, sorted_hrs[idx - 1])
    elif idx < len(sorted_hrs) - 1:
        candidates.append(sorted_hrs[idx + 1])
    start_hr = min(candidates)
    end_hr = max(candidates) + 1
    window_str = f"{start_hr}h – {end_hr}h"

    # Dados da melhor hora
    d = hours[best]
    wh = d["wh"]
    wp = d["wp"]
    wdir = deg_to_compass(d["wd"])
    wspd = d["wspd"]
    windir_compass = deg_to_compass(d["windir"])
    wk = wind_kind(d["windir"])

    # Montar descrição do vento
    if wk == "terral":
        vento_txt = f"vento terral {windir_compass} ({wspd:.0f} kt) — parede limpa"
    elif wspd < 4:
        vento_txt = f"vento fraco ({wspd:.0f} kt) — mar glassy"
    elif wk == "maral":
        vento_txt = f"vento maral {windir_compass} ({wspd:.0f} kt) — mar choposo"
    else:
        vento_txt = f"vento cruzado {windir_compass} ({wspd:.0f} kt)"

    # Montar descrição da onda
    if wh >= 1.0:
        onda_txt = f"onda {wh:.1f}m {wdir} com potência, período {wp:.0f}s"
    elif wh >= 0.6:
        onda_txt = f"onda {wh:.1f}m {wdir}, período {wp:.0f}s — surfável pra intermediário"
    else:
        onda_txt = f"onda pequena ({wh:.1f}m {wdir}, {wp:.0f}s) — melhor com prancha volumosa"

    # Descrição da maré (se disponível)
    mare_txt = ""
    tides = meta.get("tides", [])
    if tides:
        # Achar maré mais próxima da janela ideal pra contextualizar
        for t in tides:
            hr_int = int(t["time"].split(":")[0])
            if abs(hr_int - best) <= 3:
                if t["type"] == "alta":
                    mare_txt = f", maré alta às {t['time']} ({t['height']:.1f}m)"
                else:
                    mare_txt = f", maré baixa às {t['time']} ({t['height']:.1f}m)"
                break
        if not mare_txt:
            # Indicar se tá enchendo ou vazando
            baixas = [t for t in tides if t["type"] == "baixa"]
            altas = [t for t in tides if t["type"] == "alta"]
            if altas and baixas:
                prim_alta = int(altas[0]["time"].split(":")[0])
                prim_baixa = int(baixas[0]["time"].split(":")[0])
                if prim_baixa < best < prim_alta:
                    mare_txt = f", maré enchendo pra preia {altas[0]['time']}"
                elif prim_alta < best:
                    mare_txt = ", maré vazando"

    # Dica contextual
    if meta["score"] >= 6:
        dica = "Melhor dia — prioriza essa sessão."
    elif meta["score"] >= 4:
        if wk == "terral":
            dica = "Dia razoável com vento favorável."
        else:
            dica = "Onda ok mas vento não ajuda muito."
    elif wh < 0.5:
        dica = "Flat day — só se for muito fã. Leva longboard ou SUP."
    else:
        dica = "Condições fracas — se entrar, mate cedo quando o vento tiver mais calmo."

    return {
        "range": window_str,
        "description": f"<strong>{window_str}</strong> · {vento_txt}{mare_txt}. {onda_txt}.",
        "tip": dica,
        "is_bad": meta["score"] < 3.5,
    }


def gear_tip(sst: float) -> tuple[str, str]:
    """Retorna (roupa, parafina)."""
    if sst >= 24:
        return "lycra ou bermuda climber", "tropical (24°C+)"
    if sst >= 22:
        return "top 2 mm + bermuda OU long john 2/2", "warm (21-25°C)"
    if sst >= 19:
        return "long john 3/2 mm", "cool (18-23°C)"
    if sst >= 16:
        return "full suit 3/2 mm", "cold (14-19°C)"
    return "full suit 4/3 mm + capuz", "cold (14-19°C)"


def day_class(score: float) -> str:
    if score >= 5.5:
        return "good"
    if score >= 3.5:
        return "mid"
    return "bad"


def cell_class_wave(h: float) -> str:
    if h >= 0.7:
        return "cell-good"
    if h >= 0.5:
        return "cell-mid"
    return "cell-bad"


def cell_class_period(p: float) -> str:
    if p >= 8:
        return "cell-good"
    if p >= 6:
        return "cell-mid"
    return "cell-bad"


def cell_class_wind(ws: float, wdir: float) -> str:
    k = wind_kind(wdir)
    if (k == "terral" and ws < 12) or ws < 4:
        return "cell-good"
    if k == "maral" and ws > 6:
        return "cell-bad"
    return "cell-mid"


# ──────────────────────── Render ────────────────────────

def render_tides(tides: list[dict]) -> str:
    if not tides:
        return '<div class="tides"><div class="muted">Marés indisponíveis — consulte <a href="https://tabuademares.com/br/santa-catarina/itajai" target="_blank">tabuademares</a></div></div>'
    pills = []
    for t in tides:
        cls = "alta" if t.get("type") == "alta" else "baixa"
        arrow = "▲" if cls == "alta" else "▼"
        pills.append(
            f'<div class="tide-pill {cls}">{arrow} <span class="tide-time">{t["time"]}</span> {str(t["height"]).replace(".", ",")} m</div>'
        )
    return f'<div class="tides"><h3>Marés</h3><div class="tide-row">{"".join(pills)}</div></div>'


def render_day_card(meta: dict, hours: dict) -> str:
    cls = day_class(meta["score"])
    rows = []
    for label_key, fmt, cls_fn in [
        ("wh", lambda v: f"{v:.2f}".replace(".", ","), lambda d: cell_class_wave(d["wh"])),
        ("wp", lambda v: f"{v:.1f}".replace(".", ","), lambda d: cell_class_period(d["wp"])),
        ("wspd", lambda v: f"{v:.1f}".replace(".", ","), lambda d: cell_class_wind(d["wspd"], d["windir"])),
        ("windir", lambda v: deg_to_compass(v), lambda d: cell_class_wind(d["wspd"], d["windir"])),
    ]:
        cells = []
        for hr in HOURS_KEY:
            d = hours[hr]
            cells.append(f'<td class="{cls_fn(d)}">{fmt(d[label_key])}</td>')
        rows.append("".join(cells))

    window = find_window(meta, hours)
    # Estrela pro melhor dia da lista (será adicionada externamente)
    return f"""
    <div class="day-card">
      <div class="day-head {cls}">
        <div>
          <div class="day-title">{meta['weekday']} · {meta['short']}</div>
          <div class="day-sub">☀️ {meta['tmin']:.0f}° – {meta['tmax']:.0f}°C · 💧 {meta['rain']:.1f}mm · ☀ {meta['sunrise']}–{meta['sunset']}</div>
        </div>
        <div class="day-score {cls}">{meta['score']}</div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-label">Onda</div><div class="stat-value">{str(meta['wh_min']).replace('.', ',')}–{str(meta['wh_max']).replace('.', ',')} m</div></div>
        <div class="stat"><div class="stat-label">Período</div><div class="stat-value">{str(meta['wp_avg']).replace('.', ',')} s</div></div>
        <div class="stat"><div class="stat-label">SST</div><div class="stat-value">{str(meta['sst_avg']).replace('.', ',')} °C</div></div>
        <div class="stat"><div class="stat-label">Score</div><div class="stat-value">{meta['score']}/10</div></div>
      </div>
      <div class="hourly">
        <h3>Por hora</h3>
        <table class="hr-tbl">
          <thead><tr><th class="lbl"></th><th>6h</th><th>9h</th><th>12h</th><th>15h</th><th>18h</th></tr></thead>
          <tbody>
            <tr><td class="lbl">Onda (m)</td>{rows[0]}</tr>
            <tr><td class="lbl">Período (s)</td>{rows[1]}</tr>
            <tr><td class="lbl">Vento (kt)</td>{rows[2]}</tr>
            <tr><td class="lbl">Dir. vento</td>{rows[3]}</tr>
          </tbody>
        </table>
      </div>
      {render_tides(meta['tides'])}
      <div class="window{'  bad' if window['is_bad'] else ''}">
        <div class="win-label">{'⚠️ Dia fraco' if window['is_bad'] else '🎯 Janela ideal'}</div>
        <div class="win-text">{window['description']}<br><em>{window['tip']}</em></div>
      </div>
    </div>
    """


def render_ranking(days_meta: list) -> str:
    ranked = sorted(days_meta, key=lambda d: d["score"], reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    colors = ["good", "mid", "bad"]
    rows = []
    for i, d in enumerate(ranked):
        rows.append(f"""
        <div class="rank-row">
          <div class="medal">{medals[i]}</div>
          <div>
            <div class="rank-name">{d['weekday']}, {d['short']}</div>
            <div class="rank-reason">Onda {str(d['wh_min']).replace('.', ',')}–{str(d['wh_max']).replace('.', ',')}m · período {str(d['wp_avg']).replace('.', ',')}s · janela em torno de {d['best_hr']}h</div>
          </div>
          <div class="score" style="color:var(--{colors[i]})">{d['score']}</div>
        </div>""")
    return "".join(rows)


def render_gear(sst: float) -> str:
    roupa, parafina = gear_tip(sst)
    return f"""
  <div class="gear-card">
    <h2>🧥 Equipamento sugerido</h2>
    <p class="muted">Temp. água ~{sst:.1f}°C — base pras recomendações.</p>
    <div class="gear-grid">
      <div class="gear-item"><div class="gear-icon">🌡️</div><div class="gear-title">Roupa</div><div class="gear-desc">{roupa}</div></div>
      <div class="gear-item"><div class="gear-icon">🏄</div><div class="gear-title">Prancha</div><div class="gear-desc">Ondas pequenas-médias e período curto pedem fish, mid-length ou longboard. Shortboard só quando passar 0,9 m com período &gt;= 8 s.</div></div>
      <div class="gear-item"><div class="gear-icon">🕯️</div><div class="gear-title">Parafina</div><div class="gear-desc">{parafina}</div></div>
      <div class="gear-item"><div class="gear-icon">⚠️</div><div class="gear-title">Atenção</div><div class="gear-desc">Corrente do canal do Itajaí-Açu é forte na vazante. Cuidado na maré descendo, especialmente coef &gt; 80.</div></div>
    </div>
  </div>
    """


def render_html(by_day: dict, days_meta: list) -> str:
    now_str = datetime.now(BR).strftime("%d/%m/%Y · %H:%M")
    sst_avg = sum(d["sst_avg"] for d in days_meta) / len(days_meta)

    day_cards = "\n".join(render_day_card(m, by_day[m["date_s"]]) for m in days_meta)
    ranking = render_ranking(days_meta)

    labels, wave_arr, wind_arr = [], [], []
    for m in days_meta:
        for hr in HOURS_KEY:
            labels.append(f"{m['weekday'][:3]} {hr}h")
            wave_arr.append(by_day[m["date_s"]][hr]["wh"])
            wind_arr.append(by_day[m["date_s"]][hr]["wspd"])

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Análise de Surf — Praia do Atalaia / Itajaí</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{{--bg:#0a1929;--panel:#0f2438;--panel2:#152e47;--border:#1e3a5f;--text:#e6edf3;--muted:#8aa2bd;--accent:#2dd4bf;--good:#22c55e;--mid:#f59e0b;--bad:#ef4444}}
  *{{box-sizing:border-box}}
  body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:linear-gradient(180deg,#061321 0%,#0a1929 100%);color:var(--text);min-height:100vh;padding:24px}}
  .container{{max-width:1280px;margin:0 auto}}
  header{{border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:28px}}
  .h-top{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
  h1{{margin:0;font-size:28px;font-weight:700;letter-spacing:-0.3px}}
  h1 .wave{{color:var(--accent)}}
  .subtitle{{color:var(--muted);font-size:14px;margin-top:6px}}
  .muted{{color:var(--muted);font-size:13px}}
  .badge{{display:inline-block;padding:4px 10px;border-radius:999px;background:var(--panel2);color:var(--accent);font-size:12px;font-weight:600;border:1px solid var(--border)}}
  .meta-bar{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px}}
  .meta{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px}}
  .meta-label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.5px}}
  .meta-value{{font-size:18px;font-weight:600;margin-top:4px}}
  .ranking{{background:linear-gradient(135deg,#0f2438 0%,#1a3553 100%);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:28px}}
  .ranking h2{{margin:0 0 14px 0;font-size:18px;font-weight:600}}
  .rank-row{{display:grid;grid-template-columns:60px 1fr auto;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid var(--border)}}
  .rank-row:last-child{{border-bottom:none}}
  .medal{{font-size:32px;text-align:center}}
  .rank-name{{font-weight:600;font-size:16px}}
  .rank-reason{{color:var(--muted);font-size:13px;margin-top:2px}}
  .score{{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}}
  .days{{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:20px;margin-bottom:28px}}
  .day-card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
  .day-head{{padding:16px 18px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)}}
  .day-head.good{{background:linear-gradient(90deg,rgba(34,197,94,0.15),transparent)}}
  .day-head.mid{{background:linear-gradient(90deg,rgba(245,158,11,0.12),transparent)}}
  .day-head.bad{{background:linear-gradient(90deg,rgba(239,68,68,0.10),transparent)}}
  .day-title{{font-size:18px;font-weight:700}}
  .day-sub{{color:var(--muted);font-size:12px;margin-top:2px}}
  .day-score{{font-size:28px;font-weight:800;font-variant-numeric:tabular-nums}}
  .day-score.good{{color:var(--good)}} .day-score.mid{{color:var(--mid)}} .day-score.bad{{color:var(--bad)}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border)}}
  .stat{{background:var(--panel);padding:12px;text-align:center}}
  .stat-label{{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.5px}}
  .stat-value{{font-size:15px;font-weight:600;margin-top:4px}}
  .hourly{{padding:14px 18px}}
  .hourly h3, .tides h3{{margin:0 0 10px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;font-weight:600}}
  table.hr-tbl{{width:100%;border-collapse:collapse;font-size:12px}}
  table.hr-tbl th{{font-weight:500;color:var(--muted);text-align:center;padding:6px 4px;font-size:11px;border-bottom:1px solid var(--border)}}
  table.hr-tbl td{{text-align:center;padding:6px 4px;border-bottom:1px solid #122a44}}
  table.hr-tbl td.lbl{{text-align:left;color:var(--muted);font-size:11px}}
  .cell-good{{background:rgba(34,197,94,0.18);color:#86efac;font-weight:600;border-radius:4px}}
  .cell-mid{{background:rgba(245,158,11,0.15);color:#fcd34d;font-weight:600;border-radius:4px}}
  .cell-bad{{background:rgba(239,68,68,0.15);color:#fca5a5;font-weight:600;border-radius:4px}}
  .tides{{padding:0 18px 14px}}
  .tide-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}}
  .tide-pill{{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px}}
  .tide-pill .tide-time{{font-weight:700;font-size:13px;display:block}}
  .tide-pill.alta{{border-color:#0ea5e9}}
  .tide-pill.baixa{{border-color:#64748b;opacity:0.85}}
  .window{{margin:0 18px 18px;padding:12px;background:rgba(45,212,191,0.08);border:1px solid rgba(45,212,191,0.3);border-radius:10px}}
  .window.bad{{background:rgba(239,68,68,0.08);border-color:rgba(239,68,68,0.3)}}
  .window.bad .win-label{{color:#fca5a5}}
  .win-label{{color:var(--accent);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px}}
  .win-text{{margin-top:4px;font-size:14px;line-height:1.5}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px}}
  @media(max-width:780px){{.charts{{grid-template-columns:1fr}}}}
  .chart-card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}}
  .chart-card h3{{margin:0 0 12px;font-size:14px;font-weight:600}}
  .chart-wrap{{position:relative;height:240px}}
  .gear-card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:28px}}
  .gear-card h2{{margin:0 0 6px;font-size:18px}}
  .gear-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:12px}}
  .gear-item{{background:var(--panel2);border-radius:10px;padding:14px}}
  .gear-icon{{font-size:24px;margin-bottom:6px}}
  .gear-title{{font-weight:600;font-size:14px}}
  .gear-desc{{color:var(--muted);font-size:12px;margin-top:4px;line-height:1.5}}
  .notes{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:20px;font-size:13px;line-height:1.6}}
  .notes h3{{margin:0 0 8px;font-size:15px}}
  .notes ul{{margin:8px 0 0;padding-left:20px;color:var(--muted)}}
  .surfguru-card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:28px}}
  .surfguru-head{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:8px}}
  .surfguru-head h2{{margin:0;font-size:18px}}
  .sg-link{{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;background:var(--panel2);padding:6px 12px;border-radius:8px;border:1px solid var(--border)}}
  .sg-link:hover{{background:var(--border)}}
  .iframe-wrap{{margin-top:12px;border-radius:10px;overflow:hidden;border:1px solid var(--border)}}
  .compare{{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}}
  .compare a{{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--accent);text-decoration:none;font-size:12px;font-weight:600}}
  .compare a:hover{{background:var(--border)}}
  footer{{color:var(--muted);font-size:11px;text-align:center;padding-top:20px;border-top:1px solid var(--border);margin-top:20px}}
  footer a{{color:var(--accent);text-decoration:none}}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="h-top">
      <div>
        <h1><span class="wave">≋</span> Análise de Surf — Praia do Atalaia</h1>
        <div class="subtitle">Itajaí / SC · próximos 3 dias · perfil intermediário</div>
      </div>
      <div><span class="badge">Atualizado {now_str}</span></div>
    </div>
    <div class="meta-bar">
      <div class="meta"><div class="meta-label">Pico</div><div class="meta-value">Atalaia (Molhes)</div></div>
      <div class="meta"><div class="meta-label">Coordenadas</div><div class="meta-value">26.92° S · 48.64° W</div></div>
      <div class="meta"><div class="meta-label">Swell ideal</div><div class="meta-value">SE (135°)</div></div>
      <div class="meta"><div class="meta-label">Temp. mar (média)</div><div class="meta-value">{sst_avg:.1f} °C</div></div>
    </div>
  </header>

  <section class="ranking">
    <h2>🏆 Ranking dos 3 dias</h2>
    {ranking}
  </section>

  <section class="days">
    {day_cards}
  </section>

  <section class="charts">
    <div class="chart-card">
      <h3>📈 Altura de onda (m)</h3>
      <div class="chart-wrap"><canvas id="waveChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>💨 Vento (nós)</h3>
      <div class="chart-wrap"><canvas id="windChart"></canvas></div>
    </div>
  </section>

  {render_gear(sst_avg)}

  <div class="notes">
    <h3>📝 Notas técnicas</h3>
    <p>Atalaia é beach break com molhe canalizando. Pede swell SE (135°) e vento terral (W/NW/SW). Canal do rio Itajaí-Açu cria corrente forte na vazante — atenção quando coef. de maré passar 80.</p>
    <ul>
      <li>Período curto (&lt;6s): vagas de vento local. Período &gt;= 8s: swell de tempestade, ondas com mais power.</li>
      <li>Janela terral típica: 6h–12h. Vento gira pra E/SE depois das 14h (maral).</li>
      <li>Maré média/enchente costuma render melhor.</li>
    </ul>
    <div class="compare">
      <strong style="font-size:12px;color:var(--muted);align-self:center;margin-right:4px">Compare com:</strong>
      <a href="https://surfguru.com.br/previsao/brasil/santa-catarina/itajai/praia-atalaia" target="_blank">Surfguru →</a>
      <a href="https://pt.surf-forecast.com/breaks/Atalaia/forecasts/latest/six_day" target="_blank">Surf-Forecast →</a>
      <a href="https://www.waves.com.br/surf/ondas/picos/sc/itajai/atalaia-meio/" target="_blank">Waves.com.br →</a>
      <a href="https://tabuademares.com/br/santa-catarina/itajai/previsao/ondas" target="_blank">Tábua de Marés →</a>
    </div>
  </div>

  <footer>
    Dados: <a href="https://open-meteo.com/" target="_blank">Open-Meteo</a> (Wavewatch III + GFS) ·
    marés <a href="https://tabuademares.com/br/santa-catarina/itajai" target="_blank">Tábua de Marés</a><br>
    Gerado automaticamente via GitHub Actions toda manhã às 6h BRT · <a href="https://github.com/rrenanfelix/atalaia-surf" target="_blank">código no GitHub</a>
  </footer>
</div>

<script>
const labels = {json.dumps(labels)};
const wave = {json.dumps(wave_arr)};
const wind = {json.dumps(wind_arr)};
const baseOpts = {{
  responsive:true, maintainAspectRatio:false,
  plugins:{{legend:{{display:false}},tooltip:{{mode:'index',intersect:false}}}},
  scales:{{
    x:{{ticks:{{color:'#8aa2bd',font:{{size:10}}}}, grid:{{color:'#152e47'}}}},
    y:{{ticks:{{color:'#8aa2bd',font:{{size:10}}}}, grid:{{color:'#152e47'}}, beginAtZero:true}}
  }}
}};
new Chart(document.getElementById('waveChart'), {{
  type:'line',
  data:{{labels:labels, datasets:[{{label:'Altura (m)', data:wave, borderColor:'#2dd4bf', backgroundColor:'rgba(45,212,191,0.15)', fill:true, tension:0.35, borderWidth:2, pointRadius:3}}]}},
  options:baseOpts
}});
new Chart(document.getElementById('windChart'), {{
  type:'bar',
  data:{{labels:labels, datasets:[{{label:'Vento (kt)', data:wind,
    backgroundColor:wind.map(v=>v<5?'rgba(34,197,94,0.6)':v<10?'rgba(245,158,11,0.6)':'rgba(239,68,68,0.6)'),
    borderColor:wind.map(v=>v<5?'#22c55e':v<10?'#f59e0b':'#ef4444'), borderWidth:1}}]}},
  options:baseOpts
}});
</script>
</body>
</html>
"""


def main():
    print("Buscando dados…")
    by_day, days_meta = fetch_data()
    print(f"\nDias coletados: {len(days_meta)}")
    for m in days_meta:
        print(f"  {m['date_s']} {m['weekday']}: score {m['score']}/10, "
              f"onda {m['wh_min']}–{m['wh_max']}m, melhor hora ~{m['best_hr']}h, "
              f"marés: {len(m['tides'])}")
    html = render_html(by_day, days_meta)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ index.html escrito ({len(html)} bytes)")


if __name__ == "__main__":
    main()
