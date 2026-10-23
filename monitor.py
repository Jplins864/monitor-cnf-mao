import os
import json
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# Configurações do Telegram e API
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"

STATE_FILE = "state.json"
ORIGIN = "CNF"
DESTINATION = "MAO"

# As 4 combinações solicitadas
COMBINATIONS = [
    ("2026-12-15", "2027-01-09", "15/12 (Ter) ➔ 09/01 (Sáb)"),
    ("2026-12-15", "2027-01-10", "15/12 (Ter) ➔ 10/01 (Dom)"),
    ("2026-12-16", "2027-01-09", "16/12 (Qua) ➔ 09/01 (Sáb)"),
    ("2026-12-16", "2027-01-10", "16/12 (Qua) ➔ 10/01 (Dom)"),
]


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_daily_report_date": "", "daily_min": {}, "all_time_min": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def google_flights_link(out_date, ret_date):
    return (
        f"https://www.google.com/travel/flights?q=Flights%20to%20{DESTINATION}%20"
        f"from%20{ORIGIN}%20on%20{out_date}%20through%20{ret_date}&curr=BRL&hl=pt-BR"
    )


def fetch_via_serpapi(out_date, ret_date):
    if not SERPAPI_KEY:
        return None
    params = {
        "engine": "google_flights",
        "departure_id": ORIGIN,
        "arrival_id": DESTINATION,
        "outbound_date": out_date,
        "return_date": ret_date,
        "currency": "BRL",
        "hl": "pt-br",
        "gl": "br",
        "type": "1",
        "api_key": SERPAPI_KEY,
    }
    try:
        resp = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if "error" in data:
            print(f"Aviso SerpApi: {data['error']}")
            return None

        flights = data.get("best_flights", []) + data.get("other_flights", [])
        if not flights:
            return None

        best_overall = None
        best_direct = None

        for f in flights:
            price = f.get("price")
            if not price:
                continue
            legs = f.get("flights", [])
            airlines = ", ".join(sorted({leg.get("airline", "Cia") for leg in legs}))
            stops = max(0, len(legs) - 1)
            info = {
                "price": int(price),
                "airline": airlines,
                "stops": stops,
                "url": data.get("search_metadata", {}).get(
                    "google_flights_url", google_flights_link(out_date, ret_date)
                ),
            }

            if best_overall is None or info["price"] < best_overall["price"]:
                best_overall = info
            if stops == 0 and (best_direct is None or info["price"] < best_direct["price"]):
                best_direct = info

        if best_overall:
            best_overall["direct_price"] = best_direct["price"] if best_direct else None
            best_overall["direct_airline"] = best_direct["airline"] if best_direct else None
        return best_overall
    except Exception as e:
        print(f"Erro na consulta SerpApi ({out_date} -> {ret_date}): {e}")
        return None


