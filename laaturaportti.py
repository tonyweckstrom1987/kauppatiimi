"""Laaturaportti: lukee tapahtumalokin (tapahtumat.py) ja laskee prosessilaadun mittarit ryhmittain (doc 42).
Vain luku, ei koske kauppoihin, salkkuihin eika holdoutiin. Ei strategioita eika hintadataa.

Portti (doc 42, prosessilaatu): kelvollisten tulosten osuus >= 90 %, keksittyja faktoja 0 (tarkistimen tarkkuus on mitattu
eval-joukolla: katso test_eval_tarkistus.py), virheiden osuus <= verrokki + 5 prosenttiyksikkoa.
K7 --kuiva (doc 43): kelvollisten ehdotusten osuus >= 90 %, tuplia <= 20 %, virheet <= verrokki + 5 pp.
Tuotto ei kuulu tahan raporttiin: tuotto ratkaisee vasta kolmen peräkkaisen 90 paivan jakson jalkeen."""
import argparse
import json
import sys
import time
from collections import Counter, defaultdict

KELPO_MIN = 0.90
VIRHE_MARGINAALI = 0.05
TUPLIA_MAX = 0.20
MIN_N = 20          # pienemmalla otoksella ok ei tarkoita lapaisya (ei_dataa); EI nakyy silti


def lue(polku, alku=None):
    """Palauttaa rivit (dictit); rikkinaiset rivit ohitetaan."""
    rivit = []
    try:
        with open(polku, encoding="utf-8") as f:
            for rivi in f:
                try:
                    r = json.loads(rivi)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("aika"), int) and (alku is None or r["aika"] >= alku):
                    rivit.append(r)
    except OSError:
        pass
    return rivit


def _osuus(a, b):
    return a / b if b else None


