"""
Lake Mendota Sailing Conditions Advisor
A Streamlit app that:
1. Fetches weather data from Open-Meteo (no API key needed)
2. Computes an ESTIMATED flag level from wind speed (Hoofers-style thresholds)
3. Checks eligibility based on boat type + sailor rating
4. Uses Claude API to generate human-readable notes/warnings based on
   local sailing knowledge (e.g. south wind behavior at Hoofers)

IMPORTANT: The estimated flag is a forecast-based approximation only.
Actual flags are set by Hoofers staff and may differ. Always check the
live status before sailing: https://uwpd.wisc.edu/services/lake-rescue-safety/#conditions
"""

import requests
import streamlit as st
from datetime import date
import anthropic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Approximate coordinates for Lake Mendota / Hoofers Sailing Club (Madison, WI)
LATITUDE = 43.0800
LONGITUDE = -89.4100

HOOFERS_LIVE_STATUS_URL = "https://uwpd.wisc.edu/services/lake-rescue-safety/#conditions"

# WMO weather codes that indicate thunderstorms
THUNDERSTORM_CODES = {95, 96, 99}

BOAT_TYPES = ["Dinghy", "Sloop", "Keelboat"]
SAILOR_RATINGS = ["Light Weather Rating", "Heavy Weather Rating"]


# ---------------------------------------------------------------------------
# Step 1: Fetch weather data (Facts) — no AI involved
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800)  # cache for 30 minutes to avoid hammering the API
def fetch_weather(target_date: str) -> dict:
    """
    Fetch hourly weather data for Lake Mendota from Open-Meteo.
    Returns the raw hourly data dict.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": [
            "temperature_2m",
            "precipitation_probability",
            "weathercode",
            "windspeed_10m",
            "windgusts_10m",
            "winddirection_10m",
            "visibility",
            "uv_index",
        ],
        "start_date": target_date,
        "end_date": target_date,
        "windspeed_unit": "mph",  # matches Hoofers flag thresholds (mph)
        "timezone": "America/Chicago",
    }
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()["hourly"]


def summarize_midday_conditions(hourly: dict) -> dict:
    """
    Pick a representative midday hour (index 12, i.e. ~12:00 local time)
    as a simple summary snapshot for the day.
    A more advanced version could let the user pick a specific hour.
    """
    idx = 12
    return {
        "temperature_c": hourly["temperature_2m"][idx],
        "precipitation_probability": hourly["precipitation_probability"][idx],
        "weathercode": hourly["weathercode"][idx],
        "windspeed_mph": hourly["windspeed_10m"][idx],
        "windgusts_mph": hourly["windgusts_10m"][idx],
        "winddirection_deg": hourly["winddirection_10m"][idx],
        "visibility_m": hourly["visibility"][idx],
        "uv_index": hourly["uv_index"][idx],
    }


def wind_direction_to_compass(degrees: float) -> str:
    """Convert wind direction in degrees to a compass label."""
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return directions[idx]


# ---------------------------------------------------------------------------
# Step 2: Estimated flag + eligibility — rule-based, no AI
# ---------------------------------------------------------------------------

def estimate_flag(windspeed_mph: float, weathercode: int) -> str:
    """
    Estimate a Hoofers-style flag from forecast wind speed.
    NOTE: This is an approximation. Real flags are set by staff and can
    diverge from a pure wind-speed threshold (e.g. incoming storms,
    lifeguard judgment). This estimate should never replace checking
    the live status page.
    """
    if weathercode in THUNDERSTORM_CODES:
        return "Red (storm signal in forecast — lake likely closed)"
    if windspeed_mph <= 18:
        return "Green (estimated)"
    elif windspeed_mph <= 30:
        return "Blue (estimated)"
    else:
        return "Blue/Red (estimated — over 30 mph, limited or no craft allowed)"


def check_eligibility(estimated_flag: str, boat_type: str, rating: str) -> dict:
    """
    Rule-based eligibility check based on Hoofers sailing rules:
    - Green: any rating may sail
    - Blue: requires Heavy Weather Rating; Sloops may NOT sail in Blue
    - Blue/Red or Red: no sailing equipment allowed out
    Returns a dict with an eligibility flag and the reason.
    """
    if estimated_flag.startswith("Red") or estimated_flag.startswith("Blue/Red"):
        return {"eligible": False, "reason": "Wind/storm conditions exceed safe sailing limits."}

    if estimated_flag.startswith("Green"):
        return {"eligible": True, "reason": "Conditions estimated within Green flag range."}

    if estimated_flag.startswith("Blue"):
        if rating != "Heavy Weather Rating":
            return {"eligible": False, "reason": "Blue flag conditions require a Heavy Weather Rating."}
        if boat_type == "Sloop":
            return {"eligible": False, "reason": "Sloops are not permitted to sail in Blue flag conditions."}
        return {"eligible": True, "reason": "Heavy Weather Rating + non-Sloop boat meets Blue flag requirements."}

    return {"eligible": False, "reason": "Unable to determine eligibility from estimated flag."}


# ---------------------------------------------------------------------------
# Step 3: AI-generated notes — combines facts + local knowledge
# ---------------------------------------------------------------------------

def build_prompt(conditions: dict, compass_dir: str, boat_type: str, rating: str,
                  estimated_flag: str, eligibility: dict) -> str:
    """
    Build the prompt for Claude, embedding Hoofers-specific local knowledge
    (e.g. south wind behavior near the clubhouse) so the model can reason
    about nuances that pure thresholds can't capture.
    """
    return f"""You are a sailing conditions assistant for Hoofers Sailing Club on Lake Mendota (Madison, WI).

