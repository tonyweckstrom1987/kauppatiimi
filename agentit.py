"""Analyytikko ja kriitikko. Malli saa vain koodin tuottaman <data>-taulukon; vastaus on tiukka JSON.
Virheellinen vastaus = ei kauppoja (fail closed). Ei uusintayrityksia."""
import json
import re

import llm

ANALYYTIKKO = """You are the ANALYST of a paper-trading team (simulated money, daily decisions).
Decide target portfolio weights for the next day using ONLY the numbers inside the <data> block. The numbers were computed by code from daily candles; never compute, estimate or invent numbers yourself.
Allowed idea families: cross-sectional momentum (rank by r90/r30), volatility-scaled sizing (lower weight when vol30 is high), rebalancing toward targets, carry (funding) only as a tie-breaker.
FORBIDDEN (known losing families): EMA scalping, MACD, RSI variants, DCA+RSI, adaptive market-state switching, long-term daily indicator combinations.
Costs are real: about 0.2 % per trade (fee + slippage). Avoid needless turnover. Holding cash is a valid, often best decision. Not trading is allowed.
If the data is insufficient or you are not confident, answer with the don't-know schema. Admitting uncertainty is better than forcing an answer.
Weights are fractions of total portfolio value: each 0..0.15, total at most 0.90, only symbols that appear in the table, unlisted symbols mean 0.
Output exactly ONE JSON object and nothing else.
Schema A: {"paino": {"BTCUSDT": 0.10}, "perustelu": "max 500 chars", "luottamus": "matala|keskitaso|korkea"}
Schema B: {"en_tieda": true, "syy": "max 300 chars"}"""

KRIITIKKO = """You are the CRITIC of a paper-trading team. You receive the data table, the current holdings and the analyst's proposal.
Look for weaknesses: overfitting to recent returns, concentration, high volatility, correlated positions, trading costs (about 0.2 % per trade) versus expected benefit, too much turnover, missing data (n/a values), ignoring the current drawdown.
You may only REDUCE risk, never increase it. Output exactly ONE JSON object and nothing else:
{"paatos": "hyvaksy" | "pienenna" | "hylkaa", "kerroin": 0.0-1.0 (only for pienenna: multiplier applied to all weights), "ongelmat": ["short strings"]}
"hylkaa" means: make no trades today."""


class Vastausvirhe(Exception):
    pass


def pura_json(teksti):
    t = re.sub(r"```(?:json)?", "", teksti).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise Vastausvirhe("ei JSON-oliota")
    try:
        o = json.loads(t[a:b + 1])
    except (ValueError, RecursionError):
        raise Vastausvirhe("JSON ei jasenny")
    if not isinstance(o, dict):
        raise Vastausvirhe("JSON ei ole olio")
    return o


def jasenna_analyytikko(teksti):
    """Palauttaa {'en_tieda': True, 'syy': ..} tai {'paino': {...}, 'perustelu': .., 'luottamus': ..}."""
    o = pura_json(teksti)
    if o.get("en_tieda") is True:
        ulos = {"en_tieda": True, "syy": str(o.get("syy", ""))[:300]}
        if "datapyynto" in o:
            ulos["datapyynto"] = o["datapyynto"]
        return ulos
    p = o.get("paino")
    if not isinstance(p, dict):
        raise Vastausvirhe("paino puuttuu")
    for s, w in p.items():
        if not isinstance(s, str) or isinstance(w, bool) or not isinstance(w, (int, float)):
            raise Vastausvirhe("paino virheellinen")
    try:
        painot = {s: float(w) for s, w in p.items()}
    except (OverflowError, ValueError):
        raise Vastausvirhe("paino liian suuri")
    ulos = {"paino": painot,
            "perustelu": str(o.get("perustelu", ""))[:500],
            "luottamus": o.get("luottamus") if o.get("luottamus") in ("matala", "keskitaso", "korkea") else "matala"}
    if "datapyynto" in o:
        ulos["datapyynto"] = o["datapyynto"]   # raaka; silmukka.tee_pyynto validoi (huono pyynto ei kaada ajoa)
    return ulos


def jasenna_kriitikko(teksti):
    o = pura_json(teksti)
    paatos = o.get("paatos")
    if paatos not in ("hyvaksy", "pienenna", "hylkaa"):
        raise Vastausvirhe("paatos virheellinen")
    kerroin = 1.0
    if paatos == "pienenna":
        k = o.get("kerroin")
        if isinstance(k, bool) or not isinstance(k, (int, float)) or not 0.0 <= k <= 1.0:
            raise Vastausvirhe("kerroin virheellinen")
        kerroin = float(k)
    ong = o.get("ongelmat")
    ong = [str(x)[:200] for x in ong][:6] if isinstance(ong, list) else []
    return {"paatos": paatos, "kerroin": kerroin, "ongelmat": ong}


def kysy_analyytikko(taulukko, omistukset, malli, env, hae=None, havainnot="", ohje=""):
    """havainnot: valmis <havainnot>-lohko (tyhja = ei lisata); ohje: lisapromptti datapyynnolle (tyhja = ei lisata). Molemmat tulevat silmukka.py:sta."""
    sisalto = "%s\n<salkku>\n%s\n</salkku>" % (taulukko, omistukset)
    if havainnot:
        sisalto += "\n" + havainnot
    viestit = [{"role": "system", "content": ANALYYTIKKO + ohje},
               {"role": "user", "content": sisalto}]
    teksti, kaytto = llm.kysy(viestit, malli, env, hae)
    try:
        return jasenna_analyytikko(teksti), kaytto
    except Vastausvirhe as e:
        e.kaytto = kaytto   # kutsu tapahtui ja maksoi: kirjataan vaikka vastaus oli kelvoton
        raise


def kysy_kriitikko(taulukko, omistukset, ehdotus, malli, env, hae=None):
    viestit = [{"role": "system", "content": KRIITIKKO},
               {"role": "user", "content": "%s\n<salkku>\n%s\n</salkku>\n<ehdotus>\n%s\n</ehdotus>" % (
                   taulukko, omistukset, json.dumps(ehdotus, ensure_ascii=False))}]
    teksti, kaytto = llm.kysy(viestit, malli, env, hae)
    try:
        return jasenna_kriitikko(teksti), kaytto
    except Vastausvirhe as e:
        e.kaytto = kaytto
        raise


def sovella_kriitikko(painot, kritiikki):
    """Palauttaa painot (pienennettyna) tai None (hylkaa = ei kauppoja). Koskaan ei kasvateta."""
    if kritiikki["paatos"] == "hylkaa":
        return None
    k = kritiikki["kerroin"] if kritiikki["paatos"] == "pienenna" else 1.0
    return {s: w * k for s, w in painot.items()}
