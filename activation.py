import re


def validate_pro_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{7}", str(code))) and sum(map(int, str(code))) == 54


def validate_ultra_code(code: str) -> bool:
    return bool(re.fullmatch(r"\d{8}", str(code))) and sum(map(int, str(code))) == 66


def get_edition(code: str):
    if validate_ultra_code(code):
        return "ultra"
    if validate_pro_code(code):
        return "pro"
    return "standard"
