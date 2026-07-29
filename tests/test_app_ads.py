from app import app


def test_app_ads_txt_is_served_at_the_site_root():
    response = app.test_client().get("/app-ads.txt")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.get_data(as_text=True) == (
        "google.com, pub-6158185990205930, DIRECT, f08c47fec0942fa0\n"
    )
