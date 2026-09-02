import pytest
from actions import execute_action, APP_ALIASES, close_macos_app, open_macos_app
from server import extract_action, detect_action_fast

def test_extract_action_tags():
    # Test OPEN_APP
    clean, act = extract_action("Right away, sir. [ACTION:OPEN_APP] Spotify")
    assert clean == "Right away, sir."
    assert act == {"action": "open_app", "target": "Spotify"}

    # Test CLOSE_APP
    clean, act = extract_action("Closing that down, sir. [ACTION:CLOSE_APP] WhatsApp")
    assert clean == "Closing that down, sir."
    assert act == {"action": "close_app", "target": "WhatsApp"}

    # Test SPOTIFY
    clean, act = extract_action("Playing now. [ACTION:SPOTIFY] play ||| JARVIS Court song")
    assert clean == "Playing now."
    assert act == {"action": "spotify", "target": "play ||| JARVIS Court song"}

    # Test WHATSAPP
    clean, act = extract_action("Opening chat. [ACTION:WHATSAPP] Alex ||| Hello there")
    assert clean == "Opening chat."
    assert act == {"action": "whatsapp", "target": "Alex ||| Hello there"}

    # Test OPEN_FOLDER
    clean, act = extract_action("Opening in Finder. [ACTION:OPEN_FOLDER] FH-Connect")
    assert clean == "Opening in Finder."
    assert act == {"action": "open_folder", "target": "FH-Connect"}

    # Test FIRECRAWL
    clean, act = extract_action("Scraping page now. [ACTION:FIRECRAWL] https://news.ycombinator.com")
    assert clean == "Scraping page now."
    assert act == {"action": "firecrawl", "target": "https://news.ycombinator.com"}

def test_detect_action_fast_close_vs_open():
    # 1. Close WhatsApp vs Open WhatsApp vs bare WhatsApp
    act_close = detect_action_fast("close WhatsApp")
    assert act_close is not None
    assert act_close["action"] == "close_app"
    assert act_close["target"] == "WhatsApp"

    act_open = detect_action_fast("open WhatsApp")
    assert act_open is not None
    assert act_open["action"] == "whatsapp"

    # Bare word "WhatsApp" should NOT auto-open - falls through to LLM for conversational intent
    act_bare = detect_action_fast("WhatsApp")
    assert act_bare is None

    # 2. Close Spotify vs Open Spotify vs Quit Spotify
    act_quit_spotify = detect_action_fast("quit spotify")
    assert act_quit_spotify is not None
    assert act_quit_spotify["action"] == "close_app"
    assert act_quit_spotify["target"] == "Spotify"

    act_close_spotify = detect_action_fast("close the Spotify app")
    assert act_close_spotify is not None
    assert act_close_spotify["action"] == "close_app"
    assert act_close_spotify["target"] == "Spotify"

    # 3. Multi-word app aliases ("anti gravity" / "antigravity")
    act_open_ag = detect_action_fast("open anti gravity")
    assert act_open_ag is not None
    assert act_open_ag["action"] == "open_app"
    assert act_open_ag["target"] == "Antigravity"

    act_close_ag = detect_action_fast("close antigravity")
    assert act_close_ag is not None
    assert act_close_ag["action"] == "close_app"
    assert act_close_ag["target"] == "Antigravity"

    act_kill_chrome = detect_action_fast("kill chrome")
    assert act_kill_chrome is not None
    assert act_kill_chrome["action"] == "close_app"
    assert act_kill_chrome["target"] == "Google Chrome"

def test_detect_action_fast_spotify():
    act = detect_action_fast("open spotify and play some music")
    assert act is not None
    assert act["action"] == "spotify"

    act = detect_action_fast("pause music")
    assert act is not None
    assert act["action"] == "spotify"
    assert act["target"] == "pause"

    act = detect_action_fast("stop the song")
    assert act is not None
    assert act["action"] == "spotify"
    assert act["target"] == "pause"

    act = detect_action_fast("hey jarvis stop the spotify")
    assert act is not None
    assert act["action"] == "spotify"
    assert act["target"] == "pause"

    act = detect_action_fast("resume the song")
    assert act is not None
    assert act["action"] == "spotify"
    assert act["target"] == "resume"

    act = detect_action_fast("next song")
    assert act is not None
    assert act["action"] == "spotify"
    assert act["target"] == "next"

def test_detect_action_fast_browser_research():
    act = detect_action_fast("now open Safari and do some research about new launched AI models")
    assert act is not None
    assert act["action"] == "browse"
    assert "safari" in act["target"].lower()
    assert "new launched ai models" in act["target"].lower()

    act = detect_action_fast("search about quantum computing")
    assert act is not None
    assert act["action"] == "browse"
    assert act["target"] == "quantum computing"

def test_detect_action_fast_folders():
    act = detect_action_fast("open the FH-Connect folder")
    assert act is not None
    assert act["action"] == "open_folder"
    assert "fh-connect" in act["target"].lower()

