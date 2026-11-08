"""
One region vocabulary, and the map from every country spelling the providers use into it.

**Why it exists.** The Allocation tab's Regions chart used to mix three vocabularies on one pie:
Yahoo's countries for directly-held stocks (`United States`), `etf_mappings.py`'s hand-written
regions for funds (`North America`, `Emerging Markets`), and a client-side fold in
`AllocationTab.tsx` that mapped some countries to regions and left the rest as `Other`. So the
same money could sit in "United States" and in "North America" side by side. Since 2026-11-08 the
chart is a rollup of the look-through's per-company decomposition (docs/lookthrough.md,
*Allocation is a rollup of this decomposition*), and this module is the one place a country
becomes a region.

**The regions are geographic, not a market classification.** `Emerging Markets` was a bucket
here; it is an index provider's opinion (MSCI and FTSE disagree about Korea, and both reclassify),
so it drifts, and it overlaps the geographic buckets it sat beside. The five regions follow the
UN M49 geoscheme with four overrides, each the usual investment reading of a country M49 files
elsewhere: Cyprus and Türkiye are Europe (M49: Western Asia), Iran is the Middle East (M49:
Southern Asia), and Puerto Rico and the US Virgin Islands are North America (M49: Caribbean —
they are US territories whose companies report as US issuers).

**Two inputs, two strengths.** `region_of()` reads a *stated* country: an issuer basket's
country of risk (iShares, DWS) or Yahoo's country for a held stock. `region_of_isin()` reads an
ISIN's two-letter prefix — the country the security was *registered* in, which is the issuer's
home for most companies and says nothing at all for a company incorporated offshore. So the
prefix is a fallback only (the look-through asks it when no stated country exists for a
company), and the incorporation havens in `_OFFSHORE` answer `UNKNOWN` rather than send a
Chinese ADR to the Caribbean.

Never raises and never returns an unseen name: a spelling nobody has mapped becomes `UNKNOWN`,
and `unmapped_countries()` is how those get found — the same contract as `sector_taxonomy`.
Adding a provider means adding its spellings here, never a second map elsewhere.
"""
import re
import unicodedata
from typing import Dict, Iterable, Optional

UNKNOWN = "Unknown"

NORTH_AMERICA = "North America"
EUROPE = "Europe"
ASIA_PACIFIC = "Asia Pacific"
LATIN_AMERICA = "Latin America"
MIDDLE_EAST_AFRICA = "Middle East & Africa"

#: Every region, in the order the frontend's `REGION_ORDER` (lib/sectorColors.ts) mirrors.
REGIONS = (NORTH_AMERICA, EUROPE, ASIA_PACIFIC, LATIN_AMERICA, MIDDLE_EAST_AFRICA)

_NA, _EU, _AP, _LA, _ME = REGIONS

