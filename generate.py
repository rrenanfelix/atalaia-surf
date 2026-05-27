#!/usr/bin/env python3
"""
Atalaia Surf Report — gerador automático.

Busca dados frescos do Open-Meteo (Wavewatch III + GFS) para a Praia do Atalaia
(Itajaí / SC), calcula o ranking dos próximos 3 dias para um surfista
intermediário, e regrava o index.html.

Roda diariamente via GitHub Actions às 6h BRT.
"""

import json
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ───────── Configuração ─────────
LAT = -26.92
LON = -48.64
TZ = "America/Sao_Paulo"
BR = ZoneInfo(TZ)
KEY_HOURS = [6, 9, 12, 15, 18]

# Atalaia: beach break com molhe, swell ideal SE (135°), vento ideal terral (W/NW/SW)


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "atalaia-surf-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def deg_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) % 360 / 22.5)]


def wind_kind(deg: float) -> str:
    """Atalaia faz fundo pra leste — vento de W/NW/SW é terral, E/NE/SE é maral."""
    d = deg % 360
    if 200 <= d <= 340:
        return "terral"
    if 50 <= d <= 170:
        return "maral"
    return "cruzado"


def cell_class_wave(h: float) -> str:
    if h >= 0.8:
        return "cell-good"
    if h >= 0.6:
        return "cell-mid"
    return "cell-bad"


def cell_class_period(p: float) -> str:
    if p >= 8:
        return "cell-good"
    if p >= 6:
        return "cell-mid"
    return "cell-bad"


def cell_class_wind(ws: float, wdir: float) -> str:
    kind = wind_kind(wdir)
    # vento terral leve = ótimo; calmo = ótimo; maral forte = ruim
    if kind == "terral" and ws < 12:
        return "cell-good"
    if ws < 5:
        return "cell-good"
    if kind == "maral" and ws > 8:
        return "cell-bad"
    return "cell-mid"


def score_day(hours: dict) -> float:
    """Score 0-10 baseado nas janelas surfáveis (6h-12h primário)."""
    morning = [h for h in KEY_HOURS if h <= 12]
    pts = 0
    for hr in morning:
        d = hours[hr]
        # onda
        if d["wh"] >= 0.7:
            pts += 1.5
        elif d["wh"] >= 0.5:
            pts += 0.8
        # período
        if d["wp"] >= 7:
            pts += 1.0
        elif d["wp"] >= 6:
            pts += 0.5
        # vento
        k = wind_kind(d["windir"])
        if k == "terral" and d["wspd"] < 12:
            pts += 1.0
        elif d["wspd"] < 5:
            pts += 0.7
        elif k == "maral":
            pts -= 0.5
    return round(max(0, min(10, pts)), 1)


def fetch_data():
    today = datetime.now(BR).date()
    start = today.isoformat()
    end = (today + timedelta(days=2)).isoformat()

    marine = fetch(
        f"https://marine-api.open-meteo.com/v1/marine?latitude={LAT}&longitude={LON}"
        f"&hourly=wave_height,wave_direction,wave_period,swell_wave_height,"
        f"swell_wave_direction,swell_wave_period,sea_surface_temperature"
        f"&start_date={start}&end_date={end}&timezone={TZ}"
    )
    weather = fetch(
        f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
        f"&hourly=temperature_2m,precipitation,cloud_cover,wind_speed_10m,"
        f"wind_direction_10m,wind_gusts_10m"
        f"&daily=sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&start_date={start}&end_date={end}&timezone={TZ}&wind_speed_unit=kn"
    )

    by_day = {}
    mh, wh = marine["hourly"], weather["hourly"]
    for i, t in enumerate(mh["time"]):
        date_s, hour = t[:10], int(t[11:13])
        if hour not in KEY_HOURS:
            continue
        by_day.setdefault(date_s, {})[hour] = {
            "wh": mh["wave_height"][i],
            "wd": mh["wave_direction"][i],
            "wp": mh["wave_period"][i],
            "sst": mh["sea_surface_temperature"][i],
        }
    for i, t in enumerate(wh["time"]):
        date_s, hour = t[:10], int(t[11:13])
        if hour not in KEY_HOURS or date_s not in by_day or hour not in by_day[date_s]:
            continue
        by_day[date_s][hour].update({
            "temp": wh["temperature_2m"][i],
            "wspd": wh["wind_speed_10m"][i],
            "windir": wh["wind_direction_10m"][i],
            "wgust": wh["wind_gusts_10m"][i],
        })

    # Daily aggregates
    daily = {}
    for date_s in by_day:
        whs = [by_day[date_s][h]["wh"] for h in KEY_HOURS if h in by_day[date_s]]
        wps = [by_day[date_s][h]["wp"] for h in KEY_HOURS if h in by_day[date_s]]
        ssts = [by_day[date_s][h]["sst"] for h in KEY_HOURS if h in by_day[date_s]]
        daily[date_s] = {
            "wh_min": round(min(whs), 2),
            "wh_max": round(max(whs), 2),
            "wp_avg": round(sum(wps) / len(wps), 1),
            "sst_avg": round(sum(ssts) / len(ssts), 1),
            "score": score_day(by_day[date_s]),
        }

    weekday = {0: "Segunda", 1: "Terça", 2: "Quarta", 3: "Quinta",
               4: "Sexta", 5: "Sábado", 6: "Domingo"}
    months = ["jan", "fev", "mar", "abr", "mai", "jun",
              "jul", "ago", "set", "out", "nov", "dez"]

    days_meta = []
    for i, date_s in enumerate(sorted(by_day.keys())):
        d = datetime.strptime(date_s, "%Y-%m-%d").date()
        days_meta.append({
            "date_s": date_s,
            "weekday": weekday[d.weekday()],
            "short": f"{d.day:02d} {months[d.month-1]}",
            "tmax": weather["daily"]["temperature_2m_max"][i],
            "tmin": weather["daily"]["temperature_2m_min"][i],
            "rain": weather["daily"]["precipitation_sum"][i],
            "sunrise": weather["daily"]["sunrise"][i][11:16],
            "sunset": weather["daily"]["sunset"][i][11:16],
            **daily[date_s],
        })

    return by_day, days_meta, weather["daily"]