def fetch_via_fast_flights(out_date, ret_date):
    """Fallback gratuito e ilimitado caso a SerpApi não esteja configurada ou esgote os créditos."""
    try:
        from fast_flights import FlightData, Passengers, get_flights

        result = get_flights(
            flight_data=[
                FlightData(date=out_date, from_airport=ORIGIN, to_airport=DESTINATION),
                FlightData(date=ret_date, from_airport=DESTINATION, to_airport=ORIGIN),
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(adults=1),
            fetch_mode="common",
        )
        best_overall = None
        best_direct = None

        for f in getattr(result, "flights", []):
            raw_price = str(getattr(f, "price", ""))
            digits = re.sub(r"[^\d]", "", raw_price)
            if not digits:
                continue
            price = int(digits)
            stops = getattr(f, "stops", 1)
            airline = getattr(f, "name", "Cia Aérea")

            info = {
                "price": price,
                "airline": airline,
                "stops": stops if isinstance(stops, int) else 1,
                "url": google_flights_link(out_date, ret_date),
            }
            if best_overall is None or price < best_overall["price"]:
                best_overall = info
            if info["stops"] == 0 and (best_direct is None or price < best_direct["price"]):
                best_direct = info

        if best_overall:
            best_overall["direct_price"] = best_direct["price"] if best_direct else None
            best_overall["direct_airline"] = best_direct["airline"] if best_direct else None
        return best_overall
    except Exception as e:
        print(f"Erro no fallback fast-flights ({out_date} -> {ret_date}): {e}")
        return None


def get_flight_quote(out_date, ret_date):
    quote = fetch_via_serpapi(out_date, ret_date)
    if not quote:
        quote = fetch_via_fast_flights(out_date, ret_date)
    return quote


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Configure TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()


def main():
    now_brt = datetime.now(ZoneInfo("America/Sao_Paulo"))
    today_str = now_brt.strftime("%Y-%m-%d")
    time_str = now_brt.strftime("%d/%m/%Y às %H:%M")

    state = load_state()
    is_first_run_of_day = (state.get("last_daily_report_date") != today_str) or FORCE_REPORT

    results = {}
    for out_date, ret_date, label in COMBINATIONS:
        key = f"{out_date}_{ret_date}"
        quote = get_flight_quote(out_date, ret_date)
        if quote:
            quote["label"] = label
            results[key] = quote

    if not results:
        print("Nenhuma cotação retornada nesta execução.")
        return

    # 1. Boletim Diário (primeira execução do dia ou teste manual)
    if is_first_run_of_day:
        state["last_daily_report_date"] = today_str
        state["daily_min"] = {}

        cheapest_key = min(results, key=lambda k: results[k]["price"])
        lines = [
            f"✈️ <b>Boletim Diário: Confins (CNF) ⇄ Manaus (MAO)</b>",
            f"🕒 <i>Consulta em {time_str}</i>\n",
        ]

        for out_date, ret_date, label in COMBINATIONS:
            key = f"{out_date}_{ret_date}"
            q = results.get(key)
            if not q:
                continue

            price = q["price"]
            state["daily_min"][key] = price
            prev_all_time = state["all_time_min"].get(key)
            if prev_all_time is None or price < prev_all_time:
                state["all_time_min"][key] = price

            stops_txt = "Direto" if q["stops"] == 0 else f"{q['stops']} parada(s)"
            crown = " 🏆 <b>(Menor do dia)</b>" if key == cheapest_key else ""
            direct_line = ""
            if q.get("direct_price") and q["stops"] > 0:
                direct_line = f"\n   └ <i>Voo direto ({q['direct_airline']}): R$ {q['direct_price']}</i>"

            lines.append(
                f"📅 <b>{label}</b>{crown}\n"
                f"💰 <b>R$ {price}</b> • {q['airline']} ({stops_txt})"
                f"{direct_line}\n"
                f"🔗 <a href='{q['url']}'>Ver no Google Voos</a>\n"
            )

        send_telegram("\n".join(lines))
        save_state(state)
        return

    # 2. Checagens intradiárias: só avisa se cair abaixo do menor preço do dia
    drops = []
    for key, q in results.items():
        price = q["price"]
        prev_daily_min = state["daily_min"].get(key)
        prev_all_time = state["all_time_min"].get(key)

        if prev_daily_min is None:
            state["daily_min"][key] = price
            continue

        if price < prev_daily_min:
            diff = prev_daily_min - price
            is_record = prev_all_time is None or price < prev_all_time
            state["daily_min"][key] = price
            if is_record:
                state["all_time_min"][key] = price

            stops_txt = "Direto" if q["stops"] == 0 else f"{q['stops']} parada(s)"
            record_badge = " 🚨 <b>NOVO RECORDE HISTÓRICO!</b>" if is_record else ""
            drops.append(
                f"📅 <b>{q['label']}</b>{record_badge}\n"
                f"📉 Caiu de R$ {prev_daily_min} para <b>R$ {price}</b> (-R$ {diff})\n"
                f"🛫 {q['airline']} ({stops_txt})\n"
                f"🔗 <a href='{q['url']}'>Garantir no Google Voos</a>\n"
            )

    if drops:
        header = (
            f"🔥 <b>Queda de Preço no Dia! (CNF ⇄ MAO)</b>\n"
            f"🕒 <i>Detectado em {time_str}</i>\n\n"
        )
        send_telegram(header + "\n".join(drops))

    save_state(state)


if __name__ == "__main__":
    main()
