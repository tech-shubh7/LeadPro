"""Application-wide constants shared by several layers."""

# Countries a run can target: ISO code -> display name.
# Not an ISO code: "WW" marks a worldwide run (no registry, no country pack, outreach rules unverified).
WORLDWIDE = "WW"
COUNTRIES = {"GB": "United Kingdom", "US": "United States", "IN": "India", WORLDWIDE: "Worldwide"}

# Buying-signal strengths, strongest first.
SIGNAL_STRENGTHS = ("very_high", "high", "medium", "low")

# Words in the user's ask that mean they WANT staffing / recruitment businesses as leads.
STAFFING_WORDS = ("recruitment", "recruiter", "staffing", "manpower", "placement", "headhunt")
# In an employer's NAME these mark a recruiter or job board posing as a direct employer (seen live on Reed:
# "ITOL Recruit", "eFinancialCareers"). Broader than STAFFING_WORDS, which reads the user's ask.
RECRUITER_NAME_WORDS = (*STAFFING_WORDS, "recruit", "resourcing", "personnel", "talent", "careers", "jobs")

# A candidate rejected before any model call: none of its sources has a code-trusted date inside the window
# (run_service.process_candidate). The prefix is how reports count them.
NO_FRESH_SOURCE_PREFIX = "no source dated within"
NO_FRESH_SOURCE_REASON = NO_FRESH_SOURCE_PREFIX + " {days} days (checked before any model call)"
