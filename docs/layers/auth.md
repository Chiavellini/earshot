# Layer: auth (Kalshi request signing & credentials)

## Role
Owns the Kalshi credential and turns it into the three signed headers every
authenticated request carries, plus the hot-path pre-signing cache. Two types:
`Signer` (holds the key, signs) and `Presigner` (keeps fresh headers for one
`(method, path)` so the fire path never pays the RSA cost). NOT responsible for:
building request bodies or bookkeeping (executor), choosing the account per event
(that selection is `main.rs::connect_live_for`, described in Exit door). Signing
scheme is pinned in `VERIFICATIONS.md` Q3 — never change it without re-pinning.

## Entry door
- **Credential in:** either the process env (`Signer::from_env` — reads
  `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`, then the PEM file) or an explicit
  in-memory pair (`Signer::from_pem(key_id: String, pem: &str)` — accepts PKCS#1 or
  PKCS#8 RSA). `from_env` is a thin wrapper over `from_pem`.
- **Sign requests:** `Signer::headers(method, path, ts_ms)` /
  `Signer::headers_now(method, path)` → `Headers3` = `[(HDR_KEY, key_id),
  (HDR_SIG, sig), (HDR_TS, ts)]`. `path` MUST include `/trade-api/v2` and exclude
  any query string (debug-asserted).
- **Pre-sign for the hot path:** `Presigner::new(signer, method, path) -> Arc<Self>`;
  `Presigner::spawn_refresher(handle)` starts the refresh loop; `Presigner::get()` is
  the wait-free read.
- **Identity read-back:** `Signer::key_id() -> &str` (the access-key id, used by the
  arm-time credential check — see Exit door).

## Exit door
- **Signed headers:** `HDR_KEY` / `HDR_SIG` / `HDR_TS` (constants
  `KALSHI-ACCESS-KEY` / `-SIGNATURE` / `-TIMESTAMP`). Signature = RSA-PSS/SHA-256
  (salt = digest len) over the exact string `{ts_ms}{METHOD}{path}`. The body is NOT
  covered — order bodies are pre-serialized at arm time, only these three headers are
  computed near fire time.
- **`Presigner::get() -> (Headers3, u64)`** — fresh headers + the presigned entry's
  age in ns (for the `presign_age` telemetry edge). Wait-free `ArcSwap` load; if the
  entry is older than `PRESIGN_MAX_AGE` (20 s) it re-signs inline (correctness over
  speed) and warns. The refresher re-signs every `PRESIGN_REFRESH` (10 s) — 3× / 1.5×
  margin against the measured ±30 s server window.
- **Consumers:** `RestClient` (`src/kalshi/rest.rs`) holds one `Arc<Signer>` and signs
  read paths directly; the executor holds a `Presigner` in `ExecCtx` for the order POST;
  `book::run_ws` takes the `Arc<Signer>` for the WS auth. Each of these owns exactly one
  `Arc<Signer>` — no global signer state, so two events (two accounts) are isolated at
  the type level (see `docs/MULTI-EVENT.md` §8).

### Credential-source selection (`main.rs::connect_live_for`)
Which credential a live event uses is decided at arm time, NOT here:
- If the config's `[kalshi]` names `key_id` + `private_key_path`
  (`docs/layers/config.md`), the signer is built via `Signer::from_pem` from THIS
  config — never ambient env — so each event signs only with its own key.
- Otherwise `Signer::from_env` (single-account posture; keeps existing configs working).
- The signed `balance()` self-test in `connect_live_for` is the coherence proof: a PEM
  that doesn't match `key_id` fails the signature there and refuses to arm. The
  `expected_key_id` config tripwire (checked in `config::validate`) guards a config
  whose declared intent and `key_id` disagree. Proving the key belongs to a *specific*
  account beyond "it can sign" is the remote-probe tier (out of scope; see MULTI-EVENT §8).

## Obligations
- Sign the FULL path from the API root (`/trade-api/…`) and NEVER the query string
  (both debug-asserted in `headers`).
- The fire path must not block on RSA: reads go through `Presigner::get` (ArcSwap),
  never `headers_now`, except the deliberate stale-entry inline fallback.
- Secrets never touch the repo or the config: the config carries only the key id and a
  PEM *path*; the key material lives at that path or in `KALSHI_PRIVATE_KEY_PATH`.
- `key_id` is an access-key id (an identifier), not a secret — safe to log and to place
  in a config.

## Failure policy
- Missing env var, unreadable PEM, or unparseable key → `KalshiError::Credentials`;
  the caller refuses to arm (nothing signs). No fallback to an unsigned request.
- Presigned entry stale past `PRESIGN_MAX_AGE` → inline re-sign + warn, never send a
  stale signature.

## How to replace or edit this layer
- Everything is `src/kalshi/auth.rs`; credential *selection* wiring is
  `main.rs::connect_live_for`, and the `[kalshi]` config fields it reads are owned by
  `docs/layers/config.md`.
- Contract: the exact signed-string format, the three header names, and the
  refresh/max-age windows are wire facts — pinned in `VERIFICATIONS.md` Q3; update it
  with evidence before changing any of them.
- Adding a new credential source (e.g. a secrets manager) means adding a `Signer`
  constructor alongside `from_env`/`from_pem` and a branch in `connect_live_for` — the
  downstream types (`RestClient`, `Presigner`) are unaffected.

## How to test in isolation
- `src/kalshi/auth.rs` unit tests: `signature_verifies_against_exact_message_format`
  (RSA-PSS verify against the exact `{ts}{METHOD}{path}` string, header names/values),
  plus the presigner freshness/fallback behavior.
- The Presigner fire-path is exercised end-to-end in `tests/exec_mock.rs` (exactly one
  signed POST per intent).
- The credential-source selection and `expected_key_id` check are covered at the config
  layer (`cargo test config::` — the `kalshi_*` binding tests) and by the `balance()`
  self-test on any live-connected command.
