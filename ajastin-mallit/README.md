# Illan paivaretro: asennusohje Tonylle

Ajastintiedostot ovat tassa malleina (`.malli`), koska `.service`- ja `.timer`-tiedostot ovat tiimin rajattuja tiedostoja.
Tony kopioi ne itse kauppatiimin konttiin, kun PR on yhdistetty.

1. Paivita koodi konttiin (retro.py:ssa on uusi valitsin `--paiva`).
2. Kopioi mallit ilman `.malli`-paatetta hakemistoon `/etc/systemd/system/`:
   `kauppa-retro-paiva.service` ja `kauppa-retro-paiva.timer`.
3. `systemctl daemon-reload && systemctl enable --now kauppa-retro-paiva.timer`
4. Tarkista: `systemctl list-timers kauppa-retro-paiva.timer` ja ensimmaisen ajon jalkeen
   `journalctl -u kauppa-retro-paiva.service -n 20` seka `cat /srv/kauppa/havainnot/havainnot.md`.

Paivatila ei kutsu LLM:aa eika lahetä Telegramia. Ajo tulostaa yhden rivin lokiin. Sunnuntain viikkoretro (`kauppa-retro.timer`) jatkaa ennallaan ja säilyttää paivaosan.