#: ISO 3166-1: alpha-2 -> (alpha-3, English short name, region). Complete but for the five
#: uninhabited territories (AQ, BV, GS, HM, TF) and IO, which no listed company calls home;
#: XK (Kosovo) is user-assigned but used by every data vendor.
_COUNTRIES: Dict[str, tuple] = {
    "AD": ("AND", "Andorra", _EU), "AE": ("ARE", "United Arab Emirates", _ME),
    "AF": ("AFG", "Afghanistan", _AP), "AG": ("ATG", "Antigua and Barbuda", _LA),
    "AI": ("AIA", "Anguilla", _LA), "AL": ("ALB", "Albania", _EU),
    "AM": ("ARM", "Armenia", _ME), "AO": ("AGO", "Angola", _ME),
    "AR": ("ARG", "Argentina", _LA), "AS": ("ASM", "American Samoa", _AP),
    "AT": ("AUT", "Austria", _EU), "AU": ("AUS", "Australia", _AP),
    "AW": ("ABW", "Aruba", _LA), "AX": ("ALA", "Aland Islands", _EU),
    "AZ": ("AZE", "Azerbaijan", _ME), "BA": ("BIH", "Bosnia and Herzegovina", _EU),
    "BB": ("BRB", "Barbados", _LA), "BD": ("BGD", "Bangladesh", _AP),
    "BE": ("BEL", "Belgium", _EU), "BF": ("BFA", "Burkina Faso", _ME),
    "BG": ("BGR", "Bulgaria", _EU), "BH": ("BHR", "Bahrain", _ME),
    "BI": ("BDI", "Burundi", _ME), "BJ": ("BEN", "Benin", _ME),
    "BL": ("BLM", "Saint Barthelemy", _LA), "BM": ("BMU", "Bermuda", _NA),
    "BN": ("BRN", "Brunei Darussalam", _AP), "BO": ("BOL", "Bolivia", _LA),
    "BQ": ("BES", "Bonaire, Sint Eustatius and Saba", _LA), "BR": ("BRA", "Brazil", _LA),
    "BS": ("BHS", "Bahamas", _LA), "BT": ("BTN", "Bhutan", _AP),
    "BW": ("BWA", "Botswana", _ME), "BY": ("BLR", "Belarus", _EU),
    "BZ": ("BLZ", "Belize", _LA), "CA": ("CAN", "Canada", _NA),
    "CC": ("CCK", "Cocos (Keeling) Islands", _AP),
    "CD": ("COD", "Democratic Republic of the Congo", _ME),
    "CF": ("CAF", "Central African Republic", _ME), "CG": ("COG", "Congo", _ME),
    "CH": ("CHE", "Switzerland", _EU), "CI": ("CIV", "Cote d'Ivoire", _ME),
    "CK": ("COK", "Cook Islands", _AP), "CL": ("CHL", "Chile", _LA),
    "CM": ("CMR", "Cameroon", _ME), "CN": ("CHN", "China", _AP),
    "CO": ("COL", "Colombia", _LA), "CR": ("CRI", "Costa Rica", _LA),
    "CU": ("CUB", "Cuba", _LA), "CV": ("CPV", "Cabo Verde", _ME),
    "CW": ("CUW", "Curacao", _LA), "CX": ("CXR", "Christmas Island", _AP),
    "CY": ("CYP", "Cyprus", _EU), "CZ": ("CZE", "Czechia", _EU),
    "DE": ("DEU", "Germany", _EU), "DJ": ("DJI", "Djibouti", _ME),
    "DK": ("DNK", "Denmark", _EU), "DM": ("DMA", "Dominica", _LA),
    "DO": ("DOM", "Dominican Republic", _LA), "DZ": ("DZA", "Algeria", _ME),
    "EC": ("ECU", "Ecuador", _LA), "EE": ("EST", "Estonia", _EU),
    "EG": ("EGY", "Egypt", _ME), "EH": ("ESH", "Western Sahara", _ME),
    "ER": ("ERI", "Eritrea", _ME), "ES": ("ESP", "Spain", _EU),
    "ET": ("ETH", "Ethiopia", _ME), "FI": ("FIN", "Finland", _EU),
    "FJ": ("FJI", "Fiji", _AP), "FK": ("FLK", "Falkland Islands", _LA),
    "FM": ("FSM", "Micronesia", _AP), "FO": ("FRO", "Faroe Islands", _EU),
    "FR": ("FRA", "France", _EU), "GA": ("GAB", "Gabon", _ME),
    "GB": ("GBR", "United Kingdom", _EU), "GD": ("GRD", "Grenada", _LA),
    "GE": ("GEO", "Georgia", _ME), "GF": ("GUF", "French Guiana", _LA),
    "GG": ("GGY", "Guernsey", _EU), "GH": ("GHA", "Ghana", _ME),
    "GI": ("GIB", "Gibraltar", _EU), "GL": ("GRL", "Greenland", _NA),
    "GM": ("GMB", "Gambia", _ME), "GN": ("GIN", "Guinea", _ME),
    "GP": ("GLP", "Guadeloupe", _LA), "GQ": ("GNQ", "Equatorial Guinea", _ME),
    "GR": ("GRC", "Greece", _EU), "GT": ("GTM", "Guatemala", _LA),
    "GU": ("GUM", "Guam", _AP), "GW": ("GNB", "Guinea-Bissau", _ME),
    "GY": ("GUY", "Guyana", _LA), "HK": ("HKG", "Hong Kong", _AP),
    "HN": ("HND", "Honduras", _LA), "HR": ("HRV", "Croatia", _EU),
    "HT": ("HTI", "Haiti", _LA), "HU": ("HUN", "Hungary", _EU),
    "ID": ("IDN", "Indonesia", _AP), "IE": ("IRL", "Ireland", _EU),
    "IL": ("ISR", "Israel", _ME), "IM": ("IMN", "Isle of Man", _EU),
    "IN": ("IND", "India", _AP), "IQ": ("IRQ", "Iraq", _ME),
    "IR": ("IRN", "Iran", _ME), "IS": ("ISL", "Iceland", _EU),
    "IT": ("ITA", "Italy", _EU), "JE": ("JEY", "Jersey", _EU),
    "JM": ("JAM", "Jamaica", _LA), "JO": ("JOR", "Jordan", _ME),
    "JP": ("JPN", "Japan", _AP), "KE": ("KEN", "Kenya", _ME),
    "KG": ("KGZ", "Kyrgyzstan", _AP), "KH": ("KHM", "Cambodia", _AP),
    "KI": ("KIR", "Kiribati", _AP), "KM": ("COM", "Comoros", _ME),
    "KN": ("KNA", "Saint Kitts and Nevis", _LA), "KP": ("PRK", "North Korea", _AP),
    "KR": ("KOR", "South Korea", _AP), "KW": ("KWT", "Kuwait", _ME),
    "KY": ("CYM", "Cayman Islands", _LA), "KZ": ("KAZ", "Kazakhstan", _AP),
    "LA": ("LAO", "Laos", _AP), "LB": ("LBN", "Lebanon", _ME),
    "LC": ("LCA", "Saint Lucia", _LA), "LI": ("LIE", "Liechtenstein", _EU),
    "LK": ("LKA", "Sri Lanka", _AP), "LR": ("LBR", "Liberia", _ME),
    "LS": ("LSO", "Lesotho", _ME), "LT": ("LTU", "Lithuania", _EU),
    "LU": ("LUX", "Luxembourg", _EU), "LV": ("LVA", "Latvia", _EU),
    "LY": ("LBY", "Libya", _ME), "MA": ("MAR", "Morocco", _ME),
    "MC": ("MCO", "Monaco", _EU), "MD": ("MDA", "Moldova", _EU),
    "ME": ("MNE", "Montenegro", _EU), "MF": ("MAF", "Saint Martin", _LA),
    "MG": ("MDG", "Madagascar", _ME), "MH": ("MHL", "Marshall Islands", _AP),
    "MK": ("MKD", "North Macedonia", _EU), "ML": ("MLI", "Mali", _ME),
    "MM": ("MMR", "Myanmar", _AP), "MN": ("MNG", "Mongolia", _AP),
    "MO": ("MAC", "Macao", _AP), "MP": ("MNP", "Northern Mariana Islands", _AP),
    "MQ": ("MTQ", "Martinique", _LA), "MR": ("MRT", "Mauritania", _ME),
    "MS": ("MSR", "Montserrat", _LA), "MT": ("MLT", "Malta", _EU),
    "MU": ("MUS", "Mauritius", _ME), "MV": ("MDV", "Maldives", _AP),
    "MW": ("MWI", "Malawi", _ME), "MX": ("MEX", "Mexico", _LA),
    "MY": ("MYS", "Malaysia", _AP), "MZ": ("MOZ", "Mozambique", _ME),
    "NA": ("NAM", "Namibia", _ME), "NC": ("NCL", "New Caledonia", _AP),
    "NE": ("NER", "Niger", _ME), "NF": ("NFK", "Norfolk Island", _AP),
    "NG": ("NGA", "Nigeria", _ME), "NI": ("NIC", "Nicaragua", _LA),
    "NL": ("NLD", "Netherlands", _EU), "NO": ("NOR", "Norway", _EU),
    "NP": ("NPL", "Nepal", _AP), "NR": ("NRU", "Nauru", _AP),
    "NU": ("NIU", "Niue", _AP), "NZ": ("NZL", "New Zealand", _AP),
    "OM": ("OMN", "Oman", _ME), "PA": ("PAN", "Panama", _LA),
    "PE": ("PER", "Peru", _LA), "PF": ("PYF", "French Polynesia", _AP),
    "PG": ("PNG", "Papua New Guinea", _AP), "PH": ("PHL", "Philippines", _AP),
    "PK": ("PAK", "Pakistan", _AP), "PL": ("POL", "Poland", _EU),
    "PM": ("SPM", "Saint Pierre and Miquelon", _NA), "PN": ("PCN", "Pitcairn", _AP),
    "PR": ("PRI", "Puerto Rico", _NA), "PS": ("PSE", "Palestine", _ME),
    "PT": ("PRT", "Portugal", _EU), "PW": ("PLW", "Palau", _AP),
    "PY": ("PRY", "Paraguay", _LA), "QA": ("QAT", "Qatar", _ME),
    "RE": ("REU", "Reunion", _ME), "RO": ("ROU", "Romania", _EU),
    "RS": ("SRB", "Serbia", _EU), "RU": ("RUS", "Russia", _EU),
    "RW": ("RWA", "Rwanda", _ME), "SA": ("SAU", "Saudi Arabia", _ME),
    "SB": ("SLB", "Solomon Islands", _AP), "SC": ("SYC", "Seychelles", _ME),
    "SD": ("SDN", "Sudan", _ME), "SE": ("SWE", "Sweden", _EU),
    "SG": ("SGP", "Singapore", _AP), "SH": ("SHN", "Saint Helena", _ME),
    "SI": ("SVN", "Slovenia", _EU), "SJ": ("SJM", "Svalbard and Jan Mayen", _EU),
    "SK": ("SVK", "Slovakia", _EU), "SL": ("SLE", "Sierra Leone", _ME),
    "SM": ("SMR", "San Marino", _EU), "SN": ("SEN", "Senegal", _ME),
    "SO": ("SOM", "Somalia", _ME), "SR": ("SUR", "Suriname", _LA),
    "SS": ("SSD", "South Sudan", _ME), "ST": ("STP", "Sao Tome and Principe", _ME),
    "SV": ("SLV", "El Salvador", _LA), "SX": ("SXM", "Sint Maarten", _LA),
    "SY": ("SYR", "Syria", _ME), "SZ": ("SWZ", "Eswatini", _ME),
    "TC": ("TCA", "Turks and Caicos Islands", _LA), "TD": ("TCD", "Chad", _ME),
    "TG": ("TGO", "Togo", _ME), "TH": ("THA", "Thailand", _AP),
    "TJ": ("TJK", "Tajikistan", _AP), "TK": ("TKL", "Tokelau", _AP),
    "TL": ("TLS", "Timor-Leste", _AP), "TM": ("TKM", "Turkmenistan", _AP),
    "TN": ("TUN", "Tunisia", _ME), "TO": ("TON", "Tonga", _AP),
    "TR": ("TUR", "Turkiye", _EU), "TT": ("TTO", "Trinidad and Tobago", _LA),
    "TV": ("TUV", "Tuvalu", _AP), "TW": ("TWN", "Taiwan", _AP),
    "TZ": ("TZA", "Tanzania", _ME), "UA": ("UKR", "Ukraine", _EU),
    "UG": ("UGA", "Uganda", _ME), "UM": ("UMI", "United States Minor Outlying Islands", _NA),
    "US": ("USA", "United States", _NA), "UY": ("URY", "Uruguay", _LA),
    "UZ": ("UZB", "Uzbekistan", _AP), "VA": ("VAT", "Holy See", _EU),
    "VC": ("VCT", "Saint Vincent and the Grenadines", _LA), "VE": ("VEN", "Venezuela", _LA),
    "VG": ("VGB", "British Virgin Islands", _LA), "VI": ("VIR", "US Virgin Islands", _NA),
    "VN": ("VNM", "Vietnam", _AP), "VU": ("VUT", "Vanuatu", _AP),
    "WF": ("WLF", "Wallis and Futuna", _AP), "WS": ("WSM", "Samoa", _AP),
    "XK": ("XKX", "Kosovo", _EU), "YE": ("YEM", "Yemen", _ME),
    "YT": ("MYT", "Mayotte", _ME), "ZA": ("ZAF", "South Africa", _ME),
    "ZM": ("ZMB", "Zambia", _ME), "ZW": ("ZWE", "Zimbabwe", _ME),
}

