# Timeline Analytics (v0.5.0)

## Pull segmentation

The analyzer approximates Mythic+ pull boundaries from hostile player↔NPC combat activity. A new pull begins after a configurable idle gap (default 8 seconds).

This is an approximation: chain pulls can remain merged and combat logs do not encode the group's conceptual route pull boundaries directly.

For each pull the app computes:

- duration
- observed enemy names/count
- team damage / DPS
- team casts
- interrupts
- player deaths
- Boss vs Trash/Other label when encounter markers are present

## Player per-pull view

For a selected player:

- damage / DPS per pull
- casts per pull
- casts per minute
- top skill damage shares

## Death contexts

For every recorded player death the app looks back 10 seconds and summarizes:

- incoming damage
- healing received
- player's own cast count
- last cast
- top incoming damage sources

## Burst windows

The app finds non-overlapping high-damage windows (default 10 seconds) and reports damage, window DPS and top skill composition.

These are observed damage peaks, not a class-specific definition of “burst cooldown window”.
