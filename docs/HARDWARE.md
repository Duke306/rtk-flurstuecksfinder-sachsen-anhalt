# Hardware

## Referenzaufbau

- Raspberry Pi Zero 2 W
- Waveshare LC29H(DA) GPS/RTK HAT
- Dualband-GNSS-Antenne
- Smartphone-Hotspot
- USB-Powerbank
- GNSS-/Vermessungsstab mit Dosenlibelle

## LC29H-HAT

Für UART über den Raspberry-Pi-GPIO beide gelben UART-Auswahljumper auf **B** setzen. Der Pi muss beim Umstecken stromlos sein.

Standardbaudrate der Projektkonfiguration: **115200 Baud**.

## Antennenmontage

Für reproduzierbare Zentimeterergebnisse ist die Mechanik wichtig:

- Antenne möglichst freie Sicht zum Himmel
- Abstand zu Metallflächen, Fahrzeugen und Fassaden
- Stab lotrecht halten
- Antennen-/Stabhöhe im Höhenmodus korrekt eintragen

Multipath kann auch bei angezeigtem RTK FIX zu fehlerhaften Ergebnissen führen; Kontrollmessungen bleiben wichtig.
