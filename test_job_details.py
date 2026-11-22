"""Quick checks for job_details.py. Run: python test_job_details.py"""
from job_details import extract_details, extract_salary, normalise_arrangement

SALARIES = [
    # (text, (min, max, currency, period, annual_mid) or None)
    ("Actual compensation will depend on a candidate's education. The anticipated wage for this position is\n"
     "$72,000 - $189,200\nFull-time equivalent employees also will be eligible for a company bonus",
     (72000, 189200, "USD", "year", 130600)),                           # Lilly
    ("Salary\n$18.6 - $33.2", (18.6, 33.2, "USD", "hour", 53872)),      # AbbVie tile, hourly with no unit
    ("The base pay range for this role is $150K – $200K.", (150000, 200000, "USD", "year", 175000)),
    ("Pay: $25.50 per hour", (25.5, 25.5, "USD", "hour", 53040)),
    ("Hiring range: USD 95,000 to 120,000 annually", (95000, 120000, "USD", "year", 107500)),
    ("We are a $2.5 billion company with 5,000 employees.", None),       # revenue is not pay
    ("Eligible for a $5,000 sign-on bonus.", None),                      # too small for a salary
    ("No pay information here.", None),
]


def test_salary():
    for text, expected in SALARIES:
        s = extract_salary(text)
        got = (s["salary_min"], s["salary_max"], s["salary_currency"], s["salary_period"],
               s["salary_annual_mid"]) if s else None
        assert got == expected, (text, got)


def test_details():
    d = extract_details(
        "Principal Scientist - Cryo-EM Operations (Fixed Duration Employee, 12 Months). Must be able to work "
        "weekday afternoon/evening and weekend shifts. Up to 10% travel. This position is not eligible for visa "
        "sponsorship. Relocation assistance is available. Eligible for a company bonus and equity. "
        "ASCP certification preferred; Six Sigma Green Belt a plus.",
        title="Principal Scientist / Sr. Principal Scientist - Cryo-EM", employment_type="Full time")
    assert d["schedule"] == "full-time"
    assert d["employment_term"] == "fixed-term" and d["contract_months"] == 12
    assert d["shifts"] == "weekend"
    assert d["travel"] == "up to 10%"
    assert d["visa_sponsorship"] == "not offered"
    assert d["relocation"] == "offered"
    assert d["bonus"] == "yes" and d["equity"] == "yes"
    assert d["certifications"] == ["Six Sigma", "Lab (ASCP/MLS)"]

    d = extract_details("Join our 3rd shift team. This is a hybrid role based in Waco, TX.",
                        title="Manufacturing Technician I", employment_type="Regular")
    assert d["shifts"] == "3rd" and d["work_arrangement"] == "hybrid" and d["employment_term"] == "permanent"

    # real wording from Lilly and Pfizer postings
    d = extract_details("Qualified applicants must be authorized to work in the United States on a full-time basis. "
                        "Lilly will not provide support for or sponsor work authorization and visas for this role.")
    assert d["visa_sponsorship"] == "not offered"
    d = extract_details("Relocation assistance may be available based on business needs and/or eligibility. "
                        "Reimbursement of recruiting expenses for licensed physicians may constitute a reportable "
                        "transfer of value under the Sunshine Act.")
    assert d["relocation"] == "possible" and d["certifications"] == []
    assert extract_details("Must hold an active state medical license.")["certifications"] == [
        "Medical licence / board certified"]

    d = extract_details("Summer program.", title="R&D Intern - Data Science")
    assert d["employment_term"] == "internship"
    # the job board's own workplace label wins over the text
    assert extract_details("This is a hybrid team.", arrangement="ORA_REMOTE")["work_arrangement"] == "remote"


def test_arrangement_labels():
    for raw, expected in [("OnSite", "on-site"), ("ORA_ON_SITE", "on-site"), ("Site-Dedicated", "on-site"),
                          ("Hybrid", "hybrid"), ("remote", "remote"), ("ORA_HYBRID", "hybrid"), ("", "")]:
        assert normalise_arrangement(raw) == expected, (raw, normalise_arrangement(raw))


if __name__ == "__main__":
    test_salary()
    test_details()
    test_arrangement_labels()
    print("all job_details checks passed")
