"""No-trade band: kauppaa ei tehda, jos tavoitepainon muutos on alle kynnyksen (kulujen saasto).
Suodatus tehdaan ENNEN salkkua: salkku.py, riskivahti.py ja rajat.py ovat koskemattomia. Salkku siivoaa tuloksen silti omilla rajoillaan.
Vain lukeminen: salkkuun ei kirjoiteta. Jos suodatus ei onnistu, ehdotus palautetaan sellaisenaan."""
from datetime import datetime, timezone

import rajat
import riskivahti
import salkku

# Kovakoodattu oletus. 0.0 = pois paalta (kayttaytyminen ennallaan). Kayttoonoton paattaa Tony.
KYNNYS_PP = 0.0


def _avaushinnat(markkina, paiva, symbolit):
    d_ms = int(datetime(paiva.year, paiva.month, paiva.day, tzinfo=timezone.utc).timestamp() * 1000)
    ulos = {}
    for s in symbolit:
        h = salkku._hinnat(markkina, s, d_ms)
        if h is None:
            return None
        ulos[s] = h[0]
    return ulos


def suodata(con, markkina, paiva, ehdotus, sallitut, kynnys_pp=None):
    """Palauttaa ehdotuksen, jossa pienet painomuutokset on korvattu nykypainolla (ei kauppaa).
    Tavoite 0 (poistuminen) ja ehdotuksesta puuttuva kolikko (= myy pois) eivat jaa kynnyksen taakse."""
    kp = KYNNYS_PP if kynnys_pp is None else kynnys_pp
    if kp <= 0 or not isinstance(ehdotus, dict):
        return ehdotus
    try:
        painot, _ = riskivahti.siisti_ehdotus(ehdotus, sallitut)
    except riskivahti.Ehdotusvirhe:
        return ehdotus
    omat = salkku.positiot(con)
    hinnat = _avaushinnat(markkina, paiva, set(omat) | set(painot))
    if hinnat is None:
        return ehdotus
    v = salkku.kateinen(con) + sum(omat[s] * hinnat[s] for s in omat)
    if v <= 0:
        return ehdotus
    nyky = {s: omat.get(s, 0.0) * hinnat[s] / v for s in set(omat) | set(painot)}
    uusi = dict(painot)
    for s, w in painot.items():
        if w > 0 and abs(w - nyky[s]) * 100.0 < kp and nyky[s] <= rajat.MAX_POSITIO:
            if nyky[s] > 0:
                uusi[s] = nyky[s]
            else:
                del uusi[s]   # pienta uutta positiota ei avata
    if sum(uusi.values()) > 1.0 - rajat.KATEISPUSKURI_MIN:
        return ehdotus   # nykypainot eivat mahdu kateispuskuriin: ala muuta mitaan
    return uusi
