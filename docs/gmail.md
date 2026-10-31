# Gmail setup and application guarantees

## Operator setup

1. Create a Google Cloud project and enable Gmail API.
2. Configure the OAuth consent screen and add beta test accounts while in testing mode.
3. Create a **Web application** OAuth client. Register exactly
   `http://localhost:8000/gmail/callback/` locally; use HTTPS for production.
4. Put GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI in the untracked
   `.env` (or production secrets manager).
5. Generate TOKEN_ENCRYPTION_KEY and store it outside Git:

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Restart web and worker with these values. Connect Gmail from the application. OAuth
state is user/session bound, single use, expires in ten minutes, and uses PKCE.
Tokens are encrypted with Fernet before database storage. Keep the encryption key
backed up separately; losing it requires users to reconnect. A rotation requires
re-encryption, not replacing the key blindly.

Scopes are gmail.send and gmail.readonly. Read-only mailbox permission is broader
than the specific sent-message query, but is needed by Gmail's API to search for a
Message-ID after ambiguous delivery. The implementation searches sent mail and does
not fetch mailbox message bodies. Public distribution needs Google's applicable
OAuth verification/restricted-scope assessment before launch. Testing-mode refresh
tokens can expire; reconnect is a normal lifecycle action.

## User workflow

Upload a private PDF, prepare the recipient/subject/message and select an attachment.
Review it, then choose **Approve and send this application**. Beat dispatches the
approved outbox entry; a worker sends MIME mail with a stable RFC Message-ID. Gmail
confirmation means Gmail accepted the send, not that an employer received/read it.

Edits invalidate approval. Replacing a resume requires uploading a new document;
existing documents are immutable through the product UI. Attachment bytes are hashed
again immediately before sending. New sender identity invalidates existing approval.

## Failures

- Preflight problems: failed, without a network send. Fix the draft and approve again.
- Explicit Gmail rejection or revoked credentials: failed; reconnect/fix and reapprove.
- Timeout, unexpected response, server error or interrupted worker: unknown.
- Reconciliation searches sent mail by exact Message-ID. One match confirms success.
  No match leaves the outcome unknown: absence is not evidence of non-delivery.

There is no auto-resend for unknown or confirmed applications. Disconnect clears local
tokens immediately; revoke provider-side access in Google account settings as well.
A submission already in flight may still complete. Real OAuth consent, token refresh,
send and reconciliation must be smoke-tested with dedicated test accounts before beta.

References: [OAuth web flow](https://developers.google.com/identity/protocols/oauth2/web-server),
[Gmail send](https://developers.google.com/workspace/gmail/api/guides/sending).
