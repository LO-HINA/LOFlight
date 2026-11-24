"""Shared fixed-offset fallbacks for flight data with local timestamps."""
from datetime import timedelta, timezone

from core.registry import CITIES, CITY_CODE_COUNTRY

TZ_CN = timezone(timedelta(hours=8))
COUNTRY_TZ_HOURS = {
    "CN": 8, "VN": 7, "TH": 7, "SG": 8, "JP": 9, "KR": 9, "HK": 8, "TW": 8,
    # 欧洲 / 中东按**冬令时**取值（GMT / CET / 海湾 +3~+4）。
    # 夏令时期间伦敦、法兰克福等会偏 1 小时，只影响跨天判断，不影响价格。
    "GB": 0, "FR": 1, "DE": 1, "NL": 1, "CH": 1, "AT": 1, "IT": 1, "ES": 1,
    "TR": 3, "QA": 3, "AE": 4,
}
# 中转站和二三线城市机场往往不在 cities.json 里，单独给时区，避免标成 +00:00
AIRPORT_TZ_HOURS = {
    # 中国大陆（全国统一 +8）
    **{ap: 8 for ap in (
        "PEK PKX SHA PVG CAN SZX CTU TFU CKG HGH NKG WUH XIY KMG CSX TAO TNA TSN "
        "DLC SHE HRB CGO HFE FOC XMN SWA NNG KWL HAK SYX WNZ XNN LHW URC INC HET "
        "TYN SJW YNT WUX CZX NTG LYG JJN YIW DNH LXA ZUH"
    ).split()},
    # 日本
    **{ap: 9 for ap in "HND NRT KIX ITM NGO FUK CTS OKA UKB SDJ HIJ KOJ".split()},
    # 韩国 / 香港 / 台湾 / 新加坡 / 马来西亚 / 菲律宾（+8）
    **{ap: 9 for ap in "ICN GMP PUS CJU".split()},
    **{ap: 8 for ap in "HKG TPE TSA KHH MFM SIN KUL MNL CGK DPS BWN".split()},
    # 中南半岛 + 缅甸（+7）
    **{ap: 7 for ap in "BKK DMK CNX HKT SGN HAN DAD CXR PQC HUI RGN PNH VTE REP".split()},
    # 海湾（迪拜/阿布扎比 +4，多哈/利雅得 +3）
    **{ap: 4 for ap in "DXB AUH SHJ".split()},
    **{ap: 3 for ap in "DOH RUH JED KWI BAH MCT".split()},
    # 土耳其、印度（+3 / +5:30 用不到分钟级，这里只收 +3 的）
    **{ap: 3 for ap in "IST SAW MOW SVO DME VKO ZIA".split()},
    # 欧洲（冬令时：伦敦 +0，中欧 +1，东欧 +2）
    **{ap: 0 for ap in "LHR LGW STN LTN LCY MAN EDI BHX".split()},
    **{ap: 1 for ap in (
        "CDG ORY AMS FRA MUC ZRH VIE BRU GVA CPH ARN HEL OSL MAD BCN FCO MXP LIN"
    ).split()},
    **{ap: 2 for ap in "WAW PRG BUD OTP ATH".split()},
}
AIRPORT_COUNTRY = {
    airport: info["country"]
    for info in CITIES.values()
    for airport in info.get("airports", [])
}


def airport_timezone(airport_code: str) -> timezone:
    if airport_code in AIRPORT_TZ_HOURS:
        return timezone(timedelta(hours=AIRPORT_TZ_HOURS[airport_code]))
    country = AIRPORT_COUNTRY.get(airport_code) or CITY_CODE_COUNTRY.get(airport_code)
    return timezone(timedelta(hours=COUNTRY_TZ_HOURS.get(country, 0)))