def day_class(score: float) -> str:
    if score >= 6:
        return "good"
    if score >= 4:
        return "mid"
    return "bad"


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
        for hr in KEY_HOURS:
            d = hours[hr]
            val = d[label_key]
            cells.append(f'<td class="{cls_fn(d)}">{fmt(val)}</td>')
        rows.append("".join(cells))

    return f"""
    <div class="day-card">
      <div class="day-head {cls}">
        <div>
          <div class="day-title">{meta['weekday']} · {meta['short']}</div>
          <div class="day-sub">☀️ {meta['tmin']:.1f}° – {meta['tmax']:.1f}°C  ·  💧 {meta['rain']:.1f} mm  ·  ☀ {meta['sunrise']}–{meta['sunset']}</div>
        </div>
        <div class="day-score {cls}">{meta['score']}</div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-label">Onda</div><div class="stat-value">{str(meta['wh_min']).replace('.', ',')} – {str(meta['wh_max']).replace('.', ',')} m</div></div>
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
            <div class="rank-reason">Onda {str(d['wh_min']).replace('.', ',')}–{str(d['wh_max']).replace('.', ',')} m · período {str(d['wp_avg']).replace('.', ',')} s · score {d['score']}/10</div>
          </div>
          <div class="score" style="color:var(--{colors[i]})">{d['score']}</div>
        </div>""")
    return "".join(rows)


def render_html(by_day: dict, days_meta: list) -> str:
    now_str = datetime.now(BR).strftime("%d/%m/%Y · %H:%M")
    sst_avg = sum(d["sst_avg"] for d in days_meta) / len(days_meta)

    day_cards = "\n".join(render_day_card(m, by_day[m["date_s"]]) for m in days_meta)
    ranking = render_ranking(days_meta)

    # Chart data: 24 buckets de 3h (5 dias seria muito). Aqui faz 15 pontos (3 dias × 5 horas-chave)
    labels = []
    wave_arr = []
    wind_arr = []
    for m in days_meta:
        for hr in KEY_HOURS:
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
  .hourly h3{{margin:0 0 10px;font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;font-weight:600}}
  table.hr-tbl{{width:100%;border-collapse:collapse;font-size:12px}}
  table.hr-tbl th{{font-weight:500;color:var(--muted);text-align:center;padding:6px 4px;font-size:11px;border-bottom:1px solid var(--border)}}
  table.hr-tbl td{{text-align:center;padding:6px 4px;border-bottom:1px solid #122a44}}
  table.hr-tbl td.lbl{{text-align:left;color:var(--muted);font-size:11px}}
  .cell-good{{background:rgba(34,197,94,0.18);color:#86efac;font-weight:600;border-radius:4px}}
  .cell-mid{{background:rgba(245,158,11,0.15);color:#fcd34d;font-weight:600;border-radius:4px}}
  .cell-bad{{background:rgba(239,68,68,0.15);color:#fca5a5;font-weight:600;border-radius:4px}}
  .charts{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px}}
  @media(max-width:780px){{.charts{{grid-template-columns:1fr}}}}
  .chart-card{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}}
  .chart-card h3{{margin:0 0 12px;font-size:14px;font-weight:600}}
  .chart-wrap{{position:relative;height:240px}}
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
      <div class="meta"><div class="meta-label">Swell ideal</div><div class="meta-value">SE</div></div>
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

  <footer>
    Dados: <a href="https://open-meteo.com/" target="_blank">Open-Meteo</a> (Wavewatch III + GFS) · marés em
    <a href="https://tabuademares.com/br/santa-catarina/itajai/previsao/mares" target="_blank">Tábua de Marés</a>.<br>
    Gerado automaticamente via GitHub Actions toda manhã.
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
    print("Buscando dados do Open-Meteo…")
    by_day, days_meta, _ = fetch_data()
    print(f"Coletado: {len(days_meta)} dias")
    for m in days_meta:
        print(f"  {m['date_s']} {m['weekday']}: score {m['score']}/10, onda {m['wh_min']}–{m['wh_max']} m")
    html = render_html(by_day, days_meta)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ index.html escrito ({len(html)} bytes)")


if __name__ == "__main__":
    main()