#: Provider spellings beyond the short names above, normalised by `_key`. iShares writes
#: "Korea (South)"; ISO's long forms ("Korea, Republic of", "Taiwan, Province of China") turn up
#: in DWS-style files; Yahoo writes "South Korea" and "Hong Kong".
_ALIASES: Dict[str, str] = {
    "united states of america": "US", "u s": "US", "u s a": "US", "america": "US",
    "uk": "GB", "u k": "GB", "great britain": "GB", "britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "united kingdom of great britain and northern ireland": "GB",
    "korea": "KR", "korea south": "KR", "korea republic of": "KR", "republic of korea": "KR",
    "korea rep": "KR", "south korea republic of": "KR",
    "korea north": "KP", "korea democratic people s republic of": "KP", "dprk": "KP",
    "taiwan province of china": "TW", "chinese taipei": "TW", "republic of china": "TW",
    "taiwan republic of china": "TW", "taiwan roc": "TW",
    "people s republic of china": "CN", "peoples republic of china": "CN", "prc": "CN",
    "mainland china": "CN", "china mainland": "CN",
    "hong kong sar": "HK", "hong kong china": "HK", "hong kong sar china": "HK",
    "hongkong": "HK", "macau": "MO", "macao sar": "MO", "macau sar": "MO",
    "macao sar china": "MO",
    "russian federation": "RU", "viet nam": "VN", "czech republic": "CZ",
    "turkey": "TR", "republic of turkiye": "TR",
    "holland": "NL", "netherlands the": "NL",
    "uae": "AE", "iran islamic republic of": "IR", "syrian arab republic": "SY",
    "lao people s democratic republic": "LA", "lao pdr": "LA",
    "bolivia plurinational state of": "BO", "venezuela bolivarian republic of": "VE",
    "tanzania united republic of": "TZ", "united republic of tanzania": "TZ",
    "moldova republic of": "MD", "republic of moldova": "MD",
    "macedonia": "MK", "former yugoslav republic of macedonia": "MK",
    "ivory coast": "CI", "swaziland": "SZ", "cape verde": "CV", "burma": "MM",
    "east timor": "TL", "brunei": "BN", "micronesia federated states of": "FM",
    "state of palestine": "PS", "palestinian territory": "PS", "vatican": "VA",
    "vatican city": "VA", "republic of the congo": "CG", "congo republic of the": "CG",
    "congo democratic republic of the": "CD", "dr congo": "CD", "drc": "CD",
    "virgin islands british": "VG", "bvi": "VG", "virgin islands u s": "VI",
    "u s virgin islands": "VI", "slovak republic": "SK", "kyrgyz republic": "KG",
    "bosnia": "BA", "bosnia herzegovina": "BA", "trinidad": "TT",
    "st kitts and nevis": "KN", "st lucia": "LC", "st vincent and the grenadines": "VC",
    "saint martin french part": "MF", "sint maarten dutch part": "SX",
    "gambia the": "GM", "bahamas the": "BS",
    "curacao": "CW", "reunion": "RE", "aland islands": "AX", "saint barthelemy": "BL",
    "falkland islands malvinas": "FK", "faeroe islands": "FO",
}

