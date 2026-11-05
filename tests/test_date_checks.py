import pytest

from bgv.date_checks import check_dates, is_full_time
from bgv.models import JobClaim, ParsedResume, Verdict
from bgv.utils import current_month


def job(employer="Infosys", start="2018-07", end="2021-03", title="Software Engineer", current=False, raw=""):
    return JobClaim(employer=employer, title=title, start=start, end=None if current else end,
                    is_current=current, raw_text=raw)


def verdicts(*jobs):
    return [(c.verdict, c.reasons[0]) for c in check_dates(ParsedResume(full_name="X", jobs=list(jobs)))]


def future(months_ahead=2):
    y, m = current_month()
    m += months_ahead
    return f"{y + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}"


def test_end_before_start_is_contradicted():
    (v, reason), = verdicts(job(employer="Nexora", start="2023-04", end="2021-01"))
    assert v == Verdict.CONTRADICTED and "2021-01 is before the start date 2023-04" in reason


def test_start_in_the_future_is_contradicted():
    (v, reason), = verdicts(job(start=future(), current=True))
    assert v == Verdict.CONTRADICTED and "in the future" in reason


def test_consistent_dates_are_verified_and_cite_the_resume():
    out = check_dates(ParsedResume(full_name="X", jobs=[job(raw="Software Engineer, Infosys, Jul 2018 - Mar 2021")]))
    assert out[0].verdict == Verdict.VERIFIED
    assert out[0].sources[0].kind == "resume" and "Infosys" in out[0].sources[0].ref


def test_full_time_overlap_over_3_months_is_discrepancy_on_both_jobs():
    a = job(employer="Infosys", start="2018-07", end="2021-03")
    b = job(employer="Wipro", start="2020-09", end="2022-01")          # 6 months overlap
    (va, ra), (vb, rb) = verdicts(a, b)
    assert va == vb == Verdict.DISCREPANCY
    assert "Overlaps by 6 months" in ra and "Wipro" in ra and "Infosys" in rb


def test_overlap_of_exactly_3_months_is_allowed():
    a = job(employer="Infosys", start="2018-07", end="2021-03")
    b = job(employer="Wipro", start="2020-12", end="2022-01")
    assert [v for v, _ in verdicts(a, b)] == [Verdict.VERIFIED, Verdict.VERIFIED]


def test_overlap_with_current_job_uses_today():
    a = job(employer="Wipro", start="2019-03", current=True)
    b = job(employer="TCS", start="2020-01", end="2021-01")
    assert [v for v, _ in verdicts(a, b)] == [Verdict.DISCREPANCY, Verdict.DISCREPANCY]


@pytest.mark.parametrize("title,raw", [("Intern", ""), ("Freelance Web Developer", ""),
                                       ("Developer", "Part-time, weekends"), ("Advisor", "")])
def test_non_full_time_roles_may_overlap(title, raw):
    a = job(employer="Infosys", start="2018-07", end="2021-03")
    b = job(employer="Side Co", start="2019-01", end="2020-12", title=title, raw=raw)
    assert not is_full_time(b)
    assert [v for v, _ in verdicts(a, b)] == [Verdict.VERIFIED, Verdict.VERIFIED]


def test_year_only_dates_never_cause_false_flags():
    # "2021" to "2021-03" could be Jan-Mar 2021, so it is not end-before-start
    assert verdicts(job(start="2021", end="2021-03"))[0][0] == Verdict.VERIFIED
    # 2019-2021 and 2021-2023 might only touch in 2021: no guaranteed overlap over 3 months
    assert [v for v, _ in verdicts(job(employer="A", start="2019", end="2021"),
                                   job(employer="B", start="2021", end="2023"))] == [Verdict.VERIFIED] * 2
    # but "2022" to "2021" is impossible whatever the months
    assert verdicts(job(start="2022", end="2021"))[0][0] == Verdict.CONTRADICTED


def test_missing_dates_are_cannot_verify():
    (v1, r1), (v2, r2) = verdicts(job(employer="A", start=None), job(employer="B", start="2016", end=None))
    assert v1 == v2 == Verdict.CANNOT_VERIFY
    assert "no start date" in r1 and "no end date" in r2


def test_contradicted_job_is_left_out_of_overlap_checks():
    bad = job(employer="Nexora", start="2023-04", end="2021-01")
    ok = job(employer="Infosys", start="2018-07", end="2023-03")
    assert [v for v, _ in verdicts(bad, ok)] == [Verdict.CONTRADICTED, Verdict.VERIFIED]
