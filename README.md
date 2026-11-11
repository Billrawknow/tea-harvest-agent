# Kericho Tea Harvest Agent

An AI agent for a tea estate field supervisor in Kericho. Each morning the supervisor asks a question like *"Is today good for harvesting?"*. The agent checks live weather for the estate and replies with a plain-language recommendation (**GOOD**, **MARGINAL** or **NOT RECOMMENDED**), the reason, and one practical action.

AI Masterclass 2, Week 3, Day 14: function calling and AI agents.

## The problem it solves

Plucking in the wrong conditions costs money. Wet leaf reduces quality and slows the factory, and heat stresses both pickers and bushes. The supervisor currently checks the weather and applies the estate's rules by hand. This agent does it in one question, using live data and the same rules every day.

## How it works: function calling

The AI decides **for itself** whether a question needs the weather:

```
Supervisor's question
        │
        ▼
   Claude reads the question + estate rules
        │
        ├── general question ("what is two leaves and a bud?")
        │        └──► answers directly
        │
        └── weather question ("is today good for harvesting?")
                 └──► calls get_weather tool
                          │
                          ▼
                 our Python code fetches live weather (Open-Meteo)
                          │
                          ▼
                 result sent back to Claude
                          │
                          ▼
                 recommendation using the actual numbers
```

The `get_weather` tool returns:
- **Now:** current temperature, humidity and whether it's raining.
- **Next 6 hours:** hour-by-hour temperature, humidity, rain amount and rain chance.
- **Summary:** total rain expected, the highest rain chance and the highest temperature over those 6 hours.

It defaults to the estate in Kericho, and also accepts another place name (e.g. "Kapsoit").

## Harvesting rules (in the system prompt)

| Verdict | Conditions |
|---|---|
| **GOOD** | No rain in the next 6 hours (under 0.5 mm and under 40% chance each hour), 15–25 °C, humidity below 75% |
| **NOT RECOMMENDED** | Raining now, or above 28 °C |
| **MARGINAL** | Anything in between, e.g. 25–28 °C, humidity 75% or higher, or a 40–60% chance of rain. The agent suggests timing instead. |

**The verdict is calculated in Python, not by the AI.** The weather tool applies these thresholds to every hour of the forecast and returns the verdict, the reason and the **first good hour**. The AI then explains that result in plain language. Exact number comparisons belong in code: in early testing, the AI applied the 75% humidity limit inconsistently from one answer to the next.

"Low humidity" isn't defined in the brief, so this project uses **below 75%**. To change it, edit `SYSTEM_PROMPT` in `tea_harvest_agent.py`.

## Setup

1. Get an Anthropic API key at [console.anthropic.com](https://console.anthropic.com) and add credit. The weather API (Open-Meteo) is free and needs no key.
2. Install the requirements:
   ```bash
   pip install -r requirements.txt
   ```
3. Create your `.env` file and paste in your key:
   ```bash
   cp .env.example .env
   ```

## Run

Ask questions, typing `exit` to quit:
```bash
python tea_harvest_agent.py
```

Run the built-in test (2 questions that need the weather tool, 2 that don't):
```bash
python tea_harvest_agent.py --test
```
Lines starting with `[tool]` show when the agent called the weather tool and what it got back. `[rules]` shows the verdict the code calculated.

## Example output

```
Kericho Tea Harvest Agent
-------------------------

Supervisor: Is today good for harvesting?
  [tool] get_weather({})
  [tool] Kericho: 12.9 C, 96% humidity, 0 mm rain expected next 6h (max chance 7%)
  [rules] MARGINAL: temperature 12.9 C is outside 15-25 C; humidity 96% is not below 75%
Agent: **MARGINAL.** It's not raining and rain chances stay low all morning, but it's cold and wet right now: 12.9 °C and 96% humidity. Temperature reaches the 15 °C minimum around 08:00 (16.4 °C), but humidity is still 90% then, and it doesn't drop below 75% until about 10:00 (79%), when the threshold is still not quite met. Wet leaf is hard to process, so delay the start rather than sending pluckers out at dawn. Have the crew start around 09:00 to 10:00 once the leaves have dried, and recheck humidity before then.

Supervisor: Should I send the pluckers out this morning or wait?
  [tool] get_weather({})
  [tool] Kericho: 12.9 C, 96% humidity, 0 mm rain expected next 6h (max chance 7%)
  [rules] MARGINAL: temperature 12.9 C is outside 15-25 C; humidity 96% is not below 75%
Agent: **MARGINAL.** It's cool and very damp right now: 12.9 °C and 96% humidity, with no rain. Conditions don't reach the good range in the next 6 hours, though they're improving steadily. Temperature is forecast to reach 16.4 °C by 08:00 and 18.3 °C by 09:00, while humidity drops to 84% at 09:00 and 79% at 10:00, and rain chance stays low (7% at most). I'd hold the pluckers until around 09:00, when the leaves should be drier and the temperature more comfortable, rather than sending them out into wet bushes now.

Supervisor: What is the standard 'two leaves and a bud' plucking rule?
Agent: The standard "two leaves and a bud" rule means plucking the bud along with the top two leaves beneath it, breaking the shoot just above the third leaf, which stays on the bush to keep the plant growing. This is a general tea-farming practice, so no weather check is needed for this answer.

Supervisor: How often should a tea bush be plucked during the rainy season?
Agent: During the rainy season, tea bushes generally need plucking every 7 to 10 days, since growth is fast and shoots flush quickly. In drier periods the interval often stretches to 10 to 14 days. Keep to the standard of taking the bud and the top two leaves, breaking just above the third leaf, which stays on the bush. Adjust the round to the bush's actual flush, so you pluck when most shoots have reached that stage rather than strictly by the calendar.
```

## Errors handled

| Situation | What happens |
|---|---|
| Weather service down or unreachable | The tool reports the error, and the agent says it couldn't check the weather and advises checking conditions on the ground (no invented figures) |
| Unknown place name | The tool reports "Couldn't find a place called…" |
| No API key, wrong key, no credit, rate limit | A plain-English message for each |
| Agent stuck calling tools | Stops after 5 rounds with a polite message |
