"""
Lake Mendota Sailing Conditions Advisor
A Streamlit app that:
1. Fetches weather data from Open-Meteo (no API key needed)
2. Computes an ESTIMATED flag level from wind speed (Hoofers-style thresholds)
3. Checks eligibility based on boat type + sailor rating
4. Uses Claude API to generate concise, human-readable notes based on
   local sailing knowledge (e.g. south wind behavior at Hoofers)

IMPORTANT: The estimated flag is a forecast-based approximation only.
Actual flags are set by Hoofers staff and may differ. Always check the
live status before sailing: https://uwpd.wisc.edu/services/lake-rescue-safety/#conditions
"""

import requests
import streamlit as st
from datetime import date, time
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

WIND_UNITS = ["mph", "knots", "km/h"]
TEMP_UNITS = ["°C", "°F"]

# Visibility bands (in meters) -> qualitative label
# Thresholds loosely follow common aviation/marine visibility categories
VISIBILITY_BANDS = [
    (10000, "Excellent"),
    (4000, "Good"),
    (1000, "Moderate — reduced visibility"),
    (0, "Poor — fog/haze risk"),
]


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

def convert_wind_speed(mph: float, unit: str) -> float:
    """Convert a wind speed given in mph to the requested display unit."""
    if unit == "mph":
        return round(mph, 1)
    if unit == "knots":
        return round(mph * 0.868976, 1)
    if unit == "km/h":
        return round(mph * 1.60934, 1)
    raise ValueError(f"Unknown wind unit: {unit}")


def convert_temperature(celsius: float, unit: str) -> float:
    """Convert a temperature given in Celsius to the requested display unit."""
    if unit == "°C":
        return round(celsius, 1)
    if unit == "°F":
        return round(celsius * 9 / 5 + 32, 1)
    raise ValueError(f"Unknown temperature unit: {unit}")


def visibility_label(visibility_m: float) -> str:
    """Return a qualitative label for a visibility distance in meters."""
    for threshold, label in VISIBILITY_BANDS:
        if visibility_m >= threshold:
            return label
    return "Unknown"


# ---------------------------------------------------------------------------
# Step 1: Fetch weather data (Facts) — no AI involved
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800)  # cache for 30 minutes to avoid hammering the API
def fetch_weather(target_date: str) -> dict:
    """
    Fetch hourly weather data for Lake Mendota from Open-Meteo.
    Wind speed is always fetched in mph (matches Hoofers flag thresholds)
    and temperature in Celsius; display-unit conversion happens separately
    so the underlying flag/eligibility logic never has to worry about units.
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
        "windspeed_unit": "mph",
        "timezone": "America/Chicago",
    }
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()["hourly"]


def get_conditions_at_hour(hourly: dict, hour: int) -> dict:
    """
    Extract the weather snapshot for a specific hour of the day (0-23).
    Values stay in their base units (mph, Celsius) here; unit conversion
    for display happens in the UI layer.
    """
    return {
        "temperature_c": hourly["temperature_2m"][hour],
        "precipitation_probability": hourly["precipitation_probability"][hour],
        "weathercode": hourly["weathercode"][hour],
        "windspeed_mph": hourly["windspeed_10m"][hour],
        "windgusts_mph": hourly["windgusts_10m"][hour],
        "winddirection_deg": hourly["winddirection_10m"][hour],
        "visibility_m": hourly["visibility"][hour],
        "uv_index": hourly["uv_index"][hour],
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

    The prompt explicitly asks for SHORT, SELECTIVE bullet points —
    only flagging things that actually matter for this specific forecast —
    rather than restating every input value.
    """
    return f"""You are a sailing conditions assistant for Hoofers Sailing Club on Lake Mendota (Madison, WI).

Forecast snapshot for the selected time:
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

Rule-based assessment (already computed, do not recompute or restate in detail):
- Estimated flag: {estimated_flag}
- Eligibility: {"Eligible" if eligibility['eligible'] else "Not eligible"} — {eligibility['reason']}

Local knowledge (only mention if it actually applies to today's conditions):
- Hoofers sits on the south shore of Lake Mendota, near buildings. South
  wind (S/SSW/SSE) can mean near-dead air right at the shore (hard to
  sail back in) when light, or turbulent gusty air near shore when strong.
- Gusts 50%+ higher than sustained wind speed = meaningfully higher
  knockdown risk, worth flagging.
- High UV (7+) combined with sailing = worth a sun/hydration reminder.

Output format: 2-4 short bullet points, most important first. Each bullet
should be one sentence. ONLY include a bullet if it flags something the
sailor should actually pay attention to today — do NOT restate conditions
that are simply normal/fine, and do NOT explain background context (e.g.
don't describe what south wind normally does if today's wind isn't from
the south). If there is nothing noteworthy beyond the flag/eligibility
already shown, it's fine to return just one bullet saying conditions look
routine. Always end with one bullet reminding the sailor to confirm the
live flag at {HOOFERS_LIVE_STATUS_URL} before heading out.

Do not use a preamble like "Here's a quick heads-up" — start directly
with the bullets."""


