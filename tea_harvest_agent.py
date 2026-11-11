"""
Kericho Tea Harvest Agent
-------------------------
A morning assistant for a tea estate field supervisor. Ask a question like
"Is today good for harvesting?" and the agent decides whether it needs the
weather. If it does, it calls a weather tool (live data from Open-Meteo) and
turns the result into a practical, plain-language recommendation using the
estate's harvesting rules.

Usage:
    python tea_harvest_agent.py           # ask questions; type 'exit' to quit
    python tea_harvest_agent.py --test    # run the built-in test questions

Requirements:
    pip install -r requirements.txt
    A .env file containing:
        ANTHROPIC_API_KEY=your_key_here
    (The weather API needs no key.)
"""

import json
import os
import sys

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
MAX_TOKENS = 600
MAX_TOOL_ROUNDS = 5          # safety limit on the agent loop
TIMEOUT = 15
# Haiku thinks by default and thinking eats max_tokens; turn it off there only.
EXTRA_ARGS = {"thinking": {"type": "disabled"}} if "haiku" in MODEL else {}

# Estate harvesting rules (single source of truth: code computes the verdict)
GOOD_TEMP_RANGE = (15, 25)      # C
MAX_GOOD_HUMIDITY = 75          # %
MAX_GOOD_RAIN_MM = 0.5          # per hour
MAX_GOOD_RAIN_CHANCE = 40       # % per hour
BAD_TEMP_ABOVE = 28             # C

DEFAULT_LOCATION = {"name": "Kericho", "latitude": -0.3677, "longitude": 35.2831}
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

SYSTEM_PROMPT = """You are the morning assistant for a tea estate in Kericho, Kenya.
The field supervisor asks you questions before deciding the day's work.

When a question depends on current or upcoming weather (harvesting/plucking today,
rain, temperature, spraying, whether to send pluckers out), call the get_weather tool
first. For general tea-farming questions that don't depend on today's weather,
answer directly without calling the tool.

=== ESTATE HARVESTING RULES ===
GOOD conditions for harvesting (all must hold):
- No rain expected in the next 6 hours (expected rain under 0.5 mm and
  rain chance under 40% for every hour)
- Temperature between 15 C and 25 C
- Low humidity (below 75%)
BAD conditions (do not harvest):
- Active rain now
- Temperature above 28 C
Anything in between (e.g. 25-28 C, humidity 75% or higher, rain chance 40-60%):
call it MARGINAL and suggest timing, e.g. start early or wait for leaves to dry.

=== HOW TO ANSWER ===
- The weather tool returns "estate_rules_result" with the verdict and the
  first_good_hour, already calculated from the rules above. Use that verdict exactly;
  don't recalculate it. If first_good_hour is given, recommend starting then. If it is
  null, say conditions don't reach the good range in the next 6 hours.
- Start with a clear verdict: GOOD, MARGINAL or NOT RECOMMENDED.
- Then give the reason using the actual numbers (temperature, humidity, rain).
- End with one practical action for the supervisor.
- Plain language, 3-5 sentences. No markdown tables.
- Never mention tools, weather checks, or whether a question is "general"; just answer.
- For general tea questions, keep to well-established practice. Plucking standard:
  take the bud and the top two leaves, breaking just above the third leaf, which
  stays on the bush.
- Never invent weather figures. If the tool reports an error, say the weather
  couldn't be checked and advise checking conditions on the ground.
  - Only quote forecast values exactly as returned by the tool. Don't predict when a
  threshold will be crossed beyond what first_good_hour says.
"""

TOOLS = [
    {
        "name": "get_weather",
        "description": (
            "Get current weather and the next 6 hours of forecast for a location "
            "in Kenya: temperature, humidity, current rain, and hourly rain amount "
            "and chance of rain. Defaults to the estate in Kericho."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Place name, e.g. 'Kericho' or 'Kapsoit'. Omit for the estate in Kericho.",
                }
            },
            "required": [],
        },
    }
]

