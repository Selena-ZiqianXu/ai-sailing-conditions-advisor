# ⛵ Lake Mendota Sailing Conditions Advisor

A small AI-assisted tool that helps Hoofers Sailing Club members decide
whether today's conditions on Lake Mendota (Madison, WI) are suitable for
sailing, based on their boat type and sailing rating.

**Live demo:** _[add your Streamlit Cloud link here]_
**Why this project:** I'm an active member of the Hoofers Sailing Club.
In the summer, before heading out I mainly check wind speed, wind
direction, and whether thunderstorms are in the forecast (Madison summers
are pleasant enough that I don't usually think about temperature unless
it's unusually hot). From there I estimate, based on experience, whether
conditions seem workable. This project tries to formalize that judgment
call, combining the same weather factors with club-specific rules and
local knowledge (e.g. how wind behaves near the Hoofers harbor) that a
generic weather app wouldn't know.

---

## What it does

Given a date, time, boat type, and sailor rating, the app:

1. Pulls live hourly weather data for Lake Mendota (wind speed, gusts,
   direction, visibility, temperature, UV index, rain chance)
2. Estimates a Hoofers-style flag (Green / Blue / Red) from that forecast
   and checks whether the chosen boat type + rating would be eligible to
   sail under it
3. Uses Claude to generate a short, situational set of notes, things like
   south-wind harbor behavior or gust risk, but only when they actually
   apply to that day's forecast

## Design: three layers, not one black box

A deliberate design choice in this project was to **only use AI where AI
actually adds value**, and keep everything else as plain, testable code:

| Layer | What it does | AI involved? |
|---|---|---|
| **Facts** | Raw weather data from Open-Meteo | No |
| **Flag & Eligibility** | Wind-speed thresholds, boat type and rating rules | No, pure rule-based logic |
| **Notes** | Situational, human-readable guidance | Yes, Claude |

Flag and eligibility are safety-relevant decisions, so they're computed by
deterministic code, not left to an LLM to infer. Claude is used only for
the part that genuinely benefits from language understanding: turning a
pile of numbers and local know-how into a short, readable heads-up, and
deciding which of those things are actually worth mentioning today.

One specific example of this split: gust risk. Forecast data can sometimes
show gusts close to or even below the sustained wind speed (a real quirk
of the underlying model in light-wind conditions), which would make a
naive AI comparison of the two numbers meaningless. Instead, the app
pre-computes the gust-to-wind ratio in Python and tells Claude the
conclusion directly ("this is/isn't worth flagging"), rather than asking
it to do the math and judgment call itself.

## How I used AI tools while building this

I built this end-to-end using Claude (chat, for planning and code
generation) and the Claude API (in the app itself, for the Notes feature).
Claude helped me scope the project down to something buildable in a day,
work through several rounds of UI and prompt-design feedback, and debug an
issue where the Notes output rendered as one run-on paragraph instead of a
bulleted list. The prompt sent to Claude for the Notes feature explicitly
separates "facts", "already-decided rule-based conclusions", and "local
knowledge to apply only if relevant", so the model's job is narrowly
scoped to summarizing and prioritizing, not recomputing anything
safety-relevant.

## Tech stack

- **Streamlit** for the web UI
- **Open-Meteo API** for weather data (free, no API key required)
- **Claude API** (`claude-sonnet-4-6`) for the Notes feature
- Deployed on Streamlit Community Cloud

## Running it locally

```bash
pip install -r requirements.txt
```

Create `.streamlit/secrets.toml` with:

```toml
ANTHROPIC_API_KEY = "your-key-here"
```

Then run:

```bash
streamlit run streamlit_app.py
```

## Safety note

The flag shown in this app is a **forecast-based estimate only**. Actual
Hoofers flags are set by staff and can diverge from a pure wind-speed
threshold (approaching storms, on-the-water judgment calls, etc.). Always
confirm the live flag before sailing:
https://uwpd.wisc.edu/services/lake-rescue-safety/#conditions

## Future improvements

This was intentionally scoped as a one-day MVP to validate whether the
core idea, combining live weather with local sailing rules and knowledge,
was worth building at all. A few things I'd tackle next, in rough order of
value:

- **Beyond a single location.** Lake Mendota's coordinates and the
  Hoofers flag thresholds are currently hardcoded. The natural next step
  is to turn location and flag rules into a configurable "profile" so the
  same underlying logic could support other bodies of water or clubs,
  each with their own thresholds and local quirks. This is essentially a
  smaller-scale version of the exact problem Glencliff Labs is solving:
  taking judgment that currently lives in one local expert's head and
  making it available at more locations.
- **A time window instead of a single hour.** Sailing outings usually
  span a few hours, not one instant. Right now the app snapshots a single
  hour; a better version would let the user pick a start time and
  duration, and base the flag/eligibility check on the most unfavorable
  hour in that window rather than just one point in time.
- **A second data source.** The app currently relies solely on
  Open-Meteo. I intentionally kept this to one source for the MVP since
  more sources add integration and debugging overhead without changing
  the core value proposition, but a local weather station feed could
  improve accuracy for a specific harbor's microclimate.
- **Water temperature.** Useful for hypothermia risk in shoulder-season
  sailing, but Open-Meteo's marine data doesn't cover small inland lakes
  like Mendota. Left out of this version rather than faked with a rough
  estimate.
