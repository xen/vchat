from jobs.indexing.search_fields import entity_terms_to_text, normalize_uri_slug


def test_normalize_uri_slug_keeps_searchable_url_parts() -> None:
    assert (
        normalize_uri_slug(
            "https://Navigator.VBudushee.ru/direction/Профессии/?age=14-16#top"
        )
        == "navigator vbudushee ru direction профессии age 14 16"
    )


def test_entity_terms_to_text_skips_empty_terms() -> None:
    assert entity_terms_to_text([" python ", "", "BM25", "  "]) == "python BM25"
