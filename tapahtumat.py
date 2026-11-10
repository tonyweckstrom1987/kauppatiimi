"""Vain lisaava tapahtumaloki (JSON-rivit). Vain prosessin mittarit: ei strategioita, ei hintadataa, ei salaisuuksia.
Kirjausvirhe EI koskaan kaada ajoa. Polku: KAUPPA_TAPAHTUMAT (oletus /srv/kauppa/data/tapahtumat.jsonl); jos hakemistoa ei ole, ei kirjata."""
import json
import os
import re
import time

POLKU = os.environ.get("KAUPPA_TAPAHTUMAT", "/srv/kauppa/data/tapahtumat.jsonl")
SALLITUT_KENTAT = ("ajo", "ryhma", "malli", "tulos", "virhekoodi", "tokens_in", "tokens_out", "kesto_s", "ehdotettu", "hylatty", "kelvollinen", "hylkaykset", "keksitty", "tuplia")
SALAISUUDET = [re.compile(r"sk-[A-Za-z0-9_-]{16,}"), re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")]
TULOKSET = ("ok", "ei_tietoa", "virhe", "kaikki_hylatty")   # kaikki_hylatty: K7 ehdotti, mutta yksikaan speksi ei lapaissyt tarkistusta


def _siivoa(arvo):
    if isinstance(arvo, str):
        for s in SALAISUUDET:
            arvo = s.sub("[poistettu]", arvo)
        return arvo[:120]
    return arvo


def kirjaa(ajo, tulos, polku=None, nyt=None, **kentat):
    """ajo = 'paiva' | 'k7'. Palauttaa True jos rivi kirjattiin. Tuntemattomat kentat ja pitkat tekstit pudotetaan."""
    polku = polku or POLKU
    try:
        if tulos not in TULOKSET or not os.path.isdir(os.path.dirname(polku) or "."):
            return False
        rivi = {"aika": int(nyt if nyt is not None else time.time()), "ajo": ajo, "tulos": tulos}
        for k, v in kentat.items():
            if k in SALLITUT_KENTAT and k != "ajo" and v is not None:
                rivi[k] = _siivoa(v)
        with open(polku, "a", encoding="utf-8") as f:
            f.write(json.dumps(rivi, ensure_ascii=True, sort_keys=True) + "\n")
        return True
    except Exception:  # kirjaus ei saa kaataa ajoa
        return False


def paivan_tulos(t):
    """Tuloksen luokka paivaajon raporttidictista: virhe / ei_tietoa / ok."""
    if t.get("virhe"):
        return "virhe"
    a = t.get("analyytikko")
    return "ei_tietoa" if isinstance(a, dict) and a.get("en_tieda") else "ok"


def virhekoodi(virhe):
    """Virhetekstin alku (esim. LLMVirhe, Vastausvirhe, paivan LLM-kutsukatto), ei viestin sisaltoa."""
    return str(virhe).split(":", 1)[0][:40] if virhe else None
