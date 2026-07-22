"""
config.example.py — template for config.py.

Copy this to config.py (which is gitignored) and fill in your real values. The pipeline
code imports everything property-identifying from here, so no account numbers, addresses,
or sheet names ever live in the committed source.

    cp config.example.py config.py   # then edit config.py
"""

# --- Google auth + Drive inbox (paths relative to the repo root) ---
# Service-account JSON with access to your Sheets + the Drive "inbox" folder.
CREDS_FILE = "private/credentials.json"
DRIVE_CONFIG = "private/drive_config.json"
LOCAL_DATA_DIR = "private/data"
OWNER_EMAIL = "you@example.com"   # Drive inbox is shared to this address

# --- Property alias -> destination Google Sheet name ---
# Aliases are how you refer to a property on the CLI ("-p lakehouse"); the value is the
# exact Google Sheet title the entries are written to. STR = short-term rental, LTR = long-term.
PROPERTY_SHEETS = {
    "lakehouse": "2026 Lakehouse P&L",     # STR
    "lh": "2026 Lakehouse P&L",
    "cabin": "2026 Cabin P&L",             # STR
    "downtown": "2026 Downtown Unit P&L",  # LTR (professionally managed)
    "unit 4b": "2026 Downtown Unit P&L",
}
NOT_YET = {}                               # properties known but not yet wired up
PROPERTY_HELP = "lakehouse, cabin, or downtown"

# --- All destination sheets (promote iterates these) ---
SHEETS = ["2026 Lakehouse P&L", "2026 Cabin P&L", "2026 Downtown Unit P&L"]

# --- Bank account # -> (sheet, label). Routing is by the #number in the CSV filename. ---
ACCOUNT_SHEETS = {
    "1234": ("2026 Lakehouse P&L", "Lakehouse / STR"),
    "5678": ("2026 Cabin P&L", "Cabin / STR"),
    "9012": ("2026 Downtown Unit P&L", "Downtown Unit / LTR"),
}

# --- Bank-feed payee classification (lowercased substring match) ---
MORTGAGE_PAYEES = ("your credit union", "your bank", "mortgage", "loan servicing")
TRANSFER_PAYEES = ("total checking", "owner draw", "transfer to", "to checking", "xfer",
                   "your property manager")

# --- Mortgage statement routing: (servicer keywords, approx total payment, sheet, label, display).
# When one servicer holds loans on multiple properties, the payment AMOUNT disambiguates. ---
MORTGAGE_PROFILES = [
    (("your credit union", "yourcu.org"), 2500.00, "2026 Cabin P&L", "Cabin / STR", "Your CU"),
    (("your credit union", "yourcu.org"), 950.00, "2026 Downtown Unit P&L", "Downtown / LTR", "Your CU"),
    (("your bank",), 1600.00, "2026 Lakehouse P&L", "Lakehouse / STR", "Your Bank"),
]

# --- PM (AppFolio) owner-statement config, for a professionally-managed LTR ---
APPFOLIO_PROPERTY_MATCH = [
    ("downtown", "2026 Downtown Unit P&L", "Downtown Unit / LTR"),
]
APPFOLIO_ADDRESS_RE = r"123\s+Main\s+Street"   # splits party vs description on the statement
APPFOLIO_PM_NAME = "Your PM Co"                 # default payee prefix
APPFOLIO_BOILERPLATE_RE = r"Your PM Co LLC\..*?Terms of Service"

# --- Vendor keyword -> utility sub-line (your specific local utility vendors) ---
UTILITY_VENDOR_LINES = {
    "city water": "Water",
    "power co": "Electricity",
}

# --- inbox_status.py display ---
INBOX_PROPS = {
    "2026 Lakehouse P&L": "Lakehouse / STR (#1234)",
    "2026 Cabin P&L": "Cabin / STR (#5678)",
    "2026 Downtown Unit P&L": "Downtown Unit / LTR (#9012)",
}
INBOX_APPFOLIO_SHEET = "2026 Downtown Unit P&L"
