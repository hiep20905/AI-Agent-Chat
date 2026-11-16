import re
import unicodedata
from typing import Any


def normalized_text(value: Any) -> str:
    """A value compared without accents, case or punctuation: "TP. Hồ Chí Minh" reads "tp ho chi minh"."""
    decomposed = unicodedata.normalize("NFD", str(value).replace("đ", "d").replace("Đ", "D"))
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain.casefold()).split())
