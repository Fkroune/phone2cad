"""Plain-language warnings for scans that can look fine but be wrong (fast, no scan needed)."""

from scanfit_worker.pipeline import MIN_MAT_SHARE, UNCONFIRMED_SIDES, mat_view_warning


def test_few_mat_views_warn():
    # The scan that came out too small: the mat was in view in 35 of 114 photos (31 %).
    assert "35 of 114" in mat_view_warning(35, 114)
    assert mat_view_warning(25, 132, "Other side: ").startswith("Other side: Only 25 of 132")


def test_enough_mat_views_do_not_warn():
    assert mat_view_warning(88, 132) is None  # a scan that measured well
    assert mat_view_warning(int(MIN_MAT_SHARE * 100), 100) is None


def test_unconfirmed_sides_message_is_plain():
    assert "lined up" in UNCONFIRMED_SIDES and "film again" in UNCONFIRMED_SIDES
