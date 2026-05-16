# glitchmutator
glitchmutator is a mutation-testing-inspired campaign driver for exploring PicoGlitcher timing and pattern space while classifying, scoring, and reproducing non-reset fault behaviors.

## PoC campaign driver

Run the conservative workshop PoC driver:

```bash
python /home/runner/work/glitchmutator/glitchmutator/campaign_driver.py --dry-run --output campaign.jsonl
```

Use `--backend findus` (preferred), `--backend raw-pyboard` (fallback), or `--backend mock` for simulation.