def laske(rivit):
    """Ryhmittelee (ajo, ryhma) ja palauttaa sanakirjan mittareista."""
    ryhmat = defaultdict(list)
    for r in rivit:
        ryhmat[(r.get("ajo", "?"), r.get("ryhma") or r.get("malli") or "?")].append(r)
    tulos = {}
    for avain, rs in sorted(ryhmat.items()):
        n = len(rs)
        c = Counter(r.get("tulos") for r in rs)
        virheet = Counter(r.get("virhekoodi", "?") for r in rs if r.get("tulos") == "virhe")
        kestot = [r["kesto_s"] for r in rs if isinstance(r.get("kesto_s"), (int, float))]
        keksityt = [r["keksitty"] for r in rs if isinstance(r.get("keksitty"), int)]
        ehd = sum(r.get("ehdotettu", 0) for r in rs if isinstance(r.get("ehdotettu"), int))
        kelv = sum(r.get("kelvollinen", 0) for r in rs if isinstance(r.get("kelvollinen"), int))
        tup = sum(r.get("tuplia", 0) for r in rs if isinstance(r.get("tuplia"), int))
        syyt = Counter()
        for r in rs:
            h = r.get("hylkaykset")
            for osa in h.split(",") if isinstance(h, str) else ():
                koodi, _, lkm = osa.partition(":")
                if koodi and lkm.isdigit():
                    syyt[koodi[:40]] += int(lkm)
        tulos[avain] = {
            "n": n, "ok": c["ok"], "ei_tietoa": c["ei_tietoa"], "virhe": c["virhe"],
            "kaikki_hylatty": c["kaikki_hylatty"], "hylkayssyyt": dict(syyt.most_common(8)),
            "kelpo_osuus": _osuus(c["ok"] + c["ei_tietoa"], n),
            "virhe_osuus": _osuus(c["virhe"], n),
            "virhekoodit": dict(virheet.most_common(5)),
            "kesto_mediaani_s": sorted(kestot)[len(kestot) // 2] if kestot else None,
            "kesto_max_s": max(kestot) if kestot else None,
            "keksitty_rivit": sum(1 for k in keksityt if k > 0), "keksitty_mitattu": len(keksityt),
            "ehdotettu": ehd, "kelvollinen": kelv, "tuplia": tup,
            "kelv_osuus": _osuus(kelv, ehd), "tuplia_osuus": _osuus(tup, kelv),
        }
    return tulos


def portti(mittarit, verrokki="pro"):
    """Palauttaa {(ajo, ryhma): [(kohta, ok|EI|ei_dataa, selite), ...]} muille ryhmille kuin verrokille."""
    ulos = {}
    for (ajo, ryhma), m in mittarit.items():
        if ryhma == verrokki:
            continue
        v = mittarit.get((ajo, verrokki))
        rivit = []
        kuiva = ajo.endswith("_kuiva")
        osuus = m["kelv_osuus"] if kuiva else m["kelpo_osuus"]
        nimi = "kelvollisten ehdotusten osuus" if kuiva else "kelvollisten tulosten osuus"
        rivit.append((nimi + " >= 90 %", "ei_dataa" if osuus is None else ("ok" if osuus >= KELPO_MIN else "EI"),
                      "-" if osuus is None else "%.0f %% (n=%d)" % (osuus * 100, m["n"])))
        if kuiva:
            t = m["tuplia_osuus"]
            rivit.append(("tuplia <= 20 %", "ei_dataa" if t is None else ("ok" if t <= TUPLIA_MAX else "EI"),
                          "-" if t is None else "%.0f %%" % (t * 100)))
        else:
            if m["keksitty_mitattu"] == 0:
                rivit.append(("keksittyja faktoja 0", "ei_dataa", "ei mitattu"))
            else:
                rivit.append(("keksittyja faktoja 0", "ok" if m["keksitty_rivit"] == 0 else "EI",
                              "%d/%d ajossa" % (m["keksitty_rivit"], m["keksitty_mitattu"])))
        if v is None or v["virhe_osuus"] is None or m["virhe_osuus"] is None:
            rivit.append(("virheet <= verrokki + 5 pp", "ei_dataa", "verrokkia (%s) ei ole" % verrokki))
        else:
            ok = m["virhe_osuus"] <= v["virhe_osuus"] + VIRHE_MARGINAALI
            rivit.append(("virheet <= verrokki + 5 pp", "ok" if ok else "EI",
                          "%.0f %% vs %.0f %% (%s)" % (m["virhe_osuus"] * 100, v["virhe_osuus"] * 100, verrokki)))
        if m["n"] < MIN_N:
            rivit = [(k, "ei_dataa" if t == "ok" else t, sel) for k, t, sel in rivit]
            rivit.insert(0, ("otoskoko >= %d" % MIN_N, "ei_dataa", "n=%d" % m["n"]))
        ulos[(ajo, ryhma)] = rivit
    return ulos


def teksti(mittarit, portit, paivia=None):
    s = ["# Laaturaportti" + (" (viimeiset %d pv)" % paivia if paivia else ""), ""]
    if not mittarit:
        return "\n".join(s + ["Ei tapahtumia."])
    for (ajo, ryhma), m in mittarit.items():
        s.append("## %s / %s  (n=%d)" % (ajo, ryhma, m["n"]))
        s.append("- ok %d, ei_tietoa %d, virhe %d" % (m["ok"], m["ei_tietoa"], m["virhe"]) +
                 (", kaikki_hylatty %d" % m["kaikki_hylatty"] if m["kaikki_hylatty"] else ""))
        if m["hylkayssyyt"]:
            s.append("- hylkäyssyyt: " + ", ".join("%s %d" % kv for kv in m["hylkayssyyt"].items()))
        if m["virhekoodit"]:
            s.append("- virhekoodit: " + ", ".join("%s %d" % kv for kv in m["virhekoodit"].items()))
        if m["kesto_mediaani_s"] is not None:
            s.append("- kesto: mediaani %.1f s, max %.1f s" % (m["kesto_mediaani_s"], m["kesto_max_s"]))
        if m["ehdotettu"]:
            s.append("- ehdotettu %d, kelvollisia %d, tuplia %d" % (m["ehdotettu"], m["kelvollinen"], m["tuplia"]))
        if (ajo, ryhma) in portit:
            for kohta, tila, selite in portit[(ajo, ryhma)]:
                s.append("  - [%s] %s: %s" % (tila, kohta, selite))
        s.append("")
    s.append("Huom: 'keksitty' on tarkistimen havainto (ks. test_eval_tarkistus.py: tunnettu sokea piste = keksitty rankki).")
    return "\n".join(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prosessilaadun raportti tapahtumalokista (vain luku)")
    ap.add_argument("--polku", default=None)
    ap.add_argument("--paivia", type=int, default=None, help="vain viimeiset N paivaa")
    ap.add_argument("--verrokki", default="pro")
    a = ap.parse_args(argv)
    import tapahtumat
    alku = int(time.time()) - a.paivia * 86400 if a.paivia else None
    m = laske(lue(a.polku or tapahtumat.POLKU, alku))
    print(teksti(m, portti(m, a.verrokki), a.paivia))
    return 0


if __name__ == "__main__":
    sys.exit(main())