def generate_ai_notes(prompt: str, api_key: str) -> str:
    """Call Claude API to generate the notes section."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
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
    col1, col2 = st.columns(2)
    with col1:
        selected_date = st.date_input("Date", value=date.today())
    with col2:
        selected_time = st.time_input("Time", value=time(12, 0))

    col3, col4 = st.columns(2)
    with col3:
        boat_type = st.selectbox("Boat type", BOAT_TYPES)
    with col4:
        rating = st.selectbox("Sailor rating", SAILOR_RATINGS)

    # Display-unit preferences (do not affect underlying flag/eligibility logic)
    col5, col6 = st.columns(2)
    with col5:
        wind_unit = st.selectbox("Wind speed unit", WIND_UNITS)
    with col6:
        temp_unit = st.selectbox("Temperature unit", TEMP_UNITS)

    api_key = st.text_input("Anthropic API key", type="password",
                             help="Needed to generate the notes section. "
                                  "Get one at console.anthropic.com")

    if st.button("Check conditions", type="primary"):
        with st.spinner("Fetching weather data..."):
            hourly = fetch_weather(selected_date.isoformat())
            conditions = get_conditions_at_hour(hourly, selected_time.hour)
            compass_dir = wind_direction_to_compass(conditions["winddirection_deg"])

        # --- Factors (raw data, no AI) ---
        st.subheader("📊 Factors")
        st.caption(f"Snapshot for {selected_date.isoformat()} at {selected_time.strftime('%H:%M')}")

        display_wind = convert_wind_speed(conditions["windspeed_mph"], wind_unit)
        display_gusts = convert_wind_speed(conditions["windgusts_mph"], wind_unit)
        display_temp = convert_temperature(conditions["temperature_c"], temp_unit)

        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Wind speed", f"{display_wind} {wind_unit}")
        f2.metric("Gusts", f"{display_gusts} {wind_unit}")
        f3.metric("Wind direction", compass_dir)
        f3.caption(f"{conditions['winddirection_deg']:.0f}° on the compass")
        f4.metric("Visibility", f"{conditions['visibility_m'] / 1000:.1f} km")
        f4.caption(visibility_label(conditions["visibility_m"]))

        f5, f6, f7 = st.columns(3)
        f5.metric("Temperature", f"{display_temp} {temp_unit}")
        f6.metric("UV index", conditions["uv_index"])
        f7.metric("Rain chance", f"{conditions['precipitation_probability']}%",
                  help="Precipitation probability: the forecast likelihood "
                       "of measurable rain during this hour.")

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
                    st.markdown(notes)
                except Exception as e:
                    st.error(f"Could not generate notes: {e}")


if __name__ == "__main__":
    main()