TEST_QUESTIONS = [
    "Is today good for harvesting?",                         # needs weather
    "Should I send the pluckers out this morning or wait?",  # needs weather
    "What is the standard 'two leaves and a bud' plucking rule?",  # no weather
    "How often should a tea bush be plucked during the rainy season?",  # no weather
]


class AgentError(Exception):
    """A problem we can explain to the supervisor in plain English."""


# ---------------------------------------------------------------- weather tool

def find_location(name):
    if not name or name.strip().lower() == "kericho":
        return DEFAULT_LOCATION
    response = requests.get(GEOCODE_URL, params={"name": name, "count": 1, "countryCode": "KE"},
                            timeout=TIMEOUT)
    response.raise_for_status()
    results = response.json().get("results")
    if not results:
        raise ValueError(f"Couldn't find a place called '{name}' in Kenya.")
    r = results[0]
    return {"name": r["name"], "latitude": r["latitude"], "longitude": r["longitude"]}


def get_weather(location=None):
    """Runs when the AI calls the tool. Returns a dict (sent back to the AI as JSON)."""
    try:
        place = find_location(location)
        params = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,precipitation,rain,weather_code",
            "hourly": "temperature_2m,relative_humidity_2m,precipitation,precipitation_probability",
            "forecast_hours": 6,
            "timezone": "auto",
        }
        response = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as err:
        return {"error": f"Weather service unavailable: {type(err).__name__}"}
    except (ValueError, KeyError) as err:
        return {"error": str(err)}

    now, hourly = data["current"], data["hourly"]
    next_hours = [
        {
            "time": hourly["time"][i][-5:],
            "temp_c": hourly["temperature_2m"][i],
            "humidity_pct": hourly["relative_humidity_2m"][i],
            "rain_mm": hourly["precipitation"][i],
            "rain_chance_pct": hourly["precipitation_probability"][i],
        }
        for i in range(len(hourly["time"]))
    ]
    current = {
        "temp_c": now["temperature_2m"],
        "humidity_pct": now["relative_humidity_2m"],
        "raining_now": (now.get("rain") or now.get("precipitation") or 0) > 0,
        "rain_mm_now": now.get("precipitation", 0),
    }
    return {
        "location": place["name"],
        "local_time": now["time"].replace("T", " "),
        "now": current,
        "estate_rules_result": harvest_verdict(current, next_hours),
        "next_6_hours": next_hours,
        "summary": {
            "total_rain_mm_next_6h": round(sum(h["rain_mm"] or 0 for h in next_hours), 1),
            "max_rain_chance_pct_next_6h": max((h["rain_chance_pct"] or 0 for h in next_hours), default=0),
            "max_temp_c_next_6h": max((h["temp_c"] for h in next_hours), default=None),
        },
    }


def hour_is_good(h):
    return (GOOD_TEMP_RANGE[0] <= h["temp_c"] <= GOOD_TEMP_RANGE[1]
            and h["humidity_pct"] < MAX_GOOD_HUMIDITY
            and (h["rain_mm"] or 0) < MAX_GOOD_RAIN_MM
            and (h["rain_chance_pct"] or 0) < MAX_GOOD_RAIN_CHANCE)


def harvest_verdict(now, hours):
    """Apply the estate rules exactly, so the AI only has to explain the result."""
    if now["raining_now"]:
        return {"verdict": "NOT RECOMMENDED", "reason": "it is raining now", "first_good_hour": None}
    if any(h["temp_c"] > BAD_TEMP_ABOVE for h in hours) or now["temp_c"] > BAD_TEMP_ABOVE:
        return {"verdict": "NOT RECOMMENDED", "reason": f"temperature above {BAD_TEMP_ABOVE} C",
                "first_good_hour": None}
    rain_ahead = any((h["rain_mm"] or 0) >= MAX_GOOD_RAIN_MM
                     or (h["rain_chance_pct"] or 0) >= MAX_GOOD_RAIN_CHANCE for h in hours)
    good_hours = [h["time"] for h in hours if hour_is_good(h)]
    if not rain_ahead and good_hours and good_hours[0] == hours[0]["time"]:
        return {"verdict": "GOOD", "reason": "all conditions met now and no rain in the next 6 hours",
                "first_good_hour": good_hours[0]}
    problems = []
    if rain_ahead:
        problems.append("rain likely within 6 hours")
    if not GOOD_TEMP_RANGE[0] <= now["temp_c"] <= GOOD_TEMP_RANGE[1]:
        problems.append(f"temperature {now['temp_c']} C is outside {GOOD_TEMP_RANGE[0]}-{GOOD_TEMP_RANGE[1]} C")
    if now["humidity_pct"] >= MAX_GOOD_HUMIDITY:
        problems.append(f"humidity {now['humidity_pct']}% is not below {MAX_GOOD_HUMIDITY}%")
    return {"verdict": "MARGINAL", "reason": "; ".join(problems) or "conditions borderline",
            "first_good_hour": good_hours[0] if good_hours else None}


