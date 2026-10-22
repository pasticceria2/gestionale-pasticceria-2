import re
from decimal import Decimal

def format_number(val):
    if val is None or val == "":
        return ""
    try:
        f = float(str(val).replace(',', '.'))
        if f == 0:
            return ""
        if f.is_integer():
            return str(int(f))
        return f"{f:g}"
    except (ValueError, TypeError):
        return str(val)

def _parse_salato_item(item_string, default_unit=None, default_qta=None):
    if not item_string:
        item_string = ""
    item_string = item_string.strip()
    
    # Unit regex pattern
    units_pattern = r'(kg|chili|chilo|kgs|grammi|grammo|gr\.?|g|persone|persona|pers\.?|pezzi|pezzo|pz\.?|pzz|vassoi|vassoio|teglie|teglia|porzioni|porzione)'
    
    # 1. Pattern: number + unit (e.g., "40 pezzi", "1.5 kg", "1,5kg", "per 20 persone", "x 30 pz")
    match_num_unit = re.search(
        r'(?:(?:per|x|da)\s+)?(\d+(?:[.,]\d+)?)\s*' + units_pattern + r'\b',
        item_string,
        flags=re.IGNORECASE
    )
    
    # 2. Pattern: unit + number (e.g., "kg 1.5", "persone 20", "pz: 30")
    match_unit_num = re.search(
        r'\b' + units_pattern + r'\s*[:=]?\s*(\d+(?:[.,]\d+)?)',
        item_string,
        flags=re.IGNORECASE
    )
    
    quantity_str = ""
    name_str = item_string
    
    if match_num_unit:
        q_num = format_number(match_num_unit.group(1))
        unit = match_num_unit.group(2).lower()
        quantity_str = f"{q_num} {unit}".strip()
        # Remove matched portion from name
        name_str = item_string[:match_num_unit.start()] + ' ' + item_string[match_num_unit.end():]
    elif match_unit_num:
        unit = match_unit_num.group(1).lower()
        q_num = format_number(match_unit_num.group(2))
        quantity_str = f"{q_num} {unit}".strip()
        name_str = item_string[:match_unit_num.start()] + ' ' + item_string[match_unit_num.end():]
    else:
        # Just number search (e.g. "40 pizzette")
        match_num = re.search(r'(\d+(?:[.,]\d+)?)', item_string)
        if match_num:
            q_num = format_number(match_num.group(1))
            unit = (default_unit or "").strip().lower()
            quantity_str = f"{q_num} {unit}".strip()
            name_str = item_string[:match_num.start()] + ' ' + item_string[match_num.end():]
        elif default_qta:
            q_num = format_number(default_qta)
            if q_num:
                unit = (default_unit or "").strip().lower()
                quantity_str = f"{q_num} {unit}".strip()

    # Clean up product name
    # Remove leftover unit keywords if any isolated words
    name_str = re.sub(r'\b(?:per|x|da)\b', ' ', name_str, flags=re.IGNORECASE)
    name_str = re.sub(r'[\(\)\[\]\:\-\,]', ' ', name_str)
    name_str = re.sub(r'\s+', ' ', name_str).strip()
    
    if not name_str:
        name_str = item_string.strip()
        
    return quantity_str, name_str.upper()

# Test test cases
tests = [
    ("40 pizzette rosse", None, None),
    ("40 pezzi pizzette rosse", None, None),
    ("40 pz pizzette rosse", None, None),
    ("40 pz. pizzette rosse", None, None),
    ("1.5 kg rustici", None, None),
    ("1,5 kg rustici", None, None),
    ("1.5kg rustici", None, None),
    ("500 gr bottoncini", None, None),
    ("500g bottoncini", None, None),
    ("20 persone rustici", None, None),
    ("per 20 persone rustici", None, None),
    ("rustici 1.5 kg", None, None),
    ("pizzette 40 pezzi", None, None),
    ("bottoncini assortiti", "kg", 1.5),
    ("pizzette rosse", "pezzi", 50),
    ("tramezzini", "persone", 20),
    ("salatini", "gr", 500),
]

for t, u, q in tests:
    qta, name = _parse_salato_item(t, default_unit=u, default_qta=q)
    print(f"INPUT: '{t}' (u={u}, q={q}) -> QTA: '{qta}' | NAME: '{name}'")