#: A region named outright rather than a country — a provider's own grouping.
_REGION_NAMES: Dict[str, str] = {
    "north america": _NA,
    "europe": _EU, "european union": _EU, "eurozone": _EU, "euroland": _EU,
    "asia pacific": _AP, "asia": _AP, "pacific": _AP, "asia pacific ex japan": _AP,
    "asia ex japan": _AP,
    "latin america": _LA, "south america": _LA, "central america": _LA, "caribbean": _LA,
    "middle east and africa": _ME, "middle east": _ME, "africa": _ME,
}

#: Strings that are a provider saying "no country", including ones that would otherwise read
#: as a code ("NA" is Namibia).
_EXPLICIT_UNKNOWN = frozenset({
    "", "unknown", "other", "others", "na", "n a", "none", "not available", "various",
    "multiple", "multi", "global", "international", "world", "cash", "supranational",
    "emerging markets", "developed markets",
})

#: ISIN prefixes that name where a company was incorporated and nothing about where it does
#: business: a Cayman or Bermuda prefix is as likely Alibaba or Arch Capital as anything
#: Caribbean. `region_of_isin` answers UNKNOWN for these rather than guess.
_OFFSHORE = frozenset({"KY", "BM", "VG", "JE", "GG", "IM", "BS", "MH", "PA", "LR", "CW"})


