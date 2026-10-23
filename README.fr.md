# 3D Print Cost pour Home Assistant

_[English version](README.md)_

**Combien a réellement coûté cette impression ?** Cette intégration calcule,
pour chaque impression Bambu Lab, le **coût électrique réel**, le **coût du
filament réellement consommé** et le **coût total**, et les expose comme des
entités Home Assistant standards.

```text
Flower Lamp — réussi — 3 h 31

Électricité   0,640 kWh     0,13 €
Filament      PLA Noir    62,4 g   1,21 €
              PLA Blanc   22,2 g   0,51 €
              total       84,6 g   1,72 €
TOTAL                              1,85 €
```

C'est une **couche d'agrégation** : elle ne refait pas ce que d'autres outils font déjà bien.

> **Cette intégration ne modifie pas le stock Spoolman.**
> HaspelSync reste responsable de la consommation et de la synchronisation des bobines.

## Architecture

| Système                                                | Responsable de                                                                                                            |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| [ha-bambulab](https://github.com/greghesp/ha-bambulab) | l'état de l'imprimante                                                                                                    |
| [HaspelSync](https://github.com/Rdiger-36/HaspelSync)  | la correspondance slot ↔ bobine, la consommation réelle (G-code, prints annulés inclus), son enregistrement dans Spoolman |
| [Spoolman](https://github.com/Donkie/Spoolman)         | bobines, matériaux, couleurs, prix                                                                                        |
| **cette intégration**                                  | assembler le tout et calculer les coûts                                                                                   |

Tout fonctionne **en local**, sans accès internet.

## Dépendances

- Home Assistant 2025.2+ et **ha-bambulab** (obligatoires) ;
- une **prise connectée** : capteur de puissance (W) **ou** compteur d'énergie cumulé (Wh/kWh), idéalement les deux ;
- une **entité prix de l'électricité** (`sensor` ou `input_number`, en EUR/kWh, c€/kWh, EUR/MWh…) ;
- **HaspelSync** et **Spoolman** : optionnels, nécessaires pour le coût filament.

Sans HaspelSync ni Spoolman, seule l'électricité est suivie et le coût total = coût électrique.

## Installation (HACS)

1. HACS → ⋮ → **Dépôts personnalisés** → `https://github.com/ZoziLaMalice/ha-3d-print-cost`, catégorie **Intégration** ;
2. installer **3D Print Cost**, redémarrer Home Assistant ;
3. **Paramètres → Appareils et services → Ajouter une intégration → 3D Print Cost**.

## Configuration

Tout se fait dans l'interface, sans YAML :

1. **Imprimante** : l'appareil Bambu Lab ;
2. **Entités de l'imprimante** : détectées automatiquement par `translation_key` / `unique_id` (jamais par `entity_id`), modifiables ;
3. **Électricité** : puissance, compteur d'énergie, prix, méthode d'intégration ;
4. **Filament (optionnel)** : URL + clé API HaspelSync, URL Spoolman, politique de repli.

Tout est modifiable ensuite via **Configurer**, sans supprimer l'intégration ; un print en cours survit au rechargement.

## Électricité

- **Avec un compteur d'énergie** (recommandé) : énergie = `compteur_fin − compteur_début`, exacte même si HA redémarre pendant le print.
- **Avec la puissance seule** : intégration dans le temps (`left` ou `trapézoïdale`).
- **Le prix peut changer pendant le print** (Tempo, Octopus, HP/HC) : le coût est accumulé intervalle par intervalle (`énergie × prix` de l'intervalle), avec un échantillon à chaque changement de prix. Jamais `énergie totale × dernier prix`.
- Un prix inconnu n'est **jamais** compté comme zéro : l'énergie est mesurée, `electricity_cost_complete` passe à `false` et l'attribut `notes` l'indique.

## Filament

À la fin du print : les valeurs électriques sont figées, HaspelSync est interrogé
(jusqu'à ~2 min, le temps qu'il enregistre la consommation), le prix de chaque
bobine est lu dans Spoolman, puis le coût total est calculé, sauvegardé et l'événement émis.

**Ordre de confiance pour le poids** : HaspelSync (réel) → poids slicé par slot
(ha-bambulab) → poids slicé total (prix seulement si une seule bobine) → rien.
Pour un print échoué/annulé, le poids slicé n'est utilisé que si l'option
`planned_progress` est choisie (poids × progression).

**Prix au gramme** (Spoolman) : champ personnalisé `price_per_kg` / `price_per_g`,
sinon `prix bobine ÷ poids initial`, sinon `prix filament ÷ poids du filament`.
Si une bobine n'a pas de prix, le coût filament et le total sont **inconnus**
(l'attribut `partial_total_cost` donne la somme connue).

## Entités (imprimante nommée _X2D_)

- **Print en cours** : `sensor.x2d_current_print_total_cost`, `…_electricity_cost`, `…_filament_cost`, `…_energy`, `…_filament_weight`, `…_duration`, `…_cost_per_hour`, `…_phase` ;
- **Dernier print** : `sensor.x2d_last_print_total_cost`, `…_electricity_cost`, `…_filament_cost`, `…_energy`, `…_filament_weight`, `…_duration`, `…_result` ;
- **Événement** : `event.x2d_print_finished` (et l'événement bus `ha_3d_print_cost_print_finished`) ;
- **Diagnostic** : `sensor.x2d_electricity_price`.
- **Image du dernier print** : `image.x2d_last_print_image`, copie de la miniature ha-bambulab prise à la fin du print (elle reste correcte quand le print suivant démarre).

Seuls le print en cours et le dernier print sont exposés : pas d'historique ni de statistiques dans Home Assistant.

## Cartes de dashboard

[`examples/dashboard_cards.yaml`](examples/dashboard_cards.yaml) contient cinq
cartes prêtes à l'emploi ([Bubble Card](https://github.com/Clooos/Bubble-Card) + cartes natives) :
print en cours et dernier print en version simple et complète, plus une carte
unique qui bascule de l'un à l'autre. La miniature du modèle remplace l'icône.

## Dépannage

Voir la section _Troubleshooting_ du [README anglais](README.md#troubleshooting).
Les diagnostics téléchargeables masquent la clé API et les URL.

## Licence

MIT
