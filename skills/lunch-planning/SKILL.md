---
name: lunch-planning
description: Mittagessen abfragen, planen und ändern sowie die Einkaufsliste in Mahlino befüllen.
allowed-tools: get_current_date get_lunch_plan get_lunch_planning_data propose_lunch_plan add_to_shopping_list add_lunches_to_shopping_list
---

## Mittagessen-Planung

Rufe bei allem, was Datumsangaben betrifft, zuerst get_current_date auf.

### Wichtig zum Speichern
Du kannst selbst nichts im Essensplan speichern. Du schlägst nur mit propose_lunch_plan vor,
der Bot zeigt den Vorschlag dem Haushalt mit Buttons. Sagt jemand „Ja“ oder „Speichern“,
verweise auf den Button „Annehmen & speichern“. Behaupte nie, etwas sei gespeichert.

### Wochentag-Regel
Dienstags kocht Mama. Plane dafür nie ein Rezept ein. Weise in deiner Antwort kurz darauf hin.

### Wochenplanung
1. Rufe get_lunch_planning_data für heute bis in 7 Tagen auf.
2. Wähle für jeden Tag aus free_days ein Rezept aus candidates:
   a. Abwechslung: Bevorzuge Rezepte, die lange nicht dran waren.
   b. Dauer: Mo-Fr eher unter 45 Minuten, am Wochenende auch aufwändiger. Ist minutes
      leer, schätze anhand des Namens.
   c. Vielfalt: Mische verschiedene Arten von Gerichten über die Woche.
   d. Saison: Bei sonst gleichwertigen Optionen bevorzuge saisonale Zutaten (Mitteleuropa).
3. Rufe propose_lunch_plan auf. Antworte danach mit höchstens einem Satz.

Sind keine free_days vorhanden, sage das und schlage nichts vor.

### Änderungen
Bei Änderungen an einem offenen Vorschlag rufst du propose_lunch_plan erneut mit allen Tagen
auf. Für bereits geplante Tage, etwa beim Tauschen, setzt du replace=true. Schau dir dafür
vorher mit get_lunch_plan an, was geplant ist.

### Einkaufsliste
Einzelne Dinge trägst du direkt mit add_to_shopping_list ein. Die Zutaten geplanter Tage
kommen nur auf ausdrücklichen Wunsch mit add_lunches_to_shopping_list auf die Liste, einmal pro Tag.