def test_execute_action_routing():
    # Test app aliases mapping
    assert APP_ALIASES["spotify"] == "Spotify"
    assert APP_ALIASES["whatsapp"] == "WhatsApp"
    assert APP_ALIASES["chrome"] == "Google Chrome"
    assert APP_ALIASES["safari"] == "Safari"
    assert APP_ALIASES["antigravity"] == "Antigravity"
    assert APP_ALIASES["email"] == "Mail"


def test_detect_action_fast_advanced_features():
    # Multi-app quit
    act = detect_action_fast("quit the email notes and calendar")
    assert act is not None
    assert act["action"] == "close_app"

    # Search on browser
    act = detect_action_fast("search Google meet on brave")
    assert act is not None
    assert act["action"] == "browse"
    assert "google meet in brave" in act["target"].lower()

    # Open web service directly
    act = detect_action_fast("open Google meet on browser")
    assert act is not None
    assert act["action"] == "browse"
    assert "google meet" in act["target"].lower()

    # Calendar scheduling
    act = detect_action_fast("I wanted to schedule a meeting for me on the Google meet at 3:00 p.m. today")
    assert act is not None
    assert act["action"] == "schedule"
    assert "3:00" in act["target"]


def test_strip_markdown_and_actions_for_tts():
    from server import strip_markdown_for_tts
    res = strip_markdown_for_tts("Your schedule is clear today, sir. [ACTION:CALENDAR]")
    assert res == "Your schedule is clear today, sir."
    assert "[ACTION:" not in res

    res2 = strip_markdown_for_tts("Opening Brave now, sir. [ACTION:OPEN_APP] Brave Browser")
    assert res2 == "Opening Brave now, sir."


def test_research_recall_and_sanitization():
    # Research recall fast action
    act_where = detect_action_fast("where is my research")
    assert act_where is not None
    assert act_where["action"] == "show_research"

    act_show = detect_action_fast("show my research")
    assert act_show is not None
    assert act_show["action"] == "show_research"

    # Action extraction sanitization (ignoring dummy targets & fake social URLs)
    clean, act = extract_action("Here is the plan, sir. [ACTION:BROWSE] https://instagram.com/tonystark")
    assert act is None
    assert clean == "Here is the plan, sir."

    clean2, act2 = extract_action("Ready, sir. [ACTION:SCHEDULE] schedule")
    assert act2 is None
    assert clean2 == "Ready, sir."


@pytest.mark.asyncio
async def test_spotify_control_confirmation():
    from actions import control_spotify
    # "play" command with empty query or generic music query should say "Playing music on Spotify, sir."
    res1 = await control_spotify("play", "")
    assert "Playing music on Spotify" in res1["confirmation"]
    assert "Playing play on Spotify" not in res1["confirmation"]

    res2 = await control_spotify("play", "some music")
    assert "Playing music on Spotify" in res2["confirmation"]
    assert "Playing some music on Spotify" not in res2["confirmation"]


def test_deep_research_detection():
    cases = [
        ("so today I am giving you a task you need to research about Abrar akunji", "abrar akunji"),
        ("I want to research about new large language model reasoning capability", "new large language model reasoning capability"),
        ("research about quantum computing", "quantum computing"),
        ("please do some research on autonomous agents", "autonomous agents"),
        ("give me all details about Apple M4 chip", "apple m4 chip"),
    ]
    for text, expected_topic in cases:
        act = detect_action_fast(text)
        assert act is not None, f"Failed to detect research for: '{text}'"
        assert act["action"] == "research", f"Expected action 'research' for '{text}', got {act}"
        assert expected_topic.lower() in act["target"].lower(), f"Expected topic '{expected_topic}' in '{act['target']}'"


def test_maps_and_trip_planning_detection():
    # Trip planning and maps
    act1 = detect_action_fast("open maps and plan my trip to Ahmedabad")
    assert act1 is not None
    assert act1["action"] == "maps"
    assert "ahmedabad" in act1["target"].lower()

    act2 = detect_action_fast("open that in Google map on brave")
    assert act2 is not None
    assert act2["action"] == "maps"
    assert "brave" in act2["target"].lower()

    act3 = detect_action_fast("plan my trip to Mumbai")
    assert act3 is not None
    assert act3["action"] == "maps"
    assert "mumbai" in act3["target"].lower()


def test_live_weather_detection():
    weather_cases = [
        "what is the weather",
        "how is the weather outside",
        "what's the weather in my current location",
        "is it raining or sunny",
        "do I need to carry my umbrella today",
        "exact weather forecast",
    ]
    for q in weather_cases:
        act = detect_action_fast(q)
        assert act is not None, f"Failed to detect weather for '{q}'"
        assert act["action"] == "weather"


def test_google_intent_detection():
    for q in ["can you use google", "use google", "open google", "search on google", "google it"]:
        act = detect_action_fast(q)
        assert act is not None, f"Failed to detect google action for '{q}'"
        assert act["action"] == "browse"
        assert "google.com" in act["target"]


@pytest.mark.asyncio
async def test_spotify_nlp_cleaning():
    from actions import control_spotify
    res = await control_spotify("play", "something creative music that I can listen and work with")
    assert res is not None
    assert res["success"] is True
    assert "creative music" in res["confirmation"] or "Playing" in res["confirmation"]





