"""Quick checks for classify.py rules. Run: python -m pytest test_classify.py  (or python test_classify.py)"""
from classify import classify_education, classify_level, infer_missing_states, min_years_experience, parse_location

LOCATIONS = [
    ("Cambridge, MA", True, ["MA"]),
    ("Boston, MA 02110", True, ["MA"]),
    ("USA - NY - New York", True, ["NY"]),
    ("United States - Pennsylvania - Collegeville", True, ["PA"]),
    ("Holly Springs, North Carolina, United States of America", True, ["NC"]),
    ("Albuquerque, New Mexico", True, ["NM"]),
    ("Washington, D.C.", True, ["DC"]),
    ("Remote - US", True, []),
    ("US MA Remote", True, ["MA"]),
    ("Romania - Bucharest", False, []),
    ("CHN - JS - Jiangsu", False, []),
    ("Toronto, ON", False, []),
    ("Bangalore, IN, India", False, []),
    ("Mexico City, Mexico", False, []),
    ("Remote", None, []),
    ("3 Locations", None, []),
]

EDUCATION = [
    ("Bachelor's degree in Biology required; Master's preferred.", ["Bachelor's", "Master's"], "Bachelor's"),
    ("PhD in Chemistry with 0-2 years of experience, or MS with 5+ years.", ["Master's", "PhD"], "Master's"),
    ("Proficiency in MS Office. Based in Boston, MA.", [], "Not stated"),
    ("Requires a BS/MS in Engineering.", ["Bachelor's", "Master's"], "Bachelor's"),
    ("DVM or equivalent veterinary degree required.", ["Professional doctorate"], "Professional doctorate"),
    ("High school diploma or GED required.", ["High school/GED"], "High school/GED"),
    ("Experience with master data management.", [], "Not stated"),
]

LEVELS = [
    ("Associate Director, Clinical Operations", "senior"),
    ("Senior Associate Scientist", "mid"),
    ("Scientist I, Protein Engineering", "early"),
    ("Scientist II", "mid"),
    ("Scientist 3, Animal Genetic Technologies", "mid"),
    ("Manufacturing Chemist 1", "early"),
    ("Maintenance Technician 2", "mid"),
    ("Principal Scientist", "senior"),
    ("Vice President, Regulatory Affairs", "executive"),
    ("Head of Data Science", "executive"),
    ("Summer Intern - Bioinformatics", "early"),
    ("Postdoctoral Fellow, Immunology", "early"),
    ("Research Associate", "early"),
    ("Manager, Quality Assurance", "mid"),
    ("Sales Representative", "unspecified"),
    ("System Administrator 3", "mid"),
    ("Life Science Research Professional 2", "mid"),
    ("SCI Clinical Trials Office Research Administrator 3 (Hybrid)", "mid"),
    ("Phase 1 Unit Nurse", "unspecified"),
    ("Assistant Professor of Animal Science", "early"),
    ("Research Assistant Professor, Bioinformatics", "early"),
    ("Associate Professor - Plant Pathology", "mid"),
    ("Professor and Department Chair, Chemistry", "executive"),
    ("Professor of Veterinary Pathology", "senior"),
    ("Associate Dean for Research", "executive"),
]


def test_locations():
    for text, is_us, states in LOCATIONS:
        info = parse_location(text)
        assert info.is_us is is_us, (text, info)
        assert info.states == states, (text, info)


def test_education():
    for text, mentioned, lowest in EDUCATION:
        assert classify_education(text) == (mentioned, lowest), (text, classify_education(text))


def test_levels():
    for title, level in LEVELS:
        assert classify_level(title)[0] == level, (title, classify_level(title))
    # title beats a coarse ATS label
    assert classify_level("Executive Director, Strategy", ats_label="Manager with direct reports")[0] == "executive"
    assert classify_level("Data Analyst", ats_label="Entry Level") == ("early", "ats")


def test_infer_states():
    from types import SimpleNamespace as J
    jobs = [J(is_us=True, states=["NY"], city="Tarrytown", description=""),
            J(is_us=True, states=[], city="TARRYTOWN", description=""),
            J(is_us=True, states=[], city="Warren", description="Based at our campus in Warren, NJ."),
            J(is_us=True, states=[], city="Lincoln", description="Lincoln or Omaha"),  # 'or' is not Oregon
            J(is_us=False, states=[], city="Basel", description="Basel, CH")]
    assert infer_missing_states(jobs) == 2
    assert jobs[1].states == ["NY"] and jobs[2].states == ["NJ"] and jobs[3].states == []


def test_years():
    assert min_years_experience("BS with 8+ years of relevant industry experience or PhD with 3 years experience") == 3
    assert min_years_experience("5-7 years of experience in GMP manufacturing") == 5
    assert min_years_experience("Founded 25 years ago") is None


if __name__ == "__main__":
    for fn in (test_locations, test_education, test_levels, test_infer_states, test_years):
        fn()
    print("all classify checks passed")
