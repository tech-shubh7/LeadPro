"""Application-wide constants shared by several layers."""

# Countries a run can target: ISO code -> display name.
COUNTRIES = {"GB": "United Kingdom", "US": "United States", "IN": "India"}

# Buying-signal strengths, strongest first.
SIGNAL_STRENGTHS = ("very_high", "high", "medium", "low")

# Words in a company name or ad that mark a recruitment / staffing business (a recruiter, not a buyer).
STAFFING_WORDS = ("recruitment", "recruiter", "staffing", "manpower", "placement", "headhunt")
