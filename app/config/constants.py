"""Application-wide constants shared by several layers."""

# Countries a run can target: ISO code -> display name.
COUNTRIES = {"GB": "United Kingdom", "US": "United States", "IN": "India"}

# Buying-signal strengths, strongest first.
SIGNAL_STRENGTHS = ("very_high", "high", "medium", "low")

# Words in the user's ask that mean they WANT staffing / recruitment businesses as leads.
STAFFING_WORDS = ("recruitment", "recruiter", "staffing", "manpower", "placement", "headhunt")
# In an employer's NAME these mark a recruiter or job board posing as a direct employer (seen live on Reed:
# "ITOL Recruit", "eFinancialCareers"). Broader than STAFFING_WORDS, which reads the user's ask.
RECRUITER_NAME_WORDS = (*STAFFING_WORDS, "recruit", "resourcing", "personnel", "talent", "careers", "jobs")
