import re


def test_dashboard_and_play_copy_exists_for_every_supported_language():
    source = open("templates/index.html", encoding="utf-8").read()
    required = {
        "play.eyebrow",
        "play.continue",
        "dash.welcome",
        "dash.logout",
        "dash.calories",
        "dash.protein",
        "dash.carbs",
        "dash.points",
        "dash.dailyGoal",
        "dash.ofGoal",
        "dash.water",
        "dash.week",
        "dash.recent",
        "dash.empty",
        "dash.meal",
    }

    for locale in ("fr", "pt", "it", "es", "de", "ro", "la"):
        match = re.search(
            rf"Object\.assign\(translations\.{locale},\{{(.*?)\}}\);",
            source,
            re.DOTALL,
        )
        assert match, f"missing complete translation extension for {locale}"
        keys = set(re.findall(r'"([^"]+)"\s*:', match.group(1)))
        assert required <= keys, f"missing {sorted(required - keys)} for {locale}"