Today's forecast conditions (midday snapshot):
- Wind speed: {conditions['windspeed_mph']} mph
- Wind gusts: {conditions['windgusts_mph']} mph
- Wind direction: {compass_dir} ({conditions['winddirection_deg']}°)
- Visibility: {conditions['visibility_m']} m
- Temperature: {conditions['temperature_c']}°C
- UV index: {conditions['uv_index']}
- Precipitation probability: {conditions['precipitation_probability']}%

Sailor input:
- Boat type: {boat_type}
- Sailor rating: {rating}

Rule-based assessment (already computed, do not recompute):
- Estimated flag: {estimated_flag}
- Eligibility: {"Eligible" if eligibility['eligible'] else "Not eligible"} — {eligibility['reason']}

Local knowledge to apply:
- Hoofers Sailing Club sits on the south shore of Lake Mendota, surrounded by
  buildings near the harbor. South winds behave unusually here:
  - Light south wind: the shoreline can be nearly wind-still, making it hard
    to sail back into the harbor.
  - Strong south wind: turbulent, gusty air near the shore due to buildings.
- Gusts significantly higher than sustained wind speed (roughly 50%+ higher)
  indicate an increased risk of sudden knockdowns.
- Keelboats are generally more stable than dinghies in gusty conditions.

Write a short, practical set of notes (3-5 sentences) for the sailor covering:
1. Any south-wind-specific caution if wind direction is roughly S/SSW/SSE.
2. A note on gust risk if gusts are notably higher than sustained wind.
3. A reminder that the flag above is a forecast-based ESTIMATE, and the
   sailor should confirm the live flag at the clubhouse or via
   {HOOFERS_LIVE_STATUS_URL} before heading out.
4. Any sun/heat exposure note if UV index is high (7+).

Keep the tone practical and concise, like a knowledgeable club member giving
a quick heads-up — not a formal weather report."""


def generate_ai_notes(prompt: str, api_key: str) -> str:
    """Call Claude API to generate the notes section."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Lake Mendota Sailing Advisor", page_icon="⛵")
    st.title("⛵ Lake Mendota Sailing Conditions Advisor")
    st.caption("A Hoofers-flavored decision helper — not an official flag status.")

    # --- Inputs ---
    col1, col2, col3 = st.columns(3)
    with col1:
        selected_date = st.date_input("Date", value=date.today())
    with col2:
        boat_type = st.selectbox("Boat type", BOAT_TYPES)
    with col3:
        rating = st.selectbox("Sailor rating", SAILOR_RATINGS)

    api_key = st.text_input("Anthropic API key", type="password",
                             help="Needed to generate the notes section. "
                                  "Get one at console.anthropic.com")

    if st.button("Check conditions", type="primary"):
        with st.spinner("Fetching weather data..."):
            hourly = fetch_weather(selected_date.isoformat())
            conditions = summarize_midday_conditions(hourly)
            compass_dir = wind_direction_to_compass(conditions["winddirection_deg"])

        # --- Factors (raw data, no AI) ---
        st.subheader("📊 Factors")
        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Wind speed", f"{conditions['windspeed_mph']} mph")
        f2.metric("Gusts", f"{conditions['windgusts_mph']} mph")
        f3.metric("Wind direction", f"{compass_dir} ({conditions['winddirection_deg']}°)")
        f4.metric("Visibility", f"{conditions['visibility_m']} m")

        f5, f6, f7 = st.columns(3)
        f5.metric("Temperature", f"{conditions['temperature_c']}°C")
        f6.metric("UV index", conditions["uv_index"])
        f7.metric("Precip. chance", f"{conditions['precipitation_probability']}%")

        # --- Estimated flag + eligibility (rule-based) ---
        st.subheader("🚩 Estimated Flag & Eligibility")
        estimated_flag = estimate_flag(conditions["windspeed_mph"], conditions["weathercode"])
        eligibility = check_eligibility(estimated_flag, boat_type, rating)

        st.write(f"**Estimated flag:** {estimated_flag}")
        if eligibility["eligible"]:
            st.success(f"Likely eligible to sail — {eligibility['reason']}")
        else:
            st.error(f"Likely NOT eligible to sail — {eligibility['reason']}")

        st.caption(
            f"⚠️ This is a forecast-based estimate only. Always confirm the "
            f"live flag before sailing: {HOOFERS_LIVE_STATUS_URL}"
        )

        # --- AI-generated notes ---
        st.subheader("📝 Notes")
        if not api_key:
            st.info("Enter an Anthropic API key above to generate detailed notes.")
        else:
            with st.spinner("Generating notes..."):
                prompt = build_prompt(conditions, compass_dir, boat_type, rating,
                                       estimated_flag, eligibility)
                try:
                    notes = generate_ai_notes(prompt, api_key)
                    st.write(notes)
                except Exception as e:
                    st.error(f"Could not generate notes: {e}")


if __name__ == "__main__":
    main()
