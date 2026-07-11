//! Kalshi execution layer — lands in M2 (spec §5.7).
//!
//! Will contain: WS order-book cache, request signing, pre-staged IOC
//! marketable-limit orders, connection warming, burst executor. It consumes
//! `DecisionIntent`s from a ring fed by the decision stage and implements
//! `config::MarketValidator` for arm-time validation.
//!
//! Transport/signing/rate-limit facts are recorded in VERIFICATIONS.md — code
//! here must cite that file, not remembered API details.
