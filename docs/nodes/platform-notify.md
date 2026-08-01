# Node: `platform.notify`

## Job

Deliver one alert request through available operator channels and report
whether any channel succeeded.

## Owns

Channel selection, message formatting, and delivery success semantics in
`scripts/notify.sh`.

## Does not own

It does not decide when an event is unhealthy, grade runs, retry jobs, discover
slate changes, store project status, or suppress a caller's failure.

## Input contracts

`notify.sh <subject> <body> [--quiet]` is the current interface. The desired
`AlertRequest` adds severity, event/run ID, dedupe key, timestamp, and requested
channels without embedding SMTP secrets.

## Output contracts

Exit zero means at least one channel delivered; exit one means every channel
failed. Skipped/missing channels are reported. The target `DeliveryReceipt`
records per-channel success/failure without secret material.

## Side effects and exclusive resources

It may send SMTP mail, post a macOS notification, and invoke speech. It reads
mailer credentials from `~/.kalshi/mail_env`; the file and OS notification
session are external platform resources.

## Failure contract

Every channel failure is loud and returns nonzero. One successful channel may
degrade the others but must report them; notification failure never rewrites
the originating node's verdict.

## Parallel work boundary

Delivery mechanisms and receipt semantics stay here. Callers own alert policy,
severity, deduplication timing, and terminal exit behavior, and should not
reimplement SMTP/desktop delivery.

## Verification

Run `bash -n scripts/notify.sh`, then channel-specific tests with a non-sensitive
test recipient/session. Exercise missing mail env, quiet mode, one-channel
success, and all-channel failure.

## Migration status

The unified entry point and any-channel success rule exist, but its API is
positional shell text and has no durable receipt or dedupe. Add structured
request/receipt JSON before adding remote messaging channels.

