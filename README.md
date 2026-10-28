# StockWatch

Personal stock research app for US stocks. Advice only, never places trades.

- Plan: [docs/PLAN.md](docs/PLAN.md)
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- M0 verification: [docs/VERIFY.md](docs/VERIFY.md)
- M1 backfill, findings and decisions: [docs/M1.md](docs/M1.md)
- M2 nightly pipeline and backups: [docs/M2.md](docs/M2.md)
- M3 signals and the 2013 to 2019 backtest: [docs/M3.md](docs/M3.md)
- M4 agent test set and model choice: [docs/M4.md](docs/M4.md)
- M5 holdings watch and alerts: [docs/M5.md](docs/M5.md)

Status: M0 and M1 done. M2 in its week of scheduled nightly runs. M3 design frozen (tag m3-frozen). M3b ran once on 2020 to 2026: gate **fail**, 10.6% a year against SPY's 15.1%, 0 of 17 knob changes win (docs/M3.md). Per the plan, the design goes back to the drawing board and the app to paper mode. M4 model choice made from the test set: Haiku 5.5 routine, Opus 5.5 judge (docs/M4.md). Groundwork for M4 to M6 (agent document checks, holdings and alerts, the trade Worker, the site and its data contract) is built and tested but not deployed.