def _key(raw: str) -> str:
    """Accents stripped, lower case, `&` as "and", punctuation as spaces, a leading "the" off."""
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("&", " and ")
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    return text[4:] if text.startswith("the ") else text


def _build_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for alpha2, (alpha3, name, _region) in _COUNTRIES.items():
        for spelling in (alpha2, alpha3, name):
            index[_key(spelling)] = alpha2
    for spelling, alpha2 in _ALIASES.items():
        index[_key(spelling)] = alpha2
    return index


_INDEX = _build_index()


def country_code(raw: Optional[str]) -> Optional[str]:
    """A provider's country string as an ISO 3166-1 alpha-2 code, or None."""
    if raw is None:
        return None
    key = _key(raw)
    if key in _EXPLICIT_UNKNOWN:
        return None
    return _INDEX.get(key)


def region_of(raw: Optional[str]) -> str:
    """A stated country (or a region named outright) as one of `REGIONS`, or `UNKNOWN`."""
    if raw is None:
        return UNKNOWN
    key = _key(raw)
    if key in _EXPLICIT_UNKNOWN:
        return UNKNOWN
    if key in _REGION_NAMES:
        return _REGION_NAMES[key]
    code = _INDEX.get(key)
    return _COUNTRIES[code][2] if code else UNKNOWN


def region_of_isin(isin: Optional[str]) -> str:
    """
    The region of the country an ISIN was registered in, or `UNKNOWN`.

    A fallback, never a stated country: see the module docstring for why an offshore prefix
    answers nothing, and why `XS` (Euroclear) and the like are not countries at all.
    """
    if not isin:
        return UNKNOWN
    prefix = str(isin).strip().upper()[:2]
    if len(prefix) != 2 or prefix in _OFFSHORE or prefix not in _COUNTRIES:
        return UNKNOWN
    return _COUNTRIES[prefix][2]


def unmapped_countries(raws: Iterable[Optional[str]]) -> set:
    """
    The distinct spellings that fell through to `UNKNOWN` without being declared unknown.

    For diagnostics: a provider renaming "Korea (South)" would otherwise show up only as a
    quietly growing Unknown slice on the Regions chart, which looks like missing data rather
    than a missing alias.
    """
    missing = set()
    for raw in raws:
        if raw is None:
            continue
        key = _key(raw)
        if key in _EXPLICIT_UNKNOWN or key in _REGION_NAMES or key in _INDEX:
            continue
        missing.add(str(raw).strip())
    return missing