TOOL_FUNCTIONS = {"get_weather": get_weather}


# ---------------------------------------------------------------- agent loop

def call_model(client, messages):
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            **EXTRA_ARGS,
        )
    except anthropic.APIConnectionError:
        raise AgentError("No internet connection. Check your connection and try again.")
    except anthropic.AuthenticationError:
        raise AgentError("API key is wrong or expired. Check your .env file.")
    except anthropic.RateLimitError:
        raise AgentError("Too many requests. Wait 60 seconds and try again.")
    except anthropic.BadRequestError as err:
        if "credit balance" in str(err).lower():
            raise AgentError("Your Anthropic account has no credit. "
                             "Add funds at console.anthropic.com > Billing.")
        raise AgentError(f"The request was rejected: {err.message}")
    except anthropic.NotFoundError:
        raise AgentError(f"Model '{MODEL}' wasn't found. Check CLAUDE_MODEL in your .env file.")
    except anthropic.APIStatusError as err:
        raise AgentError(f"The AI service returned an error ({err.status_code}). Try again shortly.")


def run_agent(client, question):
    """Ask -> (AI calls tools -> we run them -> send results back)* -> final answer."""
    messages = [{"role": "user", "content": question}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = call_model(client, messages)
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text").strip()

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [tool] {block.name}({json.dumps(block.input)})")
            function = TOOL_FUNCTIONS.get(block.name)
            output = function(**block.input) if function else {"error": f"Unknown tool {block.name}"}
            if "error" in output:
                print(f"  [tool] error: {output['error']}")
            else:
                s = output["summary"]
                print(f"  [tool] {output['location']}: {output['now']['temp_c']} C, "
                      f"{output['now']['humidity_pct']}% humidity, "
                      f"{s['total_rain_mm_next_6h']} mm rain expected next 6h "
                      f"(max chance {s['max_rain_chance_pct_next_6h']}%)")
                v = output["estate_rules_result"]
                print(f"  [rules] {v['verdict']}: {v['reason']}"
                      + (f" | first good hour: {v['first_good_hour']}" if v["first_good_hour"] else ""))
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(output),
                **({"is_error": True} if "error" in output else {}),
            })
        messages.append({"role": "user", "content": results})

    return "Sorry, I couldn't finish working that out. Please try asking again."


# ---------------------------------------------------------------- main

def get_client():
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        sys.exit("No API key found. Add ANTHROPIC_API_KEY=your_key_here to your .env file.")
    return anthropic.Anthropic(api_key=api_key)


def ask_and_print(client, question):
    print(f"\nSupervisor: {question}")
    answer = run_agent(client, question)
    print(f"Agent: {answer}")


def main():
    print("Kericho Tea Harvest Agent")
    print("-" * 25)
    client = get_client()
    try:
        if "--test" in sys.argv[1:]:
            for question in TEST_QUESTIONS:
                ask_and_print(client, question)
            return
        print("Ask a question, e.g. 'Is today good for harvesting?'. Type 'exit' to quit.")
        while True:
            try:
                question = input("\nSupervisor: ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if question.lower() in ("exit", "quit"):
                break
            if question:
                answer = run_agent(client, question)
                print(f"Agent: {answer}")
    except AgentError as err:
        print(f"\nSorry: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
