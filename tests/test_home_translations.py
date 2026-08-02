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
        "concept.kicker",
        "concept.title",
        "concept.desc",
        "concept.badge",
        "concept.dashboardTitle",
        "concept.dashboardDesc",
        "concept.dashboardAlt",
        "concept.mealTitle",
        "concept.mealDesc",
        "concept.mealAlt",
        "concept.progressTitle",
        "concept.progressDesc",
        "concept.progressAlt",
    }

    for locale in ("fr", "pt", "it", "es", "de", "ro", "la"):
        matches = re.findall(
            rf"Object\.assign\(translations\.{locale},\{{(.*?)\}}\);",
            source,
            re.DOTALL,
        )
        assert matches, f"missing complete translation extension for {locale}"
        keys = {
            key
            for translation_block in matches
            for key in re.findall(r'"([^"]+)"\s*:', translation_block)
        }
        assert required <= keys, f"missing {sorted(required - keys)} for {locale}"
